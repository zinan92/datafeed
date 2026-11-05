"""Fetch OKX history (gold, BTC: ``okx_history.SERIES``) into the candle store and write each coverage report.

Safe to rerun: complete days are skipped. One series failing does not stop the others; the exit code says so. Writes to the store that ``--db`` names and
never to the execution-market database.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from kline.mvp_worker import _SingleRunLock
from kline.okx_history import DEFAULT_DAYS, SERIES, backfill, report_path
from kline.providers.okx_swap import OkxSwapProvider
from kline.store import KlineStore


async def _run(db: str, days: int, instruments: list[str]) -> list[dict]:
    provider = OkxSwapProvider(timeout=20.0, allowed_symbols=set(SERIES))
    store = KlineStore(db)
    try:
        return [await backfill(provider, store, path=report_path(db, instrument), instrument=instrument, days=days) for instrument in instruments]
    finally:
        await provider.aclose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--lock", default=None)
    parser.add_argument("--instrument", action="append", choices=sorted(SERIES), help="repeatable; default: every series")
    args = parser.parse_args()
    if Path(args.db).name.startswith("execution_market"):
        parser.error("OKX history belongs in the candle store, not the execution-market database")
    lock = args.lock or str(Path(args.db).parent / "okx-history.lock")
    with _SingleRunLock(lock):
        reports = asyncio.run(_run(args.db, args.days, args.instrument or list(SERIES)))
    for report in reports:
        summary = {key: report[key] for key in ("source", "instrument", "bar_count", "expected_bars", "missing_minutes", "first_bar", "last_bar", "funding")}
        print(json.dumps(summary, ensure_ascii=False))
    return 1 if any(report["fetch_failures"] or report["funding"]["error"] for report in reports) else 0


if __name__ == "__main__":
    raise SystemExit(main())
