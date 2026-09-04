"""One-shot post-close catch-up for Yahoo continuous-futures daily bars."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Sequence

from ops.watchlist_seed import (
    DEFAULT_MANIFEST,
    MARKET_DATA_DB,
    WATCHLIST_LOCK,
    run_watchlist_seed_once,
)


COMMODITY_INSTRUMENT_IDS = (
    "WATCH.CROSS.GOLD",
    "WATCH.CROSS.SILVER",
    "WATCH.CROSS.WTI",
)
DEFAULT_RECEIPT = Path("/Users/wendy/park-data/market/watchlist-commodity-catchup.json")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Catch up Gold, Silver and WTI daily bars")
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--db", default=str(MARKET_DATA_DB))
    parser.add_argument("--lock", default=str(WATCHLIST_LOCK))
    parser.add_argument("--receipt", default=str(DEFAULT_RECEIPT))
    parser.add_argument("--request-interval", type=float, default=2.0)
    parser.add_argument("--max-retries", type=int, default=2)
    parser.add_argument("--retry-backoff", type=float, default=5.0)
    parser.add_argument("--provider-timeout", type=float, default=45.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    report = asyncio.run(
        run_watchlist_seed_once(
            manifest_path=args.manifest,
            db_path=args.db,
            lock_path=args.lock,
            request_interval_seconds=args.request_interval,
            max_retries=args.max_retries,
            retry_backoff_seconds=args.retry_backoff,
            provider_timeout_seconds=args.provider_timeout,
            instrument_ids=COMMODITY_INSTRUMENT_IDS,
        )
    )
    rendered = json.dumps(report, ensure_ascii=False, sort_keys=True)
    print(rendered)
    receipt = Path(args.receipt).expanduser().resolve()
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(rendered + "\n", encoding="utf-8")
    return 0 if report["status"] == "success" else 2


if __name__ == "__main__":
    raise SystemExit(main())
