"""
Monitor engine.

Maintains a live view of all Aave V3 borrowers on Arbitrum via WebSocket.

Strategy:
  • Subscribe to Borrow / Repay / Supply / Withdraw / LiquidationCall events
    to keep the position set current.
  • On every new block header, refresh health factors for positions whose
    check_interval has elapsed.
  • Emit asyncio.Queue messages for the executor when a position is liquidatable.
"""

import asyncio
import json
import logging
import time
from typing import Any, Callable, Coroutine

from web3 import AsyncWeb3
from web3.providers.persistent import WebSocketProvider
from web3.types import BlockData

import config
from src.database import Database, Position
from src.alerts import Alerts

logger = logging.getLogger(__name__)

# 1e18 – Aave returns health factor with 18 decimals
WAD = 10 ** 18

# ─── Event topic hashes ───────────────────────────────────────────────────────
# Pre-computed keccak256 of event signatures (verified against Aave V3 ABI).
# keccak256 of each event signature — verified on-chain against Aave V3 Arbitrum pool
# Borrow(address,address,address,uint256,uint8,uint256,uint16)
TOPIC_BORROW = "0xb3d084820fb1a9decffb176436bd02558d15fac9b0ddfed8c465bc7359d7dce0"
# Repay(address,address,address,uint256,bool)
TOPIC_REPAY  = "0xa534c8dbe71f871f9f3530e97a74601fea17b426cae02e1c5aee42c96c784051"
# LiquidationCall(address,address,address,uint256,uint256,address,bool)
TOPIC_LIQUIDATION_CALL = "0xe413a321e8681d831f4dbccbca790d2952b56f977908e45be37335533e005286"

# ─── Helper ───────────────────────────────────────────────────────────────────

def _classify(hf: float) -> tuple[str, int]:
    """Return (status, check_interval_blocks)."""
    if hf < config.HF_LIQUIDATABLE:
        return "liquidatable", config.BLOCK_CHECK_INTERVAL["liquidatable"]
    elif hf < config.HF_ALERT:
        return "approaching",  config.BLOCK_CHECK_INTERVAL["approaching"]
    elif hf < config.HF_WATCH:
        return "watch",        config.BLOCK_CHECK_INTERVAL["watch"]
    else:
        return "safe",         config.BLOCK_CHECK_INTERVAL["safe"]


class Monitor:
    def __init__(
        self,
        db: Database,
        alerts: Alerts,
        opportunity_queue: asyncio.Queue,
    ):
        self.db    = db
        self.alerts = alerts
        self.q     = opportunity_queue
        self._last_block    = 0
        self._last_block_ts = time.time()

        # Load pool ABI
        with open(config.ABIS_DIR / "aave_pool.json") as f:
            self._pool_abi = json.load(f)

    # ─── Public entrypoint ──────────────────────────────────────────────────
    async def run(self):
        """Main loop – reconnects automatically on WebSocket errors."""
        logger.info("Monitor starting (chain_id=%d)", config.CHAIN_ID)
        while True:
            try:
                await self._connect_and_stream()
            except Exception as exc:
                logger.error("WebSocket error: %s — reconnecting in 5s", exc)
                await self.alerts.send(f"⚠️ Monitor disconnected: {exc}. Reconnecting…")
                await asyncio.sleep(5)

    # ─── Core streaming loop ────────────────────────────────────────────────
    async def _connect_and_stream(self):
        async with AsyncWeb3(WebSocketProvider(config.ALCHEMY_WS_URL)) as w3:
            logger.info("WebSocket connected")
            self._pool = w3.eth.contract(
                address=w3.to_checksum_address(config.AAVE_POOL_ADDRESS),
                abi=self._pool_abi,
            )

            # Store subscription IDs so we can route payloads by source.
            # web3.py 7.x wraps every message as:
            #   {'subscription': <id>, 'result': <block_or_log_dict>}
            block_sub_id = await w3.eth.subscribe("newHeads")
            log_sub_id   = await w3.eth.subscribe("logs", {
                "address": config.AAVE_POOL_ADDRESS,
                "topics": [[
                    TOPIC_BORROW,
                    TOPIC_REPAY,
                    TOPIC_LIQUIDATION_CALL,
                ]],
            })

            logger.info(
                "Subscriptions active – blocks=%s logs=%s",
                block_sub_id, log_sub_id,
            )

            async for msg in w3.socket.process_subscriptions():
                try:
                    await self._dispatch(w3, msg, block_sub_id, log_sub_id)
                except Exception as exc:
                    logger.exception("Error dispatching payload: %s", exc)

    # ─── Dispatch incoming subscription messages ────────────────────────────
    async def _dispatch(
        self,
        w3: AsyncWeb3,
        msg: dict,
        block_sub_id: str,
        log_sub_id: str,
    ):
        # web3.py 7.x: {'subscription': id, 'result': {...}}
        sub_id = msg.get("subscription")
        result = msg.get("result", msg)   # fall back to msg itself for safety

        if sub_id == block_sub_id or "number" in result:
            await self._on_block(w3, result)
            return

        if sub_id == log_sub_id or "topics" in result:
            topics = result.get("topics", [])
            if not topics:
                return
            sig = topics[0].lower() if isinstance(topics[0], str) else topics[0].hex()
            sig = sig if sig.startswith("0x") else "0x" + sig

            if sig == TOPIC_BORROW:
                await self._on_borrow(w3, result)
            elif sig == TOPIC_REPAY:
                await self._on_repay(w3, result)
            elif sig == TOPIC_LIQUIDATION_CALL:
                await self._on_liquidation_call(w3, result)

    # ─── Block handler ──────────────────────────────────────────────────────
    async def _on_block(self, w3: AsyncWeb3, block: dict):
        raw = block["number"]
        block_number = int(raw, 16) if isinstance(raw, str) else int(raw)
        self._last_block    = block_number
        self._last_block_ts = time.time()

        logger.debug("Block %d", block_number)

        # Fetch all positions due for a health-factor refresh
        due = self.db.get_positions_due(block_number)
        if not due:
            return

        logger.debug("Refreshing %d positions at block %d", len(due), block_number)

        # Batch concurrent calls (chunk to avoid rate limits)
        chunk_size = 50
        for i in range(0, len(due), chunk_size):
            chunk = due[i : i + chunk_size]
            tasks = [self._refresh_position(w3, p, block_number) for p in chunk]
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _refresh_position(self, w3: AsyncWeb3, pos: Position, block_number: int):
        try:
            data = await self._pool.functions.getUserAccountData(
                w3.to_checksum_address(pos.address)
            ).call()
        except Exception as exc:
            logger.warning("getUserAccountData failed for %s: %s", pos.address, exc)
            return

        (total_coll_base, total_debt_base, _, _, _, hf_raw) = data

        # If no debt, remove position
        if total_debt_base == 0:
            logger.info("Position %s fully repaid – removing", pos.address)
            self.db.remove_position(pos.address)
            return

        hf     = hf_raw / WAD
        status, interval = _classify(hf)

        updated = Position(
            address=pos.address,
            health_factor=hf,
            total_collateral_usd=total_coll_base / 1e8,
            total_debt_usd=total_debt_base / 1e8,
            collateral_assets=pos.collateral_assets,
            debt_assets=pos.debt_assets,
            last_updated_block=block_number,
            last_updated_ts=int(time.time()),
            status=status,
            check_interval=interval,
        )
        self.db.upsert_position(updated)

        if status == "liquidatable":
            logger.info(
                "LIQUIDATABLE: %s  HF=%.4f  debt=$%.2f",
                pos.address, hf, updated.total_debt_usd,
            )
            await self.q.put(updated)
        elif status == "approaching" and pos.status != "approaching":
            logger.info(
                "APPROACHING: %s  HF=%.4f", pos.address, hf
            )
            await self.alerts.send(
                f"⚡ Position approaching liquidation\n"
                f"User: `{pos.address}`\n"
                f"HF: {hf:.4f}  Debt: ${updated.total_debt_usd:,.0f}"
            )

    # ─── Event handlers ─────────────────────────────────────────────────────
    async def _on_borrow(self, w3: AsyncWeb3, log: dict):
        """Add new borrower or refresh existing one."""
        try:
            # onBehalfOf is topic[2] (indexed)
            topics = log["topics"]
            reserve    = _topic_to_addr(topics[1])
            on_behalf  = _topic_to_addr(topics[2])
        except (IndexError, KeyError):
            return

        borrower = w3.to_checksum_address(on_behalf)
        if not self.db.get_position(borrower):
            # New position – seed with a quick HF check
            pos = Position(
                address=borrower,
                health_factor=999.0,
                total_collateral_usd=0,
                total_debt_usd=0,
                collateral_assets="[]",
                debt_assets=json.dumps([reserve]),
                last_updated_block=0,
                last_updated_ts=0,
                status="safe",
                check_interval=100,
            )
            self.db.upsert_position(pos)
            logger.info("Tracking new borrower: %s", borrower)

    async def _on_repay(self, w3: AsyncWeb3, log: dict):
        """Check if position closed; refresh HF."""
        try:
            topics = log["topics"]
            user = w3.to_checksum_address(_topic_to_addr(topics[2]))
        except (IndexError, KeyError):
            return

        pos = self.db.get_position(user)
        if pos:
            # Mark for immediate refresh
            self.db.upsert_position(
                Position(**{**pos.__dict__, "last_updated_block": 0})
            )

    async def _on_liquidation_call(self, w3: AsyncWeb3, log: dict):
        """Log competition – we lost this one."""
        try:
            topics = log["topics"]
            user = _topic_to_addr(topics[3])
        except (IndexError, KeyError):
            return
        logger.info("Competitor liquidated %s", user)

    # ─── Heartbeat check ────────────────────────────────────────────────────
    @property
    def seconds_since_last_block(self) -> float:
        return time.time() - self._last_block_ts

    @property
    def last_block(self) -> int:
        return self._last_block


# ─── Utility ──────────────────────────────────────────────────────────────────

def _topic_to_addr(topic) -> str:
    """Convert a 32-byte topic to a 20-byte hex address string."""
    if isinstance(topic, bytes):
        hex_val = topic.hex()
    else:
        hex_val = topic
    return "0x" + hex_val[-40:]
