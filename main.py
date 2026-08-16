"""
Aave V3 Liquidation Bot — main entry point.

Runs three concurrent async tasks:
  1. Monitor  – WebSocket event stream + health factor polling
  2. Executor – consumes liquidatable positions, executes flash loan txs
  3. Heartbeat – watchdog that alerts if blocks stop arriving

Usage:
    python main.py

Signals:
    SIGINT / SIGTERM  →  graceful shutdown
"""

import asyncio
import logging
import signal
import sys
import time
from pathlib import Path

from web3 import AsyncWeb3

import config
from src.database import Database
from src.alerts import Alerts, schedule_daily_summary
from src.monitor import Monitor
from src.executor import Executor

# ─── Logging setup ────────────────────────────────────────────────────────────
log_dir = Path(config.LOG_FILE).parent
log_dir.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(config.LOG_FILE),
    ],
)
logger = logging.getLogger("main")


# ─── Heartbeat watchdog ───────────────────────────────────────────────────────
async def heartbeat_watchdog(monitor: Monitor, alerts: Alerts):
    """Alert if no block has been seen for HEARTBEAT_INTERVAL_SECONDS."""
    while True:
        await asyncio.sleep(60)  # check every minute
        gap = monitor.seconds_since_last_block
        if gap > config.HEARTBEAT_INTERVAL_SECONDS:
            logger.warning("Heartbeat failure – no block in %.0fs", gap)
            await alerts.send_heartbeat_failure(gap)


# ─── Status reporter ─────────────────────────────────────────────────────────
async def status_reporter(monitor: Monitor, alerts: Alerts, db: Database):
    """Log and send a status update every 30 minutes."""
    while True:
        await asyncio.sleep(1800)
        counts = db.position_count()
        logger.info("Status: %s  last_block=%d", counts, monitor.last_block)
        await alerts.send_status(counts, monitor.last_block)


# ─── Main ─────────────────────────────────────────────────────────────────────
async def main():
    logger.info("=" * 60)
    logger.info("Aave V3 Liquidation Bot starting")
    logger.info("Chain: %d  Pool: %s", config.CHAIN_ID, config.AAVE_POOL_ADDRESS)
    logger.info("Bot contract: %s", config.BOT_CONTRACT_ADDRESS or "NOT SET")
    logger.info("=" * 60)

    if not config.BOT_CONTRACT_ADDRESS:
        logger.error("BOT_CONTRACT_ADDRESS is not set. Deploy the contract first.")
        sys.exit(1)

    # Shared components
    db      = Database()
    alerts  = Alerts()
    opp_q   = asyncio.Queue(maxsize=100)

    await alerts.start()

    # Build async web3 for executor (HTTP, stateless)
    w3_http = AsyncWeb3(AsyncWeb3.AsyncHTTPProvider(config.ALCHEMY_HTTP_URL))

    monitor  = Monitor(db=db, alerts=alerts, opportunity_queue=opp_q)
    executor = Executor(w3=w3_http, db=db, alerts=alerts, opportunity_queue=opp_q)

    # Graceful shutdown
    loop        = asyncio.get_running_loop()
    stop_event  = asyncio.Event()

    def _shutdown(signum, frame):
        logger.info("Received signal %s – shutting down…", signum)
        loop.call_soon_threadsafe(stop_event.set)

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # Launch all tasks
    tasks = [
        asyncio.create_task(monitor.run(),                   name="monitor"),
        asyncio.create_task(executor.run(),                  name="executor"),
        asyncio.create_task(heartbeat_watchdog(monitor, alerts), name="heartbeat"),
        asyncio.create_task(status_reporter(monitor, alerts, db), name="status"),
        asyncio.create_task(schedule_daily_summary(alerts, db),   name="daily_summary"),
    ]

    logger.info("All tasks started")

    # Wait for shutdown signal or a task to crash
    done, pending = await asyncio.wait(
        [asyncio.create_task(stop_event.wait())] + tasks,
        return_when=asyncio.FIRST_COMPLETED,
    )

    # Check if any task raised an exception (unexpected crash)
    for t in done:
        if t.get_name() != "Task-1" and not t.cancelled():
            exc = t.exception()
            if exc:
                logger.error("Task %s crashed: %s", t.get_name(), exc, exc_info=exc)

    # Cancel remaining
    for t in pending:
        t.cancel()
    await asyncio.gather(*pending, return_exceptions=True)

    await alerts.send("🛑 Aave Liquidation Bot stopped")
    await alerts.stop()
    logger.info("Bot stopped cleanly")


if __name__ == "__main__":
    asyncio.run(main())
