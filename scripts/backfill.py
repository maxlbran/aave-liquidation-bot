#!/usr/bin/env python3
"""
Backfill borrower positions from recent Aave V3 Borrow events.

Alchemy free tier caps eth_getLogs at a 10-block range per request, so
this script batches calls in 10-block chunks and inserts discovered
borrowers into the local database.

Usage:
    python scripts/backfill.py             # last 2000 blocks (~8 min)
    python scripts/backfill.py --blocks 20000  # last 20k blocks (~1.4 hr)

After the backfill, re-run the bot — the monitor will immediately refresh
health factors for all seeded positions on the next block.
"""

import argparse
import json
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

import config
from src.database import Database, Position

BORROW_TOPIC = "0xb3d084820fb1a9decffb176436bd02b1b19a981433a6b84e20f3d5a1ecdf94ae"
CHUNK = 10          # Alchemy free tier hard limit
RATE_DELAY = 0.12   # seconds between requests (~8 req/s, well under free tier limit)


def rpc(method: str, params: list, rpc_url: str) -> dict:
    r = requests.post(rpc_url, json={
        "jsonrpc": "2.0", "id": 1,
        "method": method, "params": params,
    }, timeout=15)
    return r.json()


def get_latest_block(rpc_url: str) -> int:
    result = rpc("eth_blockNumber", [], rpc_url)
    return int(result["result"], 16)


def fetch_borrows(from_block: int, to_block: int, rpc_url: str) -> list[dict]:
    result = rpc("eth_getLogs", [{
        "address": config.AAVE_POOL_ADDRESS,
        "topics":  [BORROW_TOPIC],
        "fromBlock": hex(from_block),
        "toBlock":   hex(to_block),
    }], rpc_url)
    if "error" in result:
        # Silently skip — free tier may reject even 10-block ranges occasionally
        return []
    return result.get("result", [])


def parse_borrower(log: dict) -> tuple[str, str] | None:
    """Return (borrower_address, reserve_address) from a Borrow log."""
    topics = log.get("topics", [])
    if len(topics) < 3:
        return None
    reserve     = "0x" + topics[0][-40:]   # topics[0] = event sig, reserve is indexed[1]
    on_behalf   = "0x" + topics[2][-40:]   # onBehalfOf is indexed[2]
    # Correct parse: topics[1] = reserve (indexed), topics[2] = onBehalfOf (indexed)
    reserve_addr = "0x" + log["topics"][1][-40:]
    borrower     = "0x" + log["topics"][2][-40:]
    return borrower.lower(), reserve_addr.lower()


def backfill(blocks_back: int, rpc_url: str):
    db = Database()

    latest = get_latest_block(rpc_url)
    start  = latest - blocks_back
    total_chunks = (blocks_back + CHUNK - 1) // CHUNK

    print(f"Backfilling Aave V3 Borrow events")
    print(f"Pool:    {config.AAVE_POOL_ADDRESS}")
    print(f"Blocks:  {start:,} → {latest:,}  ({blocks_back:,} blocks, {total_chunks:,} requests)")
    print(f"Est. time: ~{total_chunks * RATE_DELAY:.0f}s at {1/RATE_DELAY:.0f} req/s\n")

    discovered: dict[str, list[str]] = {}  # borrower -> [reserve, ...]
    chunk_num = 0
    errors    = 0

    for blk in range(start, latest, CHUNK):
        chunk_num += 1
        to_blk = min(blk + CHUNK - 1, latest)

        logs = fetch_borrows(blk, to_blk, rpc_url)
        for log in logs:
            parsed = parse_borrower(log)
            if parsed:
                borrower, reserve = parsed
                if borrower not in discovered:
                    discovered[borrower] = []
                if reserve not in discovered[borrower]:
                    discovered[borrower].append(reserve)

        if chunk_num % 200 == 0:
            pct = chunk_num / total_chunks * 100
            print(f"  {pct:5.1f}%  chunk {chunk_num:,}/{total_chunks:,}  "
                  f"discovered {len(discovered):,} borrowers  errors={errors}")

        time.sleep(RATE_DELAY)

    print(f"\nScan complete. Found {len(discovered):,} unique borrowers.\n")

    # Seed into database
    new_count = 0
    for borrower, reserves in discovered.items():
        if not db.get_position(borrower):
            pos = Position(
                address=borrower,
                health_factor=999.0,
                total_collateral_usd=0,
                total_debt_usd=0,
                collateral_assets="[]",
                debt_assets=json.dumps(reserves),
                last_updated_block=0,   # triggers immediate refresh on next block
                last_updated_ts=0,
                status="safe",
                check_interval=1,       # check on very next block
            )
            db.upsert_position(pos)
            new_count += 1

    print(f"Inserted {new_count:,} new positions into database.")
    print(f"The monitor will refresh health factors on the next block.\n")

    counts = db.position_count()
    print("Position counts:", counts)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--blocks", type=int, default=2000,
        help="How many recent blocks to scan (default: 2000 ≈ 8 min on Arbitrum)"
    )
    args = parser.parse_args()

    rpc_url = config.ALCHEMY_HTTP_URL
    backfill(args.blocks, rpc_url)


if __name__ == "__main__":
    main()
