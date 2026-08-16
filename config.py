"""
Central configuration for the Aave V3 Liquidation Bot.

All secrets come from environment variables loaded from .env.
Never hardcode private keys or API keys here.
"""

import os
from decimal import Decimal
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ─── RPC ──────────────────────────────────────────────────────────────────────
ALCHEMY_WS_URL   = os.environ["ALCHEMY_WS_URL"]    # wss://arb-mainnet.g.alchemy.com/v2/...
ALCHEMY_HTTP_URL = os.environ["ALCHEMY_HTTP_URL"]  # https://arb-mainnet.g.alchemy.com/v2/...

# ─── Wallet ───────────────────────────────────────────────────────────────────
WALLET_ADDRESS = os.environ["WALLET_ADDRESS"]   # checksummed 0x...
PRIVATE_KEY    = os.environ["PRIVATE_KEY"]      # 0x... or bare hex

# ─── Network ──────────────────────────────────────────────────────────────────
CHAIN_ID = int(os.getenv("CHAIN_ID", "42161"))  # 42161 = Arbitrum One, 421614 = Sepolia

# ─── Aave V3 Addresses ────────────────────────────────────────────────────────
# Arbitrum Mainnet
AAVE_POOL_MAINNET             = "0x794a61358D6845594F94dc1DB02A252b5b4814aD"
AAVE_ADDRESSES_PROVIDER_MAINNET = "0xa97684ead0e402dC232d5A977953DF7ECBaB3CDb"
AAVE_DATA_PROVIDER_MAINNET    = "0x69FA688f1Dc47d4B5d8029D5a35FB7a548310654"
AAVE_ORACLE_MAINNET           = "0xb56c2F0B653B2e0b10C9b928C8580Ac5Df02C7C7"

# Arbitrum Sepolia (testnet)
AAVE_POOL_TESTNET             = "0xBfC91D59fdAA134A4ED45f7B584cAf96D7792Eff"
AAVE_ADDRESSES_PROVIDER_TESTNET = "0x4EEE0BB72C2717310a142C8Db7a84b2d72dF2f55"
AAVE_DATA_PROVIDER_TESTNET    = "0x5bbB62E8B9C2dC2E0107B9a4f1A2504D0A4aAfD2"

AAVE_POOL_ADDRESS = (
    AAVE_POOL_MAINNET if CHAIN_ID == 42161 else AAVE_POOL_TESTNET
)
AAVE_DATA_PROVIDER = (
    AAVE_DATA_PROVIDER_MAINNET if CHAIN_ID == 42161 else AAVE_DATA_PROVIDER_TESTNET
)
AAVE_ADDRESSES_PROVIDER = (
    AAVE_ADDRESSES_PROVIDER_MAINNET if CHAIN_ID == 42161 else AAVE_ADDRESSES_PROVIDER_TESTNET
)

# ─── Uniswap V3 ───────────────────────────────────────────────────────────────
UNISWAP_ROUTER    = "0xE592427A0AEce92De3Edee1F18E0157C05861564"  # Arbitrum SwapRouter
UNISWAP_ROUTER_02 = "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45"  # SwapRouter02

# ─── Token Addresses (Arbitrum One) ───────────────────────────────────────────
TOKENS = {
    "USDC":  "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",  # native USDC
    "USDCe": "0xFF970A61A04b1cA14834A43f5dE4533eBDDB5CC8",  # bridged USDC.e
    "USDT":  "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9",
    "DAI":   "0xDA10009cBd5D07dd0CeCc66161FC93D7c9000da1",
    "WETH":  "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1",
    "WBTC":  "0x2f2a2543B76A4166549F7aaB2e75Bef0aefC5B0f",
    "ARB":   "0x912CE59144191C1204E64559FE8253a0e49E6548",
    "LINK":  "0xf97f4df75117a78c1A5a0DBb814Af92458539FB4",
    "UNI":   "0xFa7F8980b0f1E64A2062791cc3b0871572f1F7f0",
    "AAVE":  "0xba5DdD1f9d7F570dc94a51479a000E3BCE967196",
    "EURS":  "0xD22a58f79e9481D1a88e00c343885A588b34b68B",
    "wstETH":"0x5979D7b546E38E414F7E9822514be443A4800529",
    "rETH":  "0xEC70Dcb4A1EFa46b8F2D97C310C9c4790ba5ffA",
    "LUSD":  "0x93b346b6BC2548dA6A1E7d98E9a421B42541425b",
    "FRAX":  "0x17FC002b466eEc40DaE837Fc4bE5c67993ddBd6",
}

# Token to use as reference for profit denomination
PROFIT_TOKEN = "USDC"
USDC_ADDRESS = TOKENS["USDC"]

# ─── Liquidation bonus per collateral asset ────────────────────────────────────
# Source: Aave V3 Arbitrum risk parameters
# bonus = (reserveLiquidationBonus - 10000) / 10000  e.g. 10500 → 5%
LIQUIDATION_BONUS: dict[str, float] = {
    TOKENS["WETH"]:   0.05,
    TOKENS["WBTC"]:   0.05,
    TOKENS["USDC"]:   0.04,
    TOKENS["USDCe"]:  0.04,
    TOKENS["USDT"]:   0.04,
    TOKENS["DAI"]:    0.04,
    TOKENS["ARB"]:    0.10,
    TOKENS["LINK"]:   0.075,
    TOKENS["UNI"]:    0.10,
    TOKENS["AAVE"]:   0.10,
    TOKENS["wstETH"]: 0.075,
    TOKENS["rETH"]:   0.075,
    TOKENS["LUSD"]:   0.05,
    TOKENS["FRAX"]:   0.05,
}
DEFAULT_LIQUIDATION_BONUS = 0.10

# Preferred Uniswap V3 fee tier per token pair (token_address -> fee_bps)
UNISWAP_FEE_TIERS: dict[str, int] = {
    TOKENS["USDC"]:  500,    # 0.05% pool (deepest for stables on Arb)
    TOKENS["USDCe"]: 500,
    TOKENS["USDT"]:  500,
    TOKENS["DAI"]:   500,
    TOKENS["WETH"]:  500,
    TOKENS["WBTC"]:  3000,
    TOKENS["ARB"]:   3000,
    TOKENS["LINK"]:  3000,
    TOKENS["UNI"]:   3000,
    TOKENS["AAVE"]:  3000,
    TOKENS["wstETH"]:100,   # wstETH-ETH correlated pool
    TOKENS["rETH"]:  500,
    TOKENS["LUSD"]:  3000,
    TOKENS["FRAX"]:  500,
}
DEFAULT_FEE_TIER = 3000  # 0.3% fallback

# ─── Our deployed contract ─────────────────────────────────────────────────────
BOT_CONTRACT_ADDRESS = os.getenv("BOT_CONTRACT_ADDRESS", "")  # set after deployment

# ─── Strategy parameters ───────────────────────────────────────────────────────
MIN_PROFIT_USD         = float(os.getenv("MIN_PROFIT_USD", "10.0"))
FLASH_LOAN_FEE         = 0.0005          # 0.05% Aave V3 flash loan fee
GAS_PRICE_MULTIPLIER   = 1.10            # 110% of current gas price
MAX_GAS_GWEI           = 2.0             # Arbitrum is cheap; abort above this
GAS_LIMIT_ESTIMATE     = 600_000         # conservative estimate for liquidation tx
SWAP_SLIPPAGE          = 0.003           # 0.3% slippage for profit estimation
MAX_LIQ_CLOSE_FACTOR   = 0.5            # Aave allows max 50% liquidation per call

# Health factor thresholds
HF_LIQUIDATABLE        = 1.0   # below this → liquidate
HF_ALERT               = 1.10  # below this → approaching, check every block
HF_WATCH               = 1.20  # below this → watch every 10 blocks
HF_SAFE                = 2.0   # above this → check every 100 blocks

# Block intervals for health factor refresh
BLOCK_CHECK_INTERVAL = {
    "liquidatable": 1,
    "approaching":  1,
    "watch":       10,
    "safe":       100,
}

# ─── Telegram ─────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")
TELEGRAM_ENABLED   = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)

# ─── Database ─────────────────────────────────────────────────────────────────
DB_PATH = Path(os.getenv("DB_PATH", "data/bot.db"))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# ─── Paths ────────────────────────────────────────────────────────────────────
BASE_DIR  = Path(__file__).parent
ABIS_DIR  = BASE_DIR / "abis"

# ─── Logging ──────────────────────────────────────────────────────────────────
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_FILE  = os.getenv("LOG_FILE", "logs/bot.log")

# ─── Heartbeat ────────────────────────────────────────────────────────────────
HEARTBEAT_INTERVAL_SECONDS = 300   # alert if no block seen in 5 min
