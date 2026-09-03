"""Read-only health view that combines the isolated Screening and Watchlist stores."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from kline.free_source_profile import apply_free_source_profile
from kline.health_matrix import (
    MATRIX_SCOPE_FULL,
    MATRIX_SCOPE_WATCHLIST,
    MATRIX_STATUSES,
    MATRIX_TIMEFRAMES,
    build_mvp_health_matrix,
)
from kline.mvp_manifest import load_manifest
from kline.registry import get_store
from kline.store import KlineStore
from kline.watchlist_manifest import load_watchlist_manifest


COMBINED_SCOPE = "screening_watchlist"
MARKET_DATA_DB_ENV = "KLINE_MARKET_DB_PATH"
_market_store_cache: dict[str, KlineStore] = {}


def _parse_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _latest_timestamp(values: list[Any], *, default: str | None = None) -> str | None:
    parsed = [(stamp, _parse_timestamp(stamp)) for stamp in values if stamp]
    parsed = [(stamp, value) for stamp, value in parsed if value is not None]
    if not parsed:
        return default
    return max(parsed, key=lambda item: item[1])[0]


def _merge_coverage(snapshots: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for timeframe in MATRIX_TIMEFRAMES:
        counts = {
            "applicable": 0,
            "not_applicable": 0,
            "technical_ready": 0,
            **{status: 0 for status in MATRIX_STATUSES},
        }
        for snapshot in snapshots:
            source = snapshot.get("coverage", {}).get(timeframe, {})
            for key in counts:
                counts[key] += int(source.get(key, 0) or 0)
        counts["ratio"] = (
            round(counts["ready"] / counts["applicable"], 4)
            if counts["applicable"]
            else None
        )
        result[timeframe] = counts
    return result


def _merge_worker(snapshots: list[dict[str, Any]]) -> dict[str, Any]:
    workers = {
        name: dict(snapshot.get("worker", {}))
        for name, snapshot in zip(("screening", "watchlist"), snapshots)
    }
    attempts = [worker.get("last_attempt_at") for worker in workers.values()]
    successes = [worker.get("last_success_at") for worker in workers.values()]
    runs = [worker for worker in workers.values() if worker.get("last_run_id")]
    latest_run = max(
        runs,
        key=lambda worker: _parse_timestamp(worker.get("last_attempt_at"))
        or datetime.min.replace(tzinfo=timezone.utc),
        default={},
    )
    due_values = [worker.get("next_due_at") for worker in workers.values() if worker.get("next_due_at")]
    due_values = [value for value in due_values if _parse_timestamp(value) is not None]
    return {
        "status": "last_run" if runs else "idle",
        "last_attempt_at": _latest_timestamp(attempts),
        "last_success_at": _latest_timestamp(successes),
        "last_run_id": latest_run.get("last_run_id"),
        "next_due_at": min(due_values, key=lambda value: _parse_timestamp(value))
        if due_values
        else None,
        "interval_seconds": None,
        "schedules": workers,
    }


def _merge_infrastructure(snapshots: list[dict[str, Any]], worker: dict[str, Any]) -> dict[str, Any]:
    screening = snapshots[0].get("infrastructure", {})
    watchlist = snapshots[1].get("infrastructure", {})
    databases = {
        "screening": dict(screening.get("database", {})),
        "market_data": dict(watchlist.get("database", {})),
    }
    database_status = (
        "ready"
        if all(item.get("status") in {"ready", "ok"} for item in databases.values())
        else "failed"
    )
    return {
        "worker": worker,
        "database": {"status": database_status, "filesystem": "local"},
        "databases": databases,
        "ssd_mount_guard": dict(watchlist.get("ssd_mount_guard", {"status": "not_observed"})),
        "nas_backup": dict(watchlist.get("nas_backup", {"status": "pending"})),
    }


def _combined_manifest_hash(snapshots: list[dict[str, Any]]) -> str:
    payload = {
        "screening": snapshots[0].get("manifest_hash"),
        "watchlist": snapshots[1].get("manifest_hash"),
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _market_store(path: str | Path | None = None) -> KlineStore:
    value = str(path or os.environ.get(MARKET_DATA_DB_ENV) or "").strip()
    if not value:
        raise RuntimeError(f"{MARKET_DATA_DB_ENV} must be configured for the combined health view")
    resolved = str(Path(value).expanduser().resolve())
    if resolved not in _market_store_cache:
        _market_store_cache[resolved] = KlineStore(resolved)
    return _market_store_cache[resolved]


def build_combined_health_matrix(
    *,
    screening_store: KlineStore | None = None,
    market_store: KlineStore | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build one read-only snapshot over the two intentionally separate stores."""

    root = Path(__file__).resolve().parents[2]
    screening_manifest = apply_free_source_profile(
        load_manifest(root / "configs" / "mvp_manifest.json")
    )
    watchlist_manifest = load_watchlist_manifest(root / "configs" / "watchlist_manifest.json")
    watchlist_free_source_ids = {
        item.source_id
        for item in watchlist_manifest.instruments
        if item.source_status == "configured"
    }
    snapshots = [
        build_mvp_health_matrix(
            screening_manifest,
            screening_store or get_store(),
            now=now,
            interval_seconds=4 * 60 * 60,
            scope=MATRIX_SCOPE_FULL,
        ),
        build_mvp_health_matrix(
            watchlist_manifest,
            market_store or _market_store(),
            now=now,
            interval_seconds=24 * 60 * 60,
            scope=MATRIX_SCOPE_WATCHLIST,
            free_source_ids=watchlist_free_source_ids,
        ),
    ]
    cells = [
        {**cell, "dataset": dataset}
        for dataset, snapshot in zip(("screening", "watchlist"), snapshots)
        for cell in snapshot["cells"]
    ]
    statuses = {
        cell["status"] for cell in cells if cell["applicability"] == "applicable"
    }
    infrastructure_statuses = [
        snapshot.get("infrastructure", {}).get("database", {}).get("status")
        for snapshot in snapshots
    ]
    if "failed" in infrastructure_statuses or statuses.intersection({"failed", "blocked"}):
        status = "failed"
    elif statuses.intersection({"partial", "stale", "unavailable"}):
        status = "partial"
    else:
        status = "ready"
    universes: dict[str, int] = {}
    for snapshot in snapshots:
        for universe, count in snapshot.get("scope", {}).get("universes", {}).items():
            universes[universe] = universes.get(universe, 0) + int(count)
    worker = _merge_worker(snapshots)
    runs = [
        {**run, "dataset": dataset}
        for dataset, snapshot in zip(("screening", "watchlist"), snapshots)
        for run in snapshot.get("runs", [])
    ]
    runs.sort(
        key=lambda run: _parse_timestamp(run.get("started_at"))
        or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    observed_at = now or datetime.now(timezone.utc)
    return {
        "status": status,
        "as_of": observed_at.astimezone(timezone.utc).replace(microsecond=0).isoformat(),
        "manifest_version": "screening_watchlist_v1",
        "manifest_hash": _combined_manifest_hash(snapshots),
        "manifest_versions": {
            "screening": snapshots[0]["manifest_version"],
            "watchlist": snapshots[1]["manifest_version"],
        },
        "scope": {
            "name": COMBINED_SCOPE,
            "instrument_count": sum(item["scope"]["instrument_count"] for item in snapshots),
            "universes": universes,
        },
        "refresh": {
            "poll_interval_seconds": 30,
            "request_timeout_seconds": 10,
            "last_success_at": observed_at.astimezone(timezone.utc).replace(microsecond=0).isoformat(),
            "snapshot_age_seconds": 0,
            "snapshot_max_age_seconds": 900,
        },
        "worker": worker,
        "workers": worker["schedules"],
        "runs": runs[:48],
        "infrastructure": _merge_infrastructure(snapshots, worker),
        "coverage": _merge_coverage(snapshots),
        "cells": cells,
    }
