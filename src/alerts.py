"""
Telegram alert system.

Sends notifications for:
  • Successful liquidations
  • Failed executions
  • Positions approaching liquidation
  • Daily P&L summary
  • Heartbeat failures (bot stopped receiving blocks)
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import aiohttp

import config

if TYPE_CHECKING:
    from src.calculator import LiquidationOpportunity

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


class Alerts:
    def __init__(self):
        self._enabled = config.TELEGRAM_ENABLED
        self._token   = config.TELEGRAM_BOT_TOKEN
        self._chat_id = config.TELEGRAM_CHAT_ID
        self._session: aiohttp.ClientSession | None = None

    async def start(self):
        self._session = aiohttp.ClientSession()
        if self._enabled:
            await self.send("🤖 Aave Liquidation Bot started")

    async def stop(self):
        if self._session:
            await self._session.close()

    # ── Send helpers ──────────────────────────────────────────────────────────

    async def send(self, text: str):
        if not self._enabled:
            logger.info("[ALERT] %s", text)
            return
        url = TELEGRAM_API.format(token=self._token)
        payload = {
            "chat_id":    self._chat_id,
            "text":       text,
            "parse_mode": "Markdown",
        }
        try:
            async with self._session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning("Telegram error %d: %s", resp.status, body)
        except Exception as exc:
            logger.warning("Telegram send failed: %s", exc)

    async def send_successful_liquidation(
        self,
        opp: "LiquidationOpportunity",
        actual_profit_usd: float,
        tx_hash: str,
    ):
        msg = (
            f"✅ *Liquidation Executed*\n"
            f"User: `{opp.position_address}`\n"
            f"Collateral: `{opp.collateral_asset[-8:]}`\n"
            f"Debt: `{opp.debt_asset[-8:]}`\n"
            f"Debt covered: ${opp.debt_amount_usd:,.2f}\n"
            f"Est. profit: ${opp.net_profit_usd:,.2f}\n"
            f"Actual profit: *${actual_profit_usd:,.2f}*\n"
            f"Tx: `{tx_hash}`"
        )
        await self.send(msg)

    async def send_failed_execution(
        self,
        opp: "LiquidationOpportunity",
        reason: str,
    ):
        msg = (
            f"❌ *Execution Failed*\n"
            f"User: `{opp.position_address}`\n"
            f"Est. profit: ${opp.net_profit_usd:,.2f}\n"
            f"Reason: `{reason[:200]}`"
        )
        await self.send(msg)

    async def send_daily_summary(self, pnl: dict):
        msg = (
            f"📊 *Daily P&L — {pnl['date']}*\n"
            f"Liquidations: {pnl['liquidations']}\n"
            f"Gross profit: ${pnl['gross_profit']:,.2f}\n"
            f"Gas cost:     ${pnl['gas_cost']:,.2f}\n"
            f"*Net profit:  ${pnl['net_profit']:,.2f}*\n"
            f"Best trade:   ${pnl['best_trade']:,.2f}"
        )
        await self.send(msg)

    async def send_heartbeat_failure(self, seconds: float):
        msg = (
            f"🔴 *Heartbeat Alert*\n"
            f"No blocks received in {seconds:.0f}s\n"
            f"Bot may be disconnected — check immediately!"
        )
        await self.send(msg)

    async def send_status(self, position_counts: dict, last_block: int):
        counts_str = "  ".join(f"{k}: {v}" for k, v in position_counts.items())
        msg = (
            f"💓 *Bot Status*\n"
            f"Last block: {last_block:,}\n"
            f"Positions: {counts_str}"
        )
        await self.send(msg)


# ─── Daily summary scheduler ─────────────────────────────────────────────────

async def schedule_daily_summary(alerts: Alerts, db):
    """Run forever, sending a P&L summary at 00:00 UTC."""
    while True:
        now = datetime.now(timezone.utc)
        # Seconds until next midnight UTC
        seconds_to_midnight = (
            86400
            - now.hour * 3600
            - now.minute * 60
            - now.second
        )
        await asyncio.sleep(seconds_to_midnight)

        yesterday = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        pnl = db.daily_pnl(yesterday)
        await alerts.send_daily_summary(pnl)
