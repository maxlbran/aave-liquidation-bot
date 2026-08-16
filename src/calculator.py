"""
Profitability calculator.

Given a liquidatable position, determines:
  1. Which collateral / debt pair to target (highest expected profit)
  2. The exact debt amount to cover (≤ 50% close factor)
  3. Net profit after flash loan fee, swap slippage, and gas

Uses Aave V3's built-in oracle (priced in USD, 8 decimals) so no
external price feed dependency.
"""

import json
import logging
from dataclasses import dataclass
from typing import Optional

from web3 import AsyncWeb3

import config
from src.database import Position

logger = logging.getLogger(__name__)

WAD = 10 ** 18

# ─── Output types ─────────────────────────────────────────────────────────────

@dataclass
class LiquidationOpportunity:
    position_address: str
    collateral_asset: str          # address
    debt_asset: str                # address
    debt_amount_wei: int           # exact amount to cover (in debt-token smallest unit)
    debt_amount_usd: float
    collateral_expected_usd: float # after bonus
    flash_fee_usd: float
    swap_slippage_usd: float
    gas_cost_usd: float
    net_profit_usd: float
    swap_fee_tier: int             # Uniswap fee tier (500/3000/10000)
    swap_path: bytes               # b"" for single-hop, encoded path for multi-hop
    min_profit_wei: int            # minProfitWei passed to contract (in debt-asset units)


# ─── Oracle ABI (minimal) ──────────────────────────────────────────────────────
ORACLE_ABI = [
    {
        "inputs": [{"name": "asset", "type": "address"}],
        "name": "getAssetPrice",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "assets", "type": "address[]"}],
        "name": "getAssetsPrices",
        "outputs": [{"name": "", "type": "uint256[]"}],
        "stateMutability": "view",
        "type": "function",
    },
]

# Data-provider per-user-reserve ABI
USER_RESERVES_ABI = [
    {
        "inputs": [
            {"name": "provider", "type": "address"},
            {"name": "user",     "type": "address"},
        ],
        "name": "getUserReservesData",
        "outputs": [
            {
                "components": [
                    {"name": "underlyingAsset",               "type": "address"},
                    {"name": "scaledATokenBalance",           "type": "uint256"},
                    {"name": "usageAsCollateralEnabledOnUser","type": "bool"},
                    {"name": "stableBorrowRate",              "type": "uint256"},
                    {"name": "scaledVariableDebt",           "type": "uint256"},
                    {"name": "principalStableDebt",          "type": "uint256"},
                    {"name": "stableBorrowLastUpdateTimestamp","type": "uint256"},
                ],
                "name": "",
                "type": "tuple[]",
            },
            {"name": "userEmodeCategoryId", "type": "uint8"},
        ],
        "stateMutability": "view",
        "type": "function",
    }
]

# Reserve config ABI (to get liquidation bonus)
RESERVE_CONFIG_ABI = [
    {
        "inputs": [{"name": "asset", "type": "address"}],
        "name": "getReserveData",
        "outputs": [
            {
                "components": [
                    {
                        "components": [{"name": "data", "type": "uint256"}],
                        "name": "configuration",
                        "type": "tuple",
                    },
                    {"name": "liquidityIndex",           "type": "uint128"},
                    {"name": "currentLiquidityRate",     "type": "uint128"},
                    {"name": "variableBorrowIndex",      "type": "uint128"},
                    {"name": "currentVariableBorrowRate","type": "uint128"},
                    {"name": "currentStableBorrowRate",  "type": "uint128"},
                    {"name": "lastUpdateTimestamp",      "type": "uint40"},
                    {"name": "id",                       "type": "uint16"},
                    {"name": "aTokenAddress",            "type": "address"},
                    {"name": "stableDebtTokenAddress",   "type": "address"},
                    {"name": "variableDebtTokenAddress", "type": "address"},
                    {"name": "interestRateStrategyAddress","type": "address"},
                    {"name": "accruedToTreasury",        "type": "uint128"},
                    {"name": "unbacked",                 "type": "uint128"},
                    {"name": "isolationModeTotalDebt",   "type": "uint128"},
                ],
                "name": "",
                "type": "tuple",
            }
        ],
        "stateMutability": "view",
        "type": "function",
    }
]

ERC20_DECIMALS_ABI = [
    {
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function",
    }
]


class Calculator:
    def __init__(self, w3: AsyncWeb3):
        self.w3 = w3
        self._oracle = w3.eth.contract(
            address=w3.to_checksum_address(config.AAVE_ORACLE_MAINNET),
            abi=ORACLE_ABI,
        )
        self._pool = w3.eth.contract(
            address=w3.to_checksum_address(config.AAVE_POOL_ADDRESS),
            abi=RESERVE_CONFIG_ABI,
        )
        self._data_provider = w3.eth.contract(
            address=w3.to_checksum_address(config.AAVE_DATA_PROVIDER),
            abi=USER_RESERVES_ABI,
        )
        # Decimals cache  {address: int}
        self._decimals_cache: dict[str, int] = {}
        # Price cache {address: float}  refreshed per block
        self._price_cache: dict[str, float] = {}

    # ── Public ────────────────────────────────────────────────────────────────

    async def find_best_opportunity(
        self,
        pos: Position,
        eth_price_usd: float,
        gas_price_wei: int,
    ) -> Optional[LiquidationOpportunity]:
        """
        Evaluate all (collateral, debt) pairs for a position and return
        the most profitable one, or None if nothing meets the threshold.
        """
        collateral_addrs: list[str] = json.loads(pos.collateral_assets)
        debt_addrs:       list[str] = json.loads(pos.debt_assets)

        if not collateral_addrs or not debt_addrs:
            # Need per-reserve detail to find actual assets
            collateral_addrs, debt_addrs = await self._fetch_user_reserves(pos.address)
            if not collateral_addrs or not debt_addrs:
                return None

        # Fetch prices for all assets in one batch call
        all_assets = list(set(collateral_addrs + debt_addrs))
        prices = await self._batch_prices(all_assets)

        gas_cost_usd = self._estimate_gas_cost(gas_price_wei, eth_price_usd)

        best: Optional[LiquidationOpportunity] = None

        for debt_asset in debt_addrs:
            debt_price = prices.get(debt_asset, 0)
            if debt_price == 0:
                continue

            debt_decimals = await self._get_decimals(debt_asset)
            debt_balance_usd = pos.total_debt_usd  # approximate; refine below if needed

            # Max 50% close factor
            max_debt_usd   = debt_balance_usd * config.MAX_LIQ_CLOSE_FACTOR
            max_debt_token = max_debt_usd / debt_price           # in token units (float)
            debt_amount_wei = int(max_debt_token * (10 ** debt_decimals))

            for coll_asset in collateral_addrs:
                coll_price = prices.get(coll_asset, 0)
                if coll_price == 0:
                    continue

                bonus = config.LIQUIDATION_BONUS.get(
                    coll_asset, config.DEFAULT_LIQUIDATION_BONUS
                )
                opp = self._calc_profit(
                    pos.address,
                    coll_asset,
                    debt_asset,
                    debt_amount_wei,
                    max_debt_usd,
                    coll_price,
                    debt_price,
                    bonus,
                    gas_cost_usd,
                    debt_decimals,
                )
                if opp is None:
                    continue
                if best is None or opp.net_profit_usd > best.net_profit_usd:
                    best = opp

        if best:
            logger.info(
                "Best opportunity: user=%s profit=$%.2f (coll=%s debt=%s)",
                pos.address, best.net_profit_usd,
                best.collateral_asset[-6:], best.debt_asset[-6:],
            )
        return best

    # ── Private helpers ───────────────────────────────────────────────────────

    def _calc_profit(
        self,
        user: str,
        coll_asset: str,
        debt_asset: str,
        debt_amount_wei: int,
        debt_amount_usd: float,
        coll_price: float,
        debt_price: float,
        bonus: float,
        gas_cost_usd: float,
        debt_decimals: int,
    ) -> Optional[LiquidationOpportunity]:
        """Pure calculation – no I/O."""
        # Collateral received (USD) = debt * (1 + bonus)
        coll_received_usd = debt_amount_usd * (1 + bonus)

        # Flash loan fee
        flash_fee_usd = debt_amount_usd * config.FLASH_LOAN_FEE

        # Swap slippage (on collateral value)
        slippage_usd = coll_received_usd * config.SWAP_SLIPPAGE

        net_profit = coll_received_usd - debt_amount_usd - flash_fee_usd - slippage_usd - gas_cost_usd

        if net_profit < config.MIN_PROFIT_USD:
            return None

        # Choose swap fee tier for collateral → debt
        swap_fee = config.UNISWAP_FEE_TIERS.get(coll_asset, config.DEFAULT_FEE_TIER)

        # minProfitWei: convert $MIN_PROFIT_USD back to debt-asset units
        min_profit_usd_in_debt = config.MIN_PROFIT_USD / debt_price
        min_profit_wei = int(min_profit_usd_in_debt * (10 ** debt_decimals))

        return LiquidationOpportunity(
            position_address=user,
            collateral_asset=coll_asset,
            debt_asset=debt_asset,
            debt_amount_wei=debt_amount_wei,
            debt_amount_usd=debt_amount_usd,
            collateral_expected_usd=coll_received_usd,
            flash_fee_usd=flash_fee_usd,
            swap_slippage_usd=slippage_usd,
            gas_cost_usd=gas_cost_usd,
            net_profit_usd=net_profit,
            swap_fee_tier=swap_fee,
            swap_path=b"",   # single-hop; multi-hop logic can be added per pair
            min_profit_wei=min_profit_wei,
        )

    async def _fetch_user_reserves(self, user: str) -> tuple[list[str], list[str]]:
        """
        Query data provider for per-reserve balances to identify which assets
        a user actually holds as collateral and debt.
        """
        try:
            (reserves, _) = await self._data_provider.functions.getUserReservesData(
                self.w3.to_checksum_address(config.AAVE_ADDRESSES_PROVIDER),
                self.w3.to_checksum_address(user),
            ).call()
        except Exception as exc:
            logger.warning("getUserReservesData failed for %s: %s", user, exc)
            return [], []

        collateral = []
        debt       = []
        for r in reserves:
            addr = r[0]  # underlyingAsset
            scaled_a_bal  = r[1]  # scaledATokenBalance
            as_coll       = r[2]  # usageAsCollateralEnabledOnUser
            scaled_var_d  = r[4]  # scaledVariableDebt
            principal_s_d = r[5]  # principalStableDebt

            if scaled_a_bal > 0 and as_coll:
                collateral.append(addr)
            if scaled_var_d > 0 or principal_s_d > 0:
                debt.append(addr)

        return collateral, debt

    async def _batch_prices(self, assets: list[str]) -> dict[str, float]:
        """Return {asset_address: price_usd} using Aave oracle (8 decimals)."""
        checksummed = [self.w3.to_checksum_address(a) for a in assets]
        try:
            prices_raw = await self._oracle.functions.getAssetsPrices(checksummed).call()
        except Exception as exc:
            logger.warning("Batch price fetch failed: %s", exc)
            return {}
        # Oracle returns price in USD with 8 decimals
        return {addr: raw / 1e8 for addr, raw in zip(assets, prices_raw)}

    async def _get_decimals(self, token: str) -> int:
        if token in self._decimals_cache:
            return self._decimals_cache[token]
        contract = self.w3.eth.contract(
            address=self.w3.to_checksum_address(token),
            abi=ERC20_DECIMALS_ABI,
        )
        try:
            d = await contract.functions.decimals().call()
        except Exception:
            d = 18  # fallback
        self._decimals_cache[token] = d
        return d

    @staticmethod
    def _estimate_gas_cost(gas_price_wei: int, eth_price_usd: float) -> float:
        gas_eth = (config.GAS_LIMIT_ESTIMATE * gas_price_wei) / 1e18
        return gas_eth * eth_price_usd
