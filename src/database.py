"""
SQLite persistence layer.

Tables:
  positions     – all known borrowers tracked by the monitor
  opportunities – every profitable opportunity found (executed or not)
  executions    – confirmed on-chain liquidations with final P&L
"""

import json
import sqlite3
import logging
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from config import DB_PATH

logger = logging.getLogger(__name__)

# ─── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class Position:
    address: str
    health_factor: float           # current HF (1e18 units decoded)
    total_collateral_usd: float    # in USD (base 8 decimals from Aave)
    total_debt_usd: float
    collateral_assets: str         # JSON list of asset addresses
    debt_assets: str               # JSON list of asset addresses
    last_updated_block: int
    last_updated_ts: int           # unix timestamp
    status: str = "watch"          # liquidatable | approaching | watch | safe
    check_interval: int = 10       # blocks between HF refresh

@dataclass
class Opportunity:
    timestamp: int
    position_address: str
    collateral_asset: str
    debt_asset: str
    debt_amount: float
    estimated_profit_usd: float
    executed: bool = False
    tx_hash: Optional[str] = None
    actual_profit_usd: Optional[float] = None
    failure_reason: Optional[str] = None

@dataclass
class Execution:
    timestamp: int
    tx_hash: str
    position_address: str
    collateral_asset: str
    debt_asset: str
    debt_amount_usd: float
    collateral_received_usd: float
    profit_usd: float
    gas_cost_usd: float
    block_number: int

# ─── Database class ───────────────────────────────────────────────────────────

class Database:
    def __init__(self, path: Path = DB_PATH):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def _conn(self):
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self):
        with self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS positions (
                    address                TEXT PRIMARY KEY,
                    health_factor          REAL NOT NULL,
                    total_collateral_usd   REAL NOT NULL DEFAULT 0,
                    total_debt_usd         REAL NOT NULL DEFAULT 0,
                    collateral_assets      TEXT NOT NULL DEFAULT '[]',
                    debt_assets            TEXT NOT NULL DEFAULT '[]',
                    last_updated_block     INTEGER NOT NULL DEFAULT 0,
                    last_updated_ts        INTEGER NOT NULL DEFAULT 0,
                    status                 TEXT NOT NULL DEFAULT 'watch',
                    check_interval         INTEGER NOT NULL DEFAULT 10
                );

                CREATE TABLE IF NOT EXISTS opportunities (
                    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp              INTEGER NOT NULL,
                    position_address       TEXT NOT NULL,
                    collateral_asset       TEXT NOT NULL,
                    debt_asset             TEXT NOT NULL,
                    debt_amount            REAL NOT NULL,
                    estimated_profit_usd   REAL NOT NULL,
                    executed               INTEGER NOT NULL DEFAULT 0,
                    tx_hash                TEXT,
                    actual_profit_usd      REAL,
                    failure_reason         TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_opp_ts      ON opportunities(timestamp);
                CREATE INDEX IF NOT EXISTS idx_opp_addr    ON opportunities(position_address);

                CREATE TABLE IF NOT EXISTS executions (
                    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp              INTEGER NOT NULL,
                    tx_hash                TEXT NOT NULL UNIQUE,
                    position_address       TEXT NOT NULL,
                    collateral_asset       TEXT NOT NULL,
                    debt_asset             TEXT NOT NULL,
                    debt_amount_usd        REAL NOT NULL,
                    collateral_received_usd REAL NOT NULL,
                    profit_usd             REAL NOT NULL,
                    gas_cost_usd           REAL NOT NULL,
                    block_number           INTEGER NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_exec_ts     ON executions(timestamp);
                CREATE INDEX IF NOT EXISTS idx_exec_profit ON executions(profit_usd);
            """)
        logger.info("Database schema initialised at %s", self.path)

    # ── Positions ──────────────────────────────────────────────────────────────

    def upsert_position(self, p: Position):
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO positions
                    (address, health_factor, total_collateral_usd, total_debt_usd,
                     collateral_assets, debt_assets, last_updated_block,
                     last_updated_ts, status, check_interval)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(address) DO UPDATE SET
                    health_factor        = excluded.health_factor,
                    total_collateral_usd = excluded.total_collateral_usd,
                    total_debt_usd       = excluded.total_debt_usd,
                    collateral_assets    = excluded.collateral_assets,
                    debt_assets          = excluded.debt_assets,
                    last_updated_block   = excluded.last_updated_block,
                    last_updated_ts      = excluded.last_updated_ts,
                    status               = excluded.status,
                    check_interval       = excluded.check_interval
            """, (
                p.address, p.health_factor, p.total_collateral_usd, p.total_debt_usd,
                p.collateral_assets, p.debt_assets, p.last_updated_block,
                p.last_updated_ts, p.status, p.check_interval
            ))

    def get_position(self, address: str) -> Optional[Position]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM positions WHERE address=?", (address,)
            ).fetchone()
        return self._row_to_position(row) if row else None

    def get_positions_due(self, current_block: int) -> list[Position]:
        """Return positions whose check_interval has elapsed."""
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT * FROM positions
                WHERE (? - last_updated_block) >= check_interval
                ORDER BY health_factor ASC
            """, (current_block,)).fetchall()
        return [self._row_to_position(r) for r in rows]

    def get_liquidatable_positions(self) -> list[Position]:
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT * FROM positions WHERE status='liquidatable'
                ORDER BY health_factor ASC
            """).fetchall()
        return [self._row_to_position(r) for r in rows]

    def get_approaching_positions(self) -> list[Position]:
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT * FROM positions WHERE status='approaching'
                ORDER BY health_factor ASC
            """).fetchall()
        return [self._row_to_position(r) for r in rows]

    def remove_position(self, address: str):
        with self._conn() as conn:
            conn.execute("DELETE FROM positions WHERE address=?", (address,))

    def position_count(self) -> dict[str, int]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) as n FROM positions GROUP BY status"
            ).fetchall()
        return {r["status"]: r["n"] for r in rows}

    @staticmethod
    def _row_to_position(row) -> Position:
        return Position(
            address=row["address"],
            health_factor=row["health_factor"],
            total_collateral_usd=row["total_collateral_usd"],
            total_debt_usd=row["total_debt_usd"],
            collateral_assets=row["collateral_assets"],
            debt_assets=row["debt_assets"],
            last_updated_block=row["last_updated_block"],
            last_updated_ts=row["last_updated_ts"],
            status=row["status"],
            check_interval=row["check_interval"],
        )

    # ── Opportunities ──────────────────────────────────────────────────────────

    def record_opportunity(self, opp: Opportunity) -> int:
        with self._conn() as conn:
            cur = conn.execute("""
                INSERT INTO opportunities
                    (timestamp, position_address, collateral_asset, debt_asset,
                     debt_amount, estimated_profit_usd, executed, tx_hash,
                     actual_profit_usd, failure_reason)
                VALUES (?,?,?,?,?,?,?,?,?,?)
            """, (
                opp.timestamp, opp.position_address, opp.collateral_asset,
                opp.debt_asset, opp.debt_amount, opp.estimated_profit_usd,
                int(opp.executed), opp.tx_hash, opp.actual_profit_usd,
                opp.failure_reason
            ))
            return cur.lastrowid

    def mark_opportunity_executed(
        self, opp_id: int, tx_hash: str, actual_profit_usd: float
    ):
        with self._conn() as conn:
            conn.execute("""
                UPDATE opportunities
                SET executed=1, tx_hash=?, actual_profit_usd=?
                WHERE id=?
            """, (tx_hash, actual_profit_usd, opp_id))

    def mark_opportunity_failed(self, opp_id: int, reason: str):
        with self._conn() as conn:
            conn.execute("""
                UPDATE opportunities
                SET executed=0, failure_reason=?
                WHERE id=?
            """, (reason, opp_id))

    # ── Executions ─────────────────────────────────────────────────────────────

    def record_execution(self, ex: Execution):
        with self._conn() as conn:
            conn.execute("""
                INSERT OR IGNORE INTO executions
                    (timestamp, tx_hash, position_address, collateral_asset,
                     debt_asset, debt_amount_usd, collateral_received_usd,
                     profit_usd, gas_cost_usd, block_number)
                VALUES (?,?,?,?,?,?,?,?,?,?)
            """, (
                ex.timestamp, ex.tx_hash, ex.position_address,
                ex.collateral_asset, ex.debt_asset, ex.debt_amount_usd,
                ex.collateral_received_usd, ex.profit_usd,
                ex.gas_cost_usd, ex.block_number
            ))

    # ── Reporting ──────────────────────────────────────────────────────────────

    def daily_pnl(self, date_str: str | None = None) -> dict:
        """Return P&L summary for a UTC calendar day (YYYY-MM-DD, default today)."""
        if date_str is None:
            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        start = int(datetime.strptime(date_str + "T00:00:00Z", "%Y-%m-%dT%H:%M:%SZ").timestamp())
        end   = start + 86400
        with self._conn() as conn:
            row = conn.execute("""
                SELECT
                    COUNT(*)              AS count,
                    COALESCE(SUM(profit_usd),0)   AS total_profit,
                    COALESCE(SUM(gas_cost_usd),0) AS total_gas,
                    COALESCE(MAX(profit_usd),0)   AS best_trade
                FROM executions
                WHERE timestamp >= ? AND timestamp < ?
            """, (start, end)).fetchone()
        return {
            "date":         date_str,
            "liquidations": row["count"],
            "gross_profit": round(row["total_profit"], 4),
            "gas_cost":     round(row["total_gas"],    4),
            "net_profit":   round(row["total_profit"] - row["total_gas"], 4),
            "best_trade":   round(row["best_trade"],   4),
        }

    def all_time_pnl(self) -> dict:
        with self._conn() as conn:
            row = conn.execute("""
                SELECT
                    COUNT(*)              AS count,
                    COALESCE(SUM(profit_usd),0)   AS total_profit,
                    COALESCE(SUM(gas_cost_usd),0) AS total_gas
                FROM executions
            """).fetchone()
        return {
            "liquidations": row["count"],
            "gross_profit": round(row["total_profit"], 4),
            "gas_cost":     round(row["total_gas"],    4),
            "net_profit":   round(row["total_profit"] - row["total_gas"], 4),
        }
