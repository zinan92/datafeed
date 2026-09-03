"""One-shot history backfill for Watchlist series eligible for consumer cutover.

This runner deliberately targets only the canonical Market Data Database and
reuses the Watchlist worker lock.  It does not change either scheduler and it
does not touch the Screening universe.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Sequence

from kline.config import Settings
from kline.ingestion import IngestionOrchestrator, IngestionPlan
from kline.mvp_worker import _SingleRunLock
from kline.registry import init
from kline.store import KlineStore
from kline.watchlist_manifest import load_watchlist_manifest
from ops.watchlist_seed import (
    DEFAULT_MANIFEST,
    MARKET_DATA_DB,
    WATCHLIST_LOCK,
    validate_watchlist_target,
)


CONSUMER_DAILY_INSTRUMENT_IDS = (
    "WATCH.CROSS.DXY",
    "WATCH.CROSS.SPX",
    "WATCH.CROSS.NDX",
    "WATCH.CROSS.SCHD",
    "WATCH.CROSS.BTC",
    "WATCH.CROSS.ETH",
    "WATCH.CROSS.HYPE",
    "WATCH.CROSS.WTI",
    "WATCH.CROSS.GOLD",
    "WATCH.CROSS.SILVER",
)
DEFAULT_HISTORY_START = "2021-01-01T00:00:00+00:00"
DEFAULT_RECEIPT = Path("/Users/wendy/park-data/market/consumer-cutover-backfill.json")


async def run_consumer_cutover_backfill(
    *,
    manifest_path: str | Path = DEFAULT_MANIFEST,
    db_path: str | Path = MARKET_DATA_DB,
    lock_path: str | Path = WATCHLIST_LOCK,
    history_start: str = DEFAULT_HISTORY_START,
    fetch_limit: int = 2000,
    request_interval_seconds: float = 2.0,
    max_retries: int = 2,
    retry_backoff_seconds: float = 2.0,
    provider_timeout_seconds: float = 45.0,
) -> dict[str, Any]:
    database, lock_file = validate_watchlist_target(db_path, lock_path)
    manifest = load_watchlist_manifest(manifest_path)
    manifest_ids = {item.instrument_id for item in manifest.instruments}
    missing = sorted(set(CONSUMER_DAILY_INSTRUMENT_IDS) - manifest_ids)
    if missing:
        raise ValueError(f"consumer backfill identities missing from Watchlist: {missing}")
    if fetch_limit < 1008:
        raise ValueError("consumer backfill fetch_limit must cover Human Review's 1008-row request")

    init(Settings(db_path=str(database), load_entrypoint_adapters=False))
    store = KlineStore(str(database))
    now = datetime.now(timezone.utc)
    with _SingleRunLock(lock_file):
        receipt = await IngestionOrchestrator(store).run_once(
            IngestionPlan(
                manifest=manifest,
                run_id=f"consumer-cutover-backfill-{now.strftime('%Y%m%dT%H%M%SZ')}",
                now=now,
                history_start=history_start,
                force_history_start=True,
                fetch_limit=fetch_limit,
                max_retries=max_retries,
                retry_backoff_seconds=retry_backoff_seconds,
                instrument_ids=CONSUMER_DAILY_INSTRUMENT_IDS,
                timeframes=("1d",),
                request_interval_seconds=request_interval_seconds,
                provider_timeout_seconds=provider_timeout_seconds,
                policy={
                    "runner": "consumer_cutover_backfill",
                    "universe": "watchlist",
                    "timeframes": ["1d"],
                    "history_start": history_start,
                    "force_history_start": True,
                    "fetch_limit": fetch_limit,
                },
            )
        )
    payload = receipt.to_dict()
    payload["eligible_instrument_ids"] = list(CONSUMER_DAILY_INSTRUMENT_IDS)
    payload["database_path"] = "canonical_market_data_database"
    payload["lock_path"] = "canonical_watchlist_worker_lock"
    return payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Backfill consumer-eligible Watchlist daily history"
    )
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--db", default=str(MARKET_DATA_DB))
    parser.add_argument("--lock", default=str(WATCHLIST_LOCK))
    parser.add_argument("--history-start", default=DEFAULT_HISTORY_START)
    parser.add_argument("--fetch-limit", type=int, default=2000)
    parser.add_argument("--request-interval", type=float, default=2.0)
    parser.add_argument("--max-retries", type=int, default=2)
    parser.add_argument("--retry-backoff", type=float, default=2.0)
    parser.add_argument("--provider-timeout", type=float, default=45.0)
    parser.add_argument("--receipt", default=str(DEFAULT_RECEIPT))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    report = asyncio.run(
        run_consumer_cutover_backfill(
            manifest_path=args.manifest,
            db_path=args.db,
            lock_path=args.lock,
            history_start=args.history_start,
            fetch_limit=args.fetch_limit,
            request_interval_seconds=args.request_interval,
            max_retries=args.max_retries,
            retry_backoff_seconds=args.retry_backoff,
            provider_timeout_seconds=args.provider_timeout,
        )
    )
    rendered = json.dumps(report, ensure_ascii=False, sort_keys=True)
    print(rendered)
    receipt_path = Path(args.receipt).expanduser().resolve()
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_text(rendered + "\n", encoding="utf-8")
    return 0 if report["status"] == "success" else 2


if __name__ == "__main__":
    raise SystemExit(main())
