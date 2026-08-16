"""
Transaction executor.

Receives LiquidationOpportunity objects from the queue, builds and submits
the on-chain transaction, waits for confirmation, and records the result.
"""

import asyncio
import json
import logging
import time
from typing import Optional

from web3 import AsyncWeb3
from web3.exceptions import ContractLogicError, TransactionNotFound
from web3.types import TxReceipt

import config
from src.calculator import Calculator, LiquidationOpportunity
from src.database import Database, Execution, Opportunity
from src.alerts import Alerts

logger = logging.getLogger(__name__)

WAD = 10 ** 18

# ─── Confirmation settings ────────────────────────────────────────────────────
TX_TIMEOUT_SECONDS  = 120   # wait up to 2 min for receipt
TX_POLL_INTERVAL    = 2     # seconds between receipt polls
MAX_RECEIPT_RETRIES = 60    # 60 × 2s = 120s


class Executor:
    def __init__(
        self,
        w3: AsyncWeb3,
        db: Database,
        alerts: Alerts,
        opportunity_queue: asyncio.Queue,
    ):
        self.w3      = w3
        self.db      = db
        self.alerts  = alerts
        self.q       = opportunity_queue
        self._calc   = Calculator(w3)

        # Load contract ABI
        with open(config.ABIS_DIR / "liquidation_bot.json") as f:
            bot_abi = json.load(f)

        if not config.BOT_CONTRACT_ADDRESS:
            raise ValueError("BOT_CONTRACT_ADDRESS not set in config / .env")

        self._contract = w3.eth.contract(
            address=w3.to_checksum_address(config.BOT_CONTRACT_ADDRESS),
            abi=bot_abi,
        )
        self._account = w3.eth.account.from_key(config.PRIVATE_KEY)

    # ─── Public entrypoint ──────────────────────────────────────────────────
    async def run(self):
        """Consume opportunities from queue and execute liquidations."""
        logger.info("Executor started")
        while True:
            try:
                pos = await self.q.get()
                await self._handle_position(pos)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.exception("Executor error: %s", exc)
            finally:
                self.q.task_done()

    # ─── Per-position handler ────────────────────────────────────────────────
    async def _handle_position(self, pos):
        from src.database import Position
        if not isinstance(pos, Position):
            return

        logger.info("Evaluating opportunity: %s", pos.address)

        # Get current gas price
        gas_price_wei = await self._get_gas_price()
        if gas_price_wei / 1e9 > config.MAX_GAS_GWEI:
            logger.warning(
                "Gas price %.2f gwei exceeds limit %.2f — skipping",
                gas_price_wei / 1e9, config.MAX_GAS_GWEI,
            )
            return

        # Fetch ETH price for gas cost estimate
        eth_price_usd = await self._get_eth_price_usd()

        opp = await self._calc.find_best_opportunity(pos, eth_price_usd, gas_price_wei)
        if opp is None:
            logger.info("No profitable opportunity for %s", pos.address)
            return

        logger.info(
            "Executing: user=%s profit=$%.2f debt=%s",
            pos.address, opp.net_profit_usd, opp.debt_asset[-6:],
        )

        # Record in DB
        db_opp = Opportunity(
            timestamp=int(time.time()),
            position_address=opp.position_address,
            collateral_asset=opp.collateral_asset,
            debt_asset=opp.debt_asset,
            debt_amount=opp.debt_amount_usd,
            estimated_profit_usd=opp.net_profit_usd,
        )
        opp_id = self.db.record_opportunity(db_opp)

        # Build + send tx
        try:
            receipt = await self._execute(opp, gas_price_wei)
        except Exception as exc:
            reason = str(exc)
            logger.error("Execution failed: %s", reason)
            self.db.mark_opportunity_failed(opp_id, reason)
            await self.alerts.send_failed_execution(opp, reason)
            return

        # Parse actual profit from event
        actual_profit_usd = self._parse_profit_from_receipt(receipt, opp)

        self.db.mark_opportunity_executed(opp_id, receipt["transactionHash"].hex(), actual_profit_usd)

        gas_used_eth = (receipt["gasUsed"] * gas_price_wei) / 1e18
        gas_cost_usd = gas_used_eth * eth_price_usd

        ex = Execution(
            timestamp=int(time.time()),
            tx_hash=receipt["transactionHash"].hex(),
            position_address=opp.position_address,
            collateral_asset=opp.collateral_asset,
            debt_asset=opp.debt_asset,
            debt_amount_usd=opp.debt_amount_usd,
            collateral_received_usd=opp.collateral_expected_usd,
            profit_usd=actual_profit_usd,
            gas_cost_usd=gas_cost_usd,
            block_number=receipt["blockNumber"],
        )
        self.db.record_execution(ex)

        await self.alerts.send_successful_liquidation(opp, actual_profit_usd, receipt["transactionHash"].hex())
        logger.info(
            "Liquidation confirmed: tx=%s profit=$%.2f",
            receipt["transactionHash"].hex(), actual_profit_usd,
        )

    # ─── Build and submit transaction ────────────────────────────────────────
    async def _execute(
        self, opp: LiquidationOpportunity, gas_price_wei: int
    ) -> TxReceipt:
        nonce = await self.w3.eth.get_transaction_count(self._account.address, "pending")

        tx = await self._contract.functions.executeLiquidation(
            self.w3.to_checksum_address(opp.collateral_asset),
            self.w3.to_checksum_address(opp.debt_asset),
            self.w3.to_checksum_address(opp.position_address),
            opp.debt_amount_wei,
            opp.swap_fee_tier,
            opp.swap_path,
            opp.min_profit_wei,
        ).build_transaction({
            "from":     self._account.address,
            "nonce":    nonce,
            "gas":      config.GAS_LIMIT_ESTIMATE,
            "gasPrice": int(gas_price_wei * config.GAS_PRICE_MULTIPLIER),
            "chainId":  config.CHAIN_ID,
        })

        signed = self._account.sign_transaction(tx)
        tx_hash = await self.w3.eth.send_raw_transaction(signed.raw_transaction)
        logger.info("Tx submitted: %s", tx_hash.hex())

        receipt = await self._wait_for_receipt(tx_hash)
        if receipt["status"] != 1:
            raise RuntimeError(f"Transaction reverted: {tx_hash.hex()}")
        return receipt

    async def _wait_for_receipt(self, tx_hash) -> TxReceipt:
        for attempt in range(MAX_RECEIPT_RETRIES):
            try:
                receipt = await self.w3.eth.get_transaction_receipt(tx_hash)
                if receipt is not None:
                    return receipt
            except TransactionNotFound:
                pass
            await asyncio.sleep(TX_POLL_INTERVAL)
        raise TimeoutError(f"Receipt not found after {TX_TIMEOUT_SECONDS}s: {tx_hash.hex()}")

    # ─── Gas helpers ──────────────────────────────────────────────────────────
    async def _get_gas_price(self) -> int:
        return await self.w3.eth.gas_price

    async def _get_eth_price_usd(self) -> float:
        """Quick price via Aave oracle for WETH."""
        oracle_abi = [
            {
                "inputs": [{"name": "asset", "type": "address"}],
                "name": "getAssetPrice",
                "outputs": [{"name": "", "type": "uint256"}],
                "stateMutability": "view",
                "type": "function",
            }
        ]
        oracle = self.w3.eth.contract(
            address=self.w3.to_checksum_address(config.AAVE_ORACLE_MAINNET),
            abi=oracle_abi,
        )
        try:
            price_raw = await oracle.functions.getAssetPrice(
                self.w3.to_checksum_address(config.TOKENS["WETH"])
            ).call()
            return price_raw / 1e8  # Aave oracle: 8 decimals, USD
        except Exception:
            return 2500.0  # conservative fallback

    # ─── Parse events ─────────────────────────────────────────────────────────
    def _parse_profit_from_receipt(
        self, receipt: TxReceipt, opp: LiquidationOpportunity
    ) -> float:
        """
        Extract actual profit from LiquidationExecuted event.
        Falls back to estimated profit if event parsing fails.
        """
        try:
            logs = self._contract.events.LiquidationExecuted().process_receipt(receipt)
            if logs:
                profit_wei = logs[0]["args"]["profit"]
                # Convert from debt-asset units to USD using estimated debt price
                if opp.debt_amount_wei > 0:
                    debt_price_usd = opp.debt_amount_usd / (opp.debt_amount_wei / 1e18)
                    return (profit_wei / 1e18) * debt_price_usd
        except Exception as exc:
            logger.warning("Could not parse LiquidationExecuted event: %s", exc)
        return opp.net_profit_usd
