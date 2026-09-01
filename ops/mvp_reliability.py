"""Run and audit the bounded 3+3 MVP reliability window.

The worker path is intentionally the same atomic ``MvpWorker`` used by the
application.  This module only selects the approved pilot identities, records
terminal receipts, and evaluates a seven-day gate; it never turns missing
provider rights into successful data.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import signal
from typing import Any, Mapping, Sequence

from kline.config import Settings
from kline.health_matrix import (
    MVP_DEMO_INSTRUMENT_IDS,
    _safe_run,
    build_mvp_health_matrix,
)
from kline.mvp_manifest import MvpManifest, load_manifest, manifest_digest
from kline.mvp_worker import MAX_INTERVAL_SECONDS, MvpWorker
from kline.registry import init
from kline.store import KlineStore


DEMO_INSTRUMENT_IDS = MVP_DEMO_INSTRUMENT_IDS
RELIABILITY_DAYS = 7
MAX_SILENT_HOURS = 8
TERMINAL_STATUSES = {"success", "partial", "failed"}


def _parse_time(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _parse_time(value).replace(microsecond=0).isoformat()


def demo_manifest(manifest: MvpManifest) -> MvpManifest:
    """Return a manifest view containing exactly the approved 3+3 identities."""

    by_id = {item.instrument_id: item for item in manifest.instruments}
    missing = [instrument_id for instrument_id in DEMO_INSTRUMENT_IDS if instrument_id not in by_id]
    if missing:
        raise ValueError(f"reliability identities missing from manifest: {', '.join(missing)}")
    return replace(
        manifest,
        instruments=tuple(by_id[instrument_id] for instrument_id in DEMO_INSTRUMENT_IDS),
    )


async def run_demo_once(
    *,
    db_path: str | Path,
    manifest_path: str | Path,
    now: datetime | None = None,
    interval_seconds: int = MAX_INTERVAL_SECONDS,
    lock_path: str | Path | None = None,
) -> dict[str, Any]:
    """Execute one real 3+3 worker attempt and return its read-only snapshot."""

    observed_at = _parse_time(now or datetime.now(timezone.utc))
    manifest = load_manifest(manifest_path)
    demo_manifest(manifest)
    database = Path(db_path).expanduser()
    init(Settings(db_path=str(database), load_entrypoint_adapters=False))
    store = KlineStore(str(database))
    worker = MvpWorker(
        manifest,
        store,
        interval_seconds=interval_seconds,
        lock_path=lock_path or database.with_suffix(".worker.lock"),
        instrument_ids=DEMO_INSTRUMENT_IDS,
        clock=lambda: observed_at,
    )
    result = await worker.run_once()
    health = build_mvp_health_matrix(
        manifest,
        store,
        scope="demo_3x3",
        now=observed_at,
    )
    return {
        "observed_at": _iso(observed_at),
        "manifest_version": manifest.version,
        "manifest_hash": manifest_digest(manifest),
        "status": result.status,
        "run_id": result.run_id,
        "reason": result.reason,
        "health": health,
        "receipt": result.receipt.to_dict() if result.receipt is not None else None,
    }


def _run_times(store: KlineStore, start: datetime, end: datetime) -> list[dict[str, Any]]:
    rows = store.latest_mvp_runs(limit=1000) if hasattr(store, "latest_mvp_runs") else []
    result: list[dict[str, Any]] = []
    for row in rows:
        stamp = row.get("started_at")
        if not stamp:
            continue
        try:
            parsed = _parse_time(stamp)
        except (TypeError, ValueError):
            continue
        if start <= parsed <= end:
            result.append(_safe_run(row))
    result.sort(key=lambda row: _parse_time(row["started_at"]), reverse=True)
    return result


def _terminal_gate(
    runs: Sequence[Mapping[str, Any]], *, start: datetime, end: datetime, interval_seconds: int
) -> dict[str, Any]:
    duration = end - start
    planned = max(1, int(duration.total_seconds() // interval_seconds) + 1)
    terminal = sum(str(row.get("status")) in TERMINAL_STATUSES for row in runs)
    rate = terminal / planned
    return {
        "status": "ready" if rate >= 0.95 else "failed",
        "planned_opportunities": planned,
        "terminal_receipts": terminal,
        "rate": round(rate, 4),
    }


def _silence_gate(
    runs: Sequence[Mapping[str, Any]], *, start: datetime, end: datetime
) -> dict[str, Any]:
    times = sorted(
        _parse_time(row.get("completed_at") or row["started_at"])
        for row in runs
        if row.get("completed_at") or row.get("started_at")
    )
    gaps = [(later - earlier).total_seconds() / 3600 for earlier, later in zip(times, times[1:])]
    if times:
        gaps.extend(
            [
                max(0.0, (times[0] - start).total_seconds() / 3600),
                max(0.0, (end - times[-1]).total_seconds() / 3600),
            ]
        )
    else:
        gaps = [(end - start).total_seconds() / 3600]
    maximum = max(gaps, default=0.0)
    return {
        "status": "ready" if maximum <= MAX_SILENT_HOURS else "failed",
        "max_silent_hours": round(maximum, 3),
        "run_count": len(times),
    }


def audit_reliability(
    manifest: MvpManifest,
    store: KlineStore,
    *,
    window_start: datetime,
    window_end: datetime,
    interval_seconds: int = MAX_INTERVAL_SECONDS,
) -> dict[str, Any]:
    """Evaluate the seven-day gate from persisted facts only."""

    start = _parse_time(window_start)
    end = _parse_time(window_end)
    runs = _run_times(store, start, end)
    terminal = _terminal_gate(runs, start=start, end=end, interval_seconds=interval_seconds)
    silence = _silence_gate(runs, start=start, end=end)
    span_days = (end - start).total_seconds() / 86_400
    seven_days = {
        "status": "ready" if span_days >= RELIABILITY_DAYS else "blocked",
        "observed_days": round(span_days, 4),
        "required_days": RELIABILITY_DAYS,
    }
    health = build_mvp_health_matrix(
        manifest,
        store,
        scope="demo_3x3",
        now=end,
    )
    applicable = [cell for cell in health["cells"] if cell["applicability"] == "applicable"]
    invalid_cells = [
        cell
        for cell in applicable
        if cell["status"] not in {"blocked", "unavailable"}
        and not cell.get("latest_closed_timestamp")
    ]
    cell_gate = {
        "status": "ready" if not invalid_cells else "failed",
        "applicable_cells": len(applicable),
        "evidenced_cells": len(applicable) - len(invalid_cells),
        "invalid_cells": [
            {
                "instrument_id": cell["instrument_id"],
                "timeframe": cell["timeframe"],
                "status": cell["status"],
            }
            for cell in invalid_cells
        ],
    }
    storage_health = store.mvp_storage_health()
    duplicate_count = (
        store.mvp_duplicate_key_count() if hasattr(store, "mvp_duplicate_key_count") else None
    )
    duplicate_gate = {
        "status": "ready" if duplicate_count == 0 else "blocked",
        "duplicate_keys": duplicate_count,
    }
    failed_run_ids = {row["run_id"] for row in runs if row.get("status") == "failed"}
    advanced_failed = (
        store.mvp_watermark_count_for_runs(failed_run_ids)
        if hasattr(store, "mvp_watermark_count_for_runs")
        else None
    )
    watermark_gate = {
        "status": "ready" if advanced_failed == 0 else "blocked",
        "failed_run_ids": sorted(failed_run_ids),
        "watermarks_advanced_for_failed_runs": advanced_failed,
    }
    gates = {
        "seven_calendar_days": seven_days,
        "terminal_receipt": terminal,
        "no_eight_hour_silence": silence,
        "cell_coverage": cell_gate,
        "canonical_idempotency": duplicate_gate,
        "failed_run_watermarks": watermark_gate,
    }
    if seven_days["status"] == "blocked":
        overall = "blocked"
    elif any(item["status"] == "failed" for item in gates.values()):
        overall = "failed"
    elif any(item["status"] == "blocked" for item in gates.values()):
        overall = "blocked"
    else:
        overall = "ready"
    return {
        "status": overall,
        "window_start": _iso(start),
        "window_end": _iso(end),
        "manifest_version": manifest.version,
        "manifest_hash": manifest_digest(manifest),
        "run_count": len(runs),
        "runs": runs,
        "coverage": health["coverage"],
        "health": health,
        "storage": storage_health,
        "gates": gates,
        "unresolved": {
            "source_entitlement": "A/US pilot source entitlement remains blocked",
            "nas_cutover": "not in #71 scope",
        },
    }


def _write_receipt(path: Path, payload: Mapping[str, Any]) -> None:
    path.expanduser().parent.mkdir(parents=True, exist_ok=True)
    path.expanduser().write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


async def _run_forever(args: argparse.Namespace) -> None:
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop_event.set)
        except NotImplementedError:  # pragma: no cover - platform fallback
            signal.signal(signum, lambda *_: stop_event.set())
    while not stop_event.is_set():
        result = await run_demo_once(
            db_path=args.db,
            manifest_path=args.manifest,
            interval_seconds=args.interval,
            lock_path=args.lock,
        )
        print(
            json.dumps(
                {
                    "observed_at": result["observed_at"],
                    "status": result["status"],
                    "run_id": result["run_id"],
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=args.interval)
        except TimeoutError:
            continue


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the bounded 3+3 MVP reliability worker")
    parser.add_argument("--manifest", default="configs/mvp_manifest.json")
    parser.add_argument("--db", default="data/kline.db")
    parser.add_argument("--lock", default=None)
    parser.add_argument("--interval", type=int, default=MAX_INTERVAL_SECONDS)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--audit", action="store_true")
    parser.add_argument("--window-start", default=None)
    parser.add_argument("--window-end", default=None)
    parser.add_argument("--receipt", default=None)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.interval <= 0 or args.interval > MAX_INTERVAL_SECONDS:
        raise SystemExit("--interval must be positive and no greater than four hours")
    if args.audit:
        end = _parse_time(args.window_end or datetime.now(timezone.utc))
        start = _parse_time(args.window_start or (end - timedelta(days=RELIABILITY_DAYS)))
        manifest = load_manifest(args.manifest)
        store = KlineStore(str(Path(args.db).expanduser()))
        report = audit_reliability(
            manifest,
            store,
            window_start=start,
            window_end=end,
            interval_seconds=args.interval,
        )
        if args.receipt:
            _write_receipt(Path(args.receipt), report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["status"] == "ready" else 2
    if args.once:
        result = asyncio.run(
            run_demo_once(
                db_path=args.db,
                manifest_path=args.manifest,
                interval_seconds=args.interval,
                lock_path=args.lock,
            )
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    asyncio.run(_run_forever(args))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
