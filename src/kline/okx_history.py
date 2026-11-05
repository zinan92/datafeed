"""OKX history for the pre-start backtest: 1-minute bars, funding and a coverage report.

Bars go to the same store ``/api/candles`` serves ``okx_swap_public`` from, under that
source only. Nothing is filled, interpolated or borrowed from another venue: what OKX did
not give is listed in the report, and the reader decides what to do about it.

The work is one UTC day at a time, each committed on its own. A day that is already
complete is skipped, so a killed run resumes where it stopped and a daily run only fetches
the newest bars.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from typing import Any, Awaitable, Callable, TypeVar

from kline.models import AssetClass, Timeframe
from kline.providers.base import ProviderError
from kline.providers.okx_swap import SOURCE_MODE, OkxSwapProvider, confirmed_candles
from kline.store import KlineStore

SCHEMA = "okx-history-report-v1"
INSTRUMENT = "XAU-USDT-SWAP"
DEFAULT_DAYS = 180
ATTEMPTS = 5
MINUTE = timedelta(minutes=1)
DAY = timedelta(days=1)

T = TypeVar("T")
Sleep = Callable[[float], Awaitable[None]]


def report_path(db_path: str | Path, instrument: str = INSTRUMENT) -> Path:
    return Path(db_path).parent / "history_reports" / f"{SOURCE_MODE}_{instrument}.json"


def _ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def _parse(stamp: str) -> datetime:
    moment = datetime.fromisoformat(stamp)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def day_windows(start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    """Whole-minute [start, end) cut at UTC midnight, newest first."""
    windows = []
    cursor = start
    while cursor < end:
        midnight = cursor.replace(hour=0, minute=0, second=0, microsecond=0) + DAY
        windows.append((cursor, min(midnight, end)))
        cursor = midnight
    return windows[::-1]


async def _retry(call: Callable[[], Awaitable[T]], sleep: Sleep) -> T:
    """Wait and retry a refused or failed page; the last error goes to the caller."""
    for attempt in range(ATTEMPTS):
        try:
            return await call()
        except ProviderError:
            if attempt == ATTEMPTS - 1:
                raise
            await sleep(2.0**attempt)
    raise AssertionError("unreachable")


async def _fetch_window(
    provider: OkxSwapProvider, store: KlineStore, start: datetime, end: datetime, instrument: str, sleep: Sleep
) -> int:
    rows: dict[int, list[Any]] = {}
    cursor = _ms(end)
    while cursor > _ms(start):
        page = await _retry(lambda: provider.history_page(instrument, Timeframe.MIN_1, after_ms=cursor), sleep)
        if not page:
            break
        for row in page:
            if _ms(start) <= int(row[0]) < _ms(end):
                rows[int(row[0])] = row
        oldest = min(int(row[0]) for row in page)
        if oldest >= cursor:
            break  # OKX did not move backwards; stop instead of looping.
        cursor = oldest
    candles = confirmed_candles([rows[key] for key in sorted(rows)], Timeframe.MIN_1)
    store.save(instrument, AssetClass.COMMODITY, Timeframe.MIN_1, candles, source_id=SOURCE_MODE)
    return len(candles)


async def _fetch_funding(provider: OkxSwapProvider, store: KlineStore, instrument: str, sleep: Sleep) -> int:
    cursor: int | None = None
    saved = 0
    while True:
        page = await _retry(lambda: provider.funding_page(instrument, after_ms=cursor), sleep)
        if not page:
            return saved
        saved += store.save_funding(
            SOURCE_MODE,
            instrument,
            [
                {
                    "funding_time": _iso(datetime.fromtimestamp(int(row["fundingTime"]) / 1000, timezone.utc)),
                    "funding_rate": float(row["fundingRate"]),
                    "realized_rate": float(row["realizedRate"]) if row.get("realizedRate") not in (None, "") else None,
                    "raw": json.dumps(row, sort_keys=True),
                }
                for row in page
            ],
        )
        oldest = min(int(row["fundingTime"]) for row in page)
        if cursor is not None and oldest >= cursor:
            return saved
        cursor = oldest


def _missing_runs(present: set[datetime], start: datetime, end: datetime, cause: str) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    cursor = start
    while cursor < end:
        if cursor not in present:
            if runs and runs[-1]["_next"] == cursor:
                runs[-1].update(end=_iso(cursor), minutes=runs[-1]["minutes"] + 1, _next=cursor + MINUTE)
            else:
                runs.append({"start": _iso(cursor), "end": _iso(cursor), "minutes": 1, "cause": cause, "_next": cursor + MINUTE})
        cursor += MINUTE
    return runs


def _merge(gaps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for gap in gaps:
        if merged and merged[-1]["_next"] == _parse(gap["start"]) and merged[-1]["cause"] == gap["cause"]:
            merged[-1].update(end=gap["end"], minutes=merged[-1]["minutes"] + gap["minutes"], _next=gap["_next"])
        else:
            merged.append(dict(gap))
    return [{key: value for key, value in gap.items() if key != "_next"} for gap in merged]


def build_report(
    store: KlineStore,
    *,
    instrument: str,
    start: datetime,
    end: datetime,
    now: datetime,
    failures: dict[str, str],
    funding_error: str | None,
) -> dict[str, Any]:
    stamps = store.candle_timestamps(
        instrument, AssetClass.COMMODITY, Timeframe.MIN_1, source_id=SOURCE_MODE, start=_iso(start), end=_iso(end)
    )
    present = {_parse(stamp) for stamp in stamps}
    gaps: list[dict[str, Any]] = []
    for window_start, window_end in day_windows(start, end)[::-1]:
        cause = "fetch_failed" if _iso(window_start) in failures else "venue_no_bar"
        gaps.extend(_missing_runs(present, window_start, window_end, cause))
    funding = store.query_funding(SOURCE_MODE, instrument)
    return {
        "schema_version": SCHEMA,
        "source": SOURCE_MODE,
        "instrument": instrument,
        "timeframe": Timeframe.MIN_1.value,
        "generated_at": _iso(now),
        "requested_start": _iso(start),
        "requested_end": _iso(end),
        "expected_bars": int((end - start) / MINUTE),
        "bar_count": len(present),
        "first_bar": _iso(min(present)) if present else None,
        "last_bar": _iso(max(present)) if present else None,
        "gaps": _merge(gaps),
        "missing_minutes": sum(gap["minutes"] for gap in gaps),
        "fetch_failures": failures,
        "funding": {
            "count": len(funding),
            "first": funding[0]["funding_time"] if funding else None,
            "last": funding[-1]["funding_time"] if funding else None,
            "error": funding_error,
        },
    }


def write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    os.replace(temporary, path)


async def backfill(
    provider: OkxSwapProvider,
    store: KlineStore,
    *,
    path: Path,
    instrument: str = INSTRUMENT,
    days: int = DEFAULT_DAYS,
    now: datetime | None = None,
    sleep: Sleep = asyncio.sleep,
) -> dict[str, Any]:
    """Fetch what is missing, then write the coverage report. Returns the report."""
    moment = now or datetime.now(timezone.utc)
    end = moment.replace(second=0, microsecond=0)  # the forming minute is not history
    start = end - timedelta(days=days)
    failures: dict[str, str] = {}
    for window_start, window_end in day_windows(start, end):
        have = store.candle_timestamps(
            instrument, AssetClass.COMMODITY, Timeframe.MIN_1, source_id=SOURCE_MODE,
            start=_iso(window_start), end=_iso(window_end),
        )
        if len(have) >= int((window_end - window_start) / MINUTE):
            continue
        try:
            await _fetch_window(provider, store, window_start, window_end, instrument, sleep)
        except ProviderError as error:
            failures[_iso(window_start)] = str(error)
    funding_error = None
    try:
        await _fetch_funding(provider, store, instrument, sleep)
    except ProviderError as error:
        funding_error = str(error)
    report = build_report(
        store, instrument=instrument, start=start, end=end, now=moment, failures=failures, funding_error=funding_error
    )
    write_report(path, report)
    return report
