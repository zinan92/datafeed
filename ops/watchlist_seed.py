"""Persist the approved daily Watchlist into the canonical Market Data Database."""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from kline.config import Settings
from kline.ingestion import IngestionOrchestrator, IngestionPlan, IngestionRunReceipt
from kline.mvp_worker import _SingleRunLock
from kline.ports import MarketDataPort
from kline.registry import init
from kline.session_freshness import assess_daily_freshness
from kline.store import KlineStore
from kline.watchlist_manifest import (
    WatchlistManifest,
    load_watchlist_manifest,
)
from ops.mvp_stock_seed import _batch_report, _batches, _classify_attempt


MARKET_DATA_DB = Path("/Users/wendy/park-data/market/kline.db")
WATCHLIST_LOCK = Path("/Users/wendy/park-data/market/watchlist-worker.lock")
DEFAULT_MANIFEST = Path("configs/watchlist_manifest.json")


def _exact_path(value: str | Path) -> Path:
    return Path(value).expanduser().absolute()


def validate_watchlist_target(
    db_path: str | Path,
    lock_path: str | Path,
) -> tuple[Path, Path]:
    database = _exact_path(db_path)
    lock_file = _exact_path(lock_path)
    if database != MARKET_DATA_DB or database.resolve() != MARKET_DATA_DB.resolve():
        raise ValueError(f"Watchlist runner requires persistent Market Data Database: {MARKET_DATA_DB}")
    if lock_file != WATCHLIST_LOCK or lock_file.resolve() != WATCHLIST_LOCK.resolve():
        raise ValueError(f"Watchlist runner requires dedicated Watchlist lock: {WATCHLIST_LOCK}")
    return database, lock_file


def _persisted_ids(store: KlineStore, manifest: WatchlistManifest) -> set[str]:
    approved = {item.instrument_id for item in manifest.instruments}
    return {
        str(row["instrument_id"])
        for row in store.mvp_latest_closed_bars()
        if row.get("manifest_version") == manifest.version
        and row.get("timeframe") == "1d"
        and row.get("instrument_id") in approved
    }


def _watchlist_batch_report(
    receipt: IngestionRunReceipt,
    *,
    batch_index: int,
    batch_size: int,
) -> dict[str, Any]:
    report = _batch_report(
        receipt,
        phase="daily",
        universe="watchlist",
        batch_index=batch_index,
        batch_size=batch_size,
    )
    counts = Counter(report["error_counts"])
    samples = list(report["error_samples"])
    for attempt in receipt.source_attempts:
        provider_attempts = [
            dict(item)
            for item in attempt.get("provider_attempts", ())
            if isinstance(item, Mapping)
        ]
        for provider_attempt in provider_attempts:
            if (
                _classify_attempt(provider_attempt) is None
                and str(provider_attempt.get("status") or "").casefold() == "error"
            ):
                counts["other_error"] += 1
                if len(samples) < 5:
                    samples.append(
                        {
                            "symbol": attempt.get("provider_symbol"),
                            "timeframe": attempt.get("timeframe"),
                            "source": provider_attempt.get("source"),
                            "status": "error",
                            "http_status": provider_attempt.get("http_status"),
                            "error": str(provider_attempt.get("error") or "")[:240],
                        }
                    )
    report["error_counts"] = dict(counts)
    report["error_samples"] = samples
    return report


async def execute_watchlist_batches(
    *,
    manifest: WatchlistManifest,
    store: KlineStore,
    lock_path: str | Path,
    adapter_resolver: Callable[[Any], MarketDataPort | None] | None = None,
    now: datetime | None = None,
    batch_size: int = 10,
    request_interval_seconds: float = 0.5,
    max_retries: int = 2,
    retry_backoff_seconds: float = 2.0,
    provider_timeout_seconds: float = 30.0,
    instrument_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    if request_interval_seconds < 0:
        raise ValueError("request_interval_seconds must be non-negative")
    manifest_hash = manifest.validated_digest()
    selected_ids = (
        tuple(instrument_ids)
        if instrument_ids is not None
        else tuple(item.instrument_id for item in manifest.instruments)
    )
    known_ids = {item.instrument_id for item in manifest.instruments}
    unknown_ids = sorted(set(selected_ids) - known_ids)
    if unknown_ids:
        raise ValueError(f"Watchlist target identities missing from manifest: {unknown_ids}")
    if not selected_ids:
        raise ValueError("Watchlist target selection must not be empty")
    orchestrator = IngestionOrchestrator(store, adapter_resolver=adapter_resolver)
    reports: list[dict[str, Any]] = []
    current_cells: dict[str, Any] = {}
    with _SingleRunLock(Path(lock_path)):
        for batch_index, batch in enumerate(_batches(selected_ids, batch_size), start=1):
            run_now = now or datetime.now(timezone.utc)
            run_id = (
                f"watchlist-{run_now.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
                f"-{batch_index:03d}"
            )
            receipt = await orchestrator.run_once(
                IngestionPlan(
                    manifest=manifest,
                    run_id=run_id,
                    now=run_now,
                    instrument_ids=batch,
                    timeframes=("1d",),
                    request_interval_seconds=request_interval_seconds,
                    max_retries=max_retries,
                    retry_backoff_seconds=retry_backoff_seconds,
                    provider_timeout_seconds=provider_timeout_seconds,
                    policy={
                        "runner": "watchlist_seed",
                        "universe": "watchlist",
                        "timeframes": ["1d"],
                        "batch_size": batch_size,
                    },
                )
            )
            current_cells.update(
                {cell.instrument_id: cell for cell in receipt.requested_cells}
            )
            reports.append(
                _watchlist_batch_report(
                    receipt,
                    batch_index=batch_index,
                    batch_size=len(batch),
                )
            )

    error_counts: Counter[str] = Counter()
    row_counts: Counter[str] = Counter()
    latency_values: list[float] = []
    status_counts: Counter[str] = Counter()
    for report in reports:
        status_counts[report["status"]] += 1
        error_counts.update(report["error_counts"])
        row_counts.update(report["row_counts"])
        latency_values.extend(report.pop("_latencies_ms", []))
    latency_values.sort()
    p95_index = (
        max(0, min(len(latency_values) - 1, (95 * len(latency_values) + 99) // 100 - 1))
        if latency_values
        else None
    )
    selected_set = set(selected_ids)
    persisted = _persisted_ids(store, manifest) & selected_set
    remaining = [
        item_id for item_id in selected_ids if item_id not in persisted
    ]
    latest_by_id = {
        str(row["instrument_id"]): row
        for row in store.mvp_latest_closed_bars()
        if row.get("manifest_version") == manifest.version
        and row.get("timeframe") == "1d"
        and row.get("instrument_id") in selected_set
    }
    instruments_by_id = {item.instrument_id: item for item in manifest.instruments}
    evaluation_now = now or datetime.now(timezone.utc)
    statuses: dict[str, dict[str, Any]] = {}
    for item_id in selected_ids:
        cell = current_cells.get(item_id)
        status = cell.status if cell is not None else "missing_receipt"
        reason = (
            cell.error or cell.status
            if cell is not None and cell.status != "ready"
            else None
        )
        details: dict[str, Any] = {
            "status": status,
            "reason": reason,
            "available_in_store": item_id in persisted,
        }
        latest = latest_by_id.get(item_id)
        declared = assess_daily_freshness(
            instruments_by_id[item_id],
            str(latest.get("latest_timestamp")) if latest is not None else None,
            now=evaluation_now,
        )
        if declared.convention is not None:
            details.update(
                {
                    "daily_timestamp_convention": declared.convention,
                    "observed_session": declared.observed_session.isoformat()
                    if declared.observed_session is not None
                    else None,
                    "expected_session": declared.expected_session.isoformat()
                    if declared.expected_session is not None
                    else None,
                }
            )
        if status == "ready" and declared.stale is True:
            details["status"] = "stale"
            details["reason"] = "latest_closed_session_missing"
        statuses[item_id] = details
    current_failed = [
        item_id for item_id in selected_ids if statuses[item_id]["status"] != "ready"
    ]
    instrument_status_counts = Counter(
        details["status"] for details in statuses.values()
    )
    return {
        "observed_at": evaluation_now.isoformat(),
        "status": "success" if not current_failed else "partial",
        "manifest_version": manifest.version,
        "manifest_hash": manifest_hash,
        "instrument_count": len(selected_ids),
        "persisted_instrument_count": len(persisted),
        "remaining_after": remaining,
        "current_failed": current_failed,
        "instrument_status_counts": dict(instrument_status_counts),
        "timeframes": ["1d"],
        "batch_count": len(reports),
        "batch_status_counts": dict(status_counts),
        "row_counts": dict(row_counts),
        "error_counts": dict(error_counts),
        "rate_limit_errors": error_counts.get("rate_limit", 0),
        "forbidden_errors": error_counts.get("forbidden", 0),
        "server_errors": error_counts.get("server_error", 0),
        "timeout_errors": error_counts.get("timeout", 0),
        "attempt_count": sum(int(report["attempt_count"]) for report in reports),
        "latency_sample_count": len(latency_values),
        "p95_latency_ms": round(latency_values[p95_index], 1)
        if p95_index is not None
        else None,
        "instrument_statuses": statuses,
        "reports": reports,
    }


async def run_watchlist_seed_once(
    *,
    manifest_path: str | Path = DEFAULT_MANIFEST,
    db_path: str | Path = MARKET_DATA_DB,
    lock_path: str | Path = WATCHLIST_LOCK,
    batch_size: int = 10,
    request_interval_seconds: float = 0.5,
    max_retries: int = 2,
    retry_backoff_seconds: float = 2.0,
    provider_timeout_seconds: float = 30.0,
    instrument_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    database, lock_file = validate_watchlist_target(db_path, lock_path)
    manifest = load_watchlist_manifest(manifest_path)
    init(Settings(db_path=str(database), load_entrypoint_adapters=False))
    return await execute_watchlist_batches(
        manifest=manifest,
        store=KlineStore(str(database)),
        lock_path=lock_file,
        batch_size=batch_size,
        request_interval_seconds=request_interval_seconds,
        max_retries=max_retries,
        retry_backoff_seconds=retry_backoff_seconds,
        provider_timeout_seconds=provider_timeout_seconds,
        instrument_ids=instrument_ids,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Persist the approved daily Watchlist")
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--db", default=str(MARKET_DATA_DB))
    parser.add_argument("--lock", default=str(WATCHLIST_LOCK))
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--request-interval", type=float, default=0.5)
    parser.add_argument("--max-retries", type=int, default=2)
    parser.add_argument("--retry-backoff", type=float, default=2.0)
    parser.add_argument("--provider-timeout", type=float, default=30.0)
    parser.add_argument("--receipt", default=None)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    report = asyncio.run(
        run_watchlist_seed_once(
            manifest_path=args.manifest,
            db_path=args.db,
            lock_path=args.lock,
            batch_size=args.batch_size,
            request_interval_seconds=args.request_interval,
            max_retries=args.max_retries,
            retry_backoff_seconds=args.retry_backoff,
            provider_timeout_seconds=args.provider_timeout,
        )
    )
    rendered = json.dumps(report, ensure_ascii=False, sort_keys=True)
    print(rendered)
    if args.receipt:
        Path(args.receipt).write_text(rendered + "\n", encoding="utf-8")
    return 0 if report["status"] == "success" else 2


if __name__ == "__main__":
    raise SystemExit(main())
