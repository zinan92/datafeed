"""Seed the remaining 100+100 stock MVP with bounded free-source batches.

The existing reliability worker intentionally exercises only the approved 3+3
vertical slice.  This one-shot runner is the next, resumable step: it skips
instrument/timeframe identities that already have closed bars, runs coarse bars
before intraday bars, and writes a redacted batch report for rate-limit review.
It never touches the legacy service database or performs a NAS cutover.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import signal
from typing import Any, Mapping, Sequence

from kline.config import Settings
from kline.free_source_profile import apply_free_source_profile
from kline.ingestion import IngestionOrchestrator, IngestionPlan, IngestionRunReceipt
from kline.mvp_manifest import MvpManifest, load_manifest, manifest_digest
from kline.registry import init
from kline.mvp_worker import _SingleRunLock
from kline.store import KlineStore


STOCK_UNIVERSES = ("a_share", "us_stock")
COARSE_TIMEFRAMES = ("1d", "1w")
INTRADAY_TIMEFRAMES = ("15m", "1h", "4h")
DEFAULT_BATCH_SIZE = 10
DEFAULT_REQUEST_INTERVAL_SECONDS = 0.25
DEFAULT_MAX_RETRIES = 2
DEFAULT_RETRY_BACKOFF_SECONDS = 1.0
DEFAULT_PROVIDER_TIMEOUT_SECONDS = 45.0
SAFE_OBSERVER_DB = Path("/Users/wendy/datafeed-runtime-issue-71/data/kline.db")
_RATE_MARKERS = ("429", "rate limit", "too many", "throttl", "quota")
_FORBIDDEN_MARKERS = ("403", "forbidden", "blocked")
_SERVER_MARKERS = ("500", "502", "503", "504", "server error", "bad gateway", "service unavailable")
_TIMEOUT_MARKERS = ("timeout", "timed out", "deadline")
_EMPTY_MARKERS = ("no data", "no rows", "empty")
_EMPTY_PATTERN = re.compile(r"\bno\s+\w+\s+rows?\b")


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def stock_instrument_ids(manifest: MvpManifest) -> dict[str, tuple[str, ...]]:
    """Return the canonical stock IDs grouped in deterministic universe order."""

    return {
        universe: tuple(
            item.instrument_id for item in manifest.instruments if item.universe == universe
        )
        for universe in STOCK_UNIVERSES
    }


def _seeded_keys(store: KlineStore) -> set[tuple[str, str, str, str, str]]:
    rows = store.mvp_latest_closed_bars()
    return {
        (
            str(row.get("source_id") or ""),
            str(row.get("instrument_id") or ""),
            str(row.get("timeframe") or ""),
            str(row.get("adjustment_basis") or "raw_unadjusted"),
            str(row.get("manifest_version") or ""),
        )
        for row in rows
    }


def remaining_stock_ids(
    manifest: MvpManifest,
    store: KlineStore,
    required_timeframes: Sequence[str] | None = None,
) -> dict[str, tuple[str, ...]]:
    """Select stock identities missing at least one required closed-bar series."""

    seeded = _seeded_keys(store)
    timeframes = tuple(required_timeframes or ("15m", "1h", "4h", "1d", "1w"))
    result: dict[str, tuple[str, ...]] = {}
    for universe, ids in stock_instrument_ids(manifest).items():
        remaining: list[str] = []
        for instrument_id in ids:
            item = next(
                item for item in manifest.instruments if item.instrument_id == instrument_id
            )
            complete = all(
                (
                    item.source_id,
                    item.instrument_id,
                    timeframe,
                    item.adjustment_basis,
                    manifest.version,
                )
                in seeded
                for timeframe in timeframes
            )
            if not complete:
                remaining.append(instrument_id)
        result[universe] = tuple(remaining)
    return result


def _batches(ids: Sequence[str], size: int) -> list[tuple[str, ...]]:
    return [tuple(ids[index : index + size]) for index in range(0, len(ids), size)]


def validate_seed_target(
    db_path: str | Path, lock_path: str | Path | None = None
) -> tuple[Path, Path]:
    """Fail closed unless the seed targets the isolated observer database."""

    database = Path(db_path).expanduser().resolve()
    if database != SAFE_OBSERVER_DB.resolve():
        raise ValueError(f"stock seed refuses non-observer database; expected {SAFE_OBSERVER_DB}")
    lock_file = (
        Path(lock_path).expanduser().resolve()
        if lock_path
        else SAFE_OBSERVER_DB.with_name("mvp-worker.lock").resolve()
    )
    canonical_lock = SAFE_OBSERVER_DB.with_name("mvp-worker.lock").resolve()
    if lock_file != canonical_lock:
        raise ValueError(f"stock seed requires the canonical observer lock: {canonical_lock}")
    return database, lock_file


def _classify_attempt(attempt: Mapping[str, Any]) -> str | None:
    if attempt.get("watermark_regression_suppressed"):
        return "watermark_regression"
    status = str(attempt.get("status") or "").casefold()
    error = str(attempt.get("error") or "").casefold()
    http_status = attempt.get("http_status")
    try:
        status_code = int(http_status) if http_status is not None else None
    except (TypeError, ValueError):
        status_code = None
    text = f"{status} {error} {http_status or ''}"
    if any(marker in text for marker in _RATE_MARKERS):
        return "rate_limit"
    if any(marker in text for marker in _FORBIDDEN_MARKERS):
        return "forbidden"
    if (status_code is not None and 500 <= status_code <= 599) or any(
        marker in text for marker in _SERVER_MARKERS
    ):
        return "server_error"
    if any(marker in text for marker in _TIMEOUT_MARKERS):
        return "timeout"
    if any(marker in text for marker in _EMPTY_MARKERS) or _EMPTY_PATTERN.search(text):
        return "empty_response"
    if status in {
        "failed",
        "unavailable",
        "provider_error",
        "malformed",
        "transform_incomplete",
    }:
        return "other_error"
    return None


def _redact_error(value: str) -> str:
    text = re.sub(r"(?i)(token|api[_-]?key|secret|password)=?[^\s,;]+", r"\1=<redacted>", value)
    return text[:240]


def _batch_report(
    receipt: IngestionRunReceipt,
    *,
    phase: str,
    universe: str,
    batch_index: int,
    batch_size: int,
) -> dict[str, Any]:
    error_counts: Counter[str] = Counter()
    samples: list[dict[str, Any]] = []
    latency_values: list[float] = []
    attempt_count = 0
    for attempt in receipt.source_attempts:
        provider_attempts = [
            dict(item) for item in attempt.get("provider_attempts", ()) if isinstance(item, Mapping)
        ]
        records = provider_attempts or [dict(attempt)]
        if provider_attempts and _classify_attempt(attempt) is not None:
            records.append(dict(attempt))
        attempt_count += len(records)
        for record in records:
            record.setdefault("symbol", attempt.get("provider_symbol"))
            record.setdefault("timeframe", attempt.get("timeframe"))
            if record.get("latency_ms") is None and attempt.get("latency_ms") is not None:
                record["latency_ms"] = attempt["latency_ms"]
            try:
                latency = float(record["latency_ms"])
            except (KeyError, TypeError, ValueError):
                latency = None
            if latency is not None:
                latency_values.append(latency)
            category = _classify_attempt(record)
            if category is None:
                continue
            error_counts[category] += 1
            if len(samples) < 5:
                samples.append(
                    {
                        "symbol": record.get("symbol"),
                        "timeframe": record.get("timeframe"),
                        "source": record.get("source"),
                        "status": record.get("status"),
                        "http_status": record.get("http_status"),
                        "error": _redact_error(
                            str(record.get("error") or attempt.get("error") or "")
                        ),
                    }
                )
    sorted_latencies = sorted(latency_values)
    p95_index = (
        max(0, min(len(sorted_latencies) - 1, (95 * len(sorted_latencies) + 99) // 100 - 1))
        if sorted_latencies
        else None
    )
    return {
        "run_id": receipt.run_id,
        "phase": phase,
        "universe": universe,
        "batch_index": batch_index,
        "batch_size": batch_size,
        "status": receipt.status,
        "attempt_count": attempt_count,
        "latency_sample_count": len(latency_values),
        "p95_latency_ms": round(sorted_latencies[p95_index], 1) if p95_index is not None else None,
        "requested_cells": len(receipt.requested_cells),
        "row_counts": dict(receipt.row_counts),
        "quality": dict(receipt.quality),
        "error_counts": dict(error_counts),
        "error_samples": samples,
        "_latencies_ms": latency_values,
    }


async def run_stock_seed_once(
    *,
    db_path: str | Path,
    manifest_path: str | Path,
    batch_size: int = DEFAULT_BATCH_SIZE,
    request_interval_seconds: float = DEFAULT_REQUEST_INTERVAL_SECONDS,
    max_retries: int = DEFAULT_MAX_RETRIES,
    retry_backoff_seconds: float = DEFAULT_RETRY_BACKOFF_SECONDS,
    provider_timeout_seconds: float = DEFAULT_PROVIDER_TIMEOUT_SECONDS,
    phase: str = "all",
    include_seeded: bool = False,
    lock_path: str | Path | None = None,
) -> dict[str, Any]:
    """Run the remaining stock identities in coarse-first, bounded batches."""

    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    if request_interval_seconds < 0:
        raise ValueError("request_interval_seconds must be non-negative")
    if max_retries < 0:
        raise ValueError("max_retries must be non-negative")
    if retry_backoff_seconds < 0:
        raise ValueError("retry_backoff_seconds must be non-negative")
    if provider_timeout_seconds <= 0:
        raise ValueError("provider_timeout_seconds must be positive")
    phase_map = {
        "coarse": (COARSE_TIMEFRAMES,),
        "intraday": (INTRADAY_TIMEFRAMES,),
        "all": (COARSE_TIMEFRAMES, INTRADAY_TIMEFRAMES),
    }
    if phase not in phase_map:
        raise ValueError(f"unsupported phase: {phase}")

    observed_at = datetime.now(timezone.utc)
    manifest = apply_free_source_profile(load_manifest(manifest_path))
    database, lock_file = validate_seed_target(db_path, lock_path)
    init(Settings(db_path=str(database), load_entrypoint_adapters=False))
    store = KlineStore(str(database))
    orchestrator = IngestionOrchestrator(store)
    reports: list[dict[str, Any]] = []
    selected_initial = (
        stock_instrument_ids(manifest) if include_seeded else remaining_stock_ids(manifest, store)
    )
    selected_by_phase: dict[str, dict[str, int]] = {}
    for phase_timeframes in phase_map[phase]:
        phase_name = "coarse" if phase_timeframes == COARSE_TIMEFRAMES else "intraday"
        phase_selected = (
            stock_instrument_ids(manifest)
            if include_seeded
            else remaining_stock_ids(manifest, store, required_timeframes=phase_timeframes)
        )
        selected_by_phase[phase_name] = {
            universe: len(ids) for universe, ids in phase_selected.items()
        }
        for universe in STOCK_UNIVERSES:
            for batch_index, batch in enumerate(
                _batches(phase_selected[universe], batch_size), start=1
            ):
                run_now = datetime.now(timezone.utc)
                run_id = (
                    f"mvp-stocks-{run_now.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
                    f"-{phase_name}-{universe}-{batch_index:03d}"
                )
                with _SingleRunLock(lock_file):
                    receipt = await orchestrator.run_once(
                        IngestionPlan(
                            manifest=manifest,
                            run_id=run_id,
                            now=run_now,
                            instrument_ids=batch,
                            timeframes=phase_timeframes,
                            max_retries=max_retries,
                            retry_backoff_seconds=retry_backoff_seconds,
                            provider_timeout_seconds=provider_timeout_seconds,
                            request_interval_seconds=request_interval_seconds,
                            policy={
                                "runner": "mvp_stock_seed",
                                "phase": phase_name,
                                "universe": universe,
                                "batch_size": batch_size,
                                "request_interval_seconds": request_interval_seconds,
                                "max_retries": max_retries,
                                "retry_backoff_seconds": retry_backoff_seconds,
                                "provider_timeout_seconds": provider_timeout_seconds,
                            },
                        )
                    )
                reports.append(
                    _batch_report(
                        receipt,
                        phase=phase_name,
                        universe=universe,
                        batch_index=batch_index,
                        batch_size=len(batch),
                    )
                )

    status_counts: Counter[str] = Counter(report["status"] for report in reports)
    error_counts: Counter[str] = Counter()
    row_counts: Counter[str] = Counter()
    latency_values: list[float] = []
    for report in reports:
        error_counts.update(report["error_counts"])
        row_counts.update(report["row_counts"])
        latency_values.extend(report.pop("_latencies_ms", []))
    final_remaining = remaining_stock_ids(manifest, store)
    latency_values.sort()
    p95_index = (
        max(0, min(len(latency_values) - 1, (95 * len(latency_values) + 99) // 100 - 1))
        if latency_values
        else None
    )
    return {
        "observed_at": _iso(observed_at),
        "status": "success"
        if not final_remaining["a_share"] and not final_remaining["us_stock"]
        else "partial",
        "manifest_version": manifest.version,
        "manifest_hash": manifest_digest(manifest),
        "selected_initial": {universe: len(ids) for universe, ids in selected_initial.items()},
        "selected_by_phase": selected_by_phase,
        "selected_total": sum(sum(counts.values()) for counts in selected_by_phase.values()),
        "remaining_after": {universe: len(ids) for universe, ids in final_remaining.items()},
        "batch_size": batch_size,
        "request_interval_seconds": request_interval_seconds,
        "max_retries": max_retries,
        "retry_backoff_seconds": retry_backoff_seconds,
        "provider_timeout_seconds": provider_timeout_seconds,
        "phase": phase,
        "batch_count": len(reports),
        "batch_status_counts": dict(status_counts),
        "row_counts": dict(row_counts),
        "error_counts": dict(error_counts),
        "rate_limit_errors": error_counts.get("rate_limit", 0),
        "server_errors": error_counts.get("server_error", 0),
        "attempt_count": sum(int(report["attempt_count"]) for report in reports),
        "latency_sample_count": len(latency_values),
        "p95_latency_ms": round(latency_values[p95_index], 1) if p95_index is not None else None,
        "reports": reports,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Seed remaining 100+100 MVP stock identities")
    parser.add_argument("--manifest", default="configs/mvp_manifest.json")
    parser.add_argument("--db", default=str(SAFE_OBSERVER_DB))
    parser.add_argument("--lock", default=None)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--request-interval", type=float, default=DEFAULT_REQUEST_INTERVAL_SECONDS)
    parser.add_argument("--max-retries", type=int, default=DEFAULT_MAX_RETRIES)
    parser.add_argument("--retry-backoff", type=float, default=DEFAULT_RETRY_BACKOFF_SECONDS)
    parser.add_argument("--provider-timeout", type=float, default=DEFAULT_PROVIDER_TIMEOUT_SECONDS)
    parser.add_argument("--phase", choices=("coarse", "intraday", "all"), default="all")
    parser.add_argument("--include-seeded", action="store_true")
    parser.add_argument("--receipt", default=None)
    parser.add_argument("--forever", action="store_true")
    parser.add_argument("--interval", type=int, default=4 * 60 * 60)
    return parser


async def _run_forever(args: argparse.Namespace) -> None:
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop_event.set)
        except NotImplementedError:  # pragma: no cover - platform fallback
            signal.signal(signum, lambda *_: stop_event.set())
    while not stop_event.is_set():
        report = await run_stock_seed_once(
            db_path=args.db,
            manifest_path=args.manifest,
            batch_size=args.batch_size,
            request_interval_seconds=args.request_interval,
            max_retries=args.max_retries,
            retry_backoff_seconds=args.retry_backoff,
            provider_timeout_seconds=args.provider_timeout,
            phase=args.phase,
            include_seeded=args.include_seeded,
            lock_path=args.lock,
        )
        print(
            json.dumps(
                {
                    "observed_at": report["observed_at"],
                    "status": report["status"],
                    "selected_total": report["selected_total"],
                    "remaining_after": report["remaining_after"],
                    "rate_limit_errors": report["rate_limit_errors"],
                    "server_errors": report["server_errors"],
                    "p95_latency_ms": report["p95_latency_ms"],
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=args.interval)
        except TimeoutError:
            continue


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.interval <= 0:
        raise SystemExit("--interval must be positive")
    if args.forever:
        asyncio.run(_run_forever(args))
        return 0
    report = asyncio.run(
        run_stock_seed_once(
            db_path=args.db,
            manifest_path=args.manifest,
            batch_size=args.batch_size,
            request_interval_seconds=args.request_interval,
            max_retries=args.max_retries,
            retry_backoff_seconds=args.retry_backoff,
            provider_timeout_seconds=args.provider_timeout,
            phase=args.phase,
            include_seeded=args.include_seeded,
            lock_path=args.lock,
        )
    )
    payload = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.receipt:
        path = Path(args.receipt).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0 if report["status"] == "success" else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
