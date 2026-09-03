"""Read-only health view that combines the isolated Screening and Watchlist stores."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping

from kline.free_source_profile import apply_free_source_profile
from kline.health_matrix import (
    MATRIX_SCOPE_FULL,
    MATRIX_SCOPE_WATCHLIST,
    MATRIX_STATUSES,
    MATRIX_TIMEFRAMES,
    build_mvp_health_matrix,
)
from kline.mvp_manifest import load_manifest
from kline.store import KlineReadOnlyStore, KlineStore
from kline.time_utils import parse_utc_timestamp
from kline.watchlist_manifest import load_watchlist_manifest


COMBINED_SCOPE = "screening_watchlist"
SCREENING_DB_ENV = "KLINE_DB_PATH"
MARKET_DATA_DB_ENV = "KLINE_MARKET_DB_PATH"
_screening_store_cache: dict[str, KlineReadOnlyStore] = {}
_market_store_cache: dict[str, KlineReadOnlyStore] = {}


def _latest_timestamp(values: list[Any], *, default: str | None = None) -> str | None:
    parsed = [(stamp, parse_utc_timestamp(stamp)) for stamp in values if stamp]
    parsed = [(stamp, value) for stamp, value in parsed if value is not None]
    if not parsed:
        return default
    return max(parsed, key=lambda item: item[1])[0]


def _merge_coverage(
    snapshots: Mapping[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for timeframe in MATRIX_TIMEFRAMES:
        counts = {
            "applicable": 0,
            "not_applicable": 0,
            "technical_ready": 0,
            **{status: 0 for status in MATRIX_STATUSES},
        }
        for snapshot in snapshots.values():
            source = snapshot.get("coverage", {}).get(timeframe, {})
            for key in counts:
                counts[key] += int(source.get(key, 0) or 0)
        counts["ratio"] = (
            round(
                (counts["ready"] + counts["ready_unverified"]) / counts["applicable"],
                4,
            )
            if counts["applicable"]
            else None
        )
        result[timeframe] = counts
    return result


def _merge_worker(snapshots: Mapping[str, dict[str, Any]]) -> dict[str, Any]:
    workers = {
        name: dict(snapshot.get("worker", {})) for name, snapshot in snapshots.items()
    }
    attempts = [worker.get("last_attempt_at") for worker in workers.values()]
    successes = [worker.get("last_success_at") for worker in workers.values()]
    runs = [worker for worker in workers.values() if worker.get("last_run_id")]
    latest_run = max(
        runs,
        key=lambda worker: parse_utc_timestamp(worker.get("last_attempt_at"))
        or datetime.min.replace(tzinfo=timezone.utc),
        default={},
    )
    due_values = [worker.get("next_due_at") for worker in workers.values() if worker.get("next_due_at")]
    due_values = [value for value in due_values if parse_utc_timestamp(value) is not None]
    return {
        "status": "last_run" if runs else "idle",
        "last_attempt_at": _latest_timestamp(attempts),
        "last_success_at": _latest_timestamp(successes),
        "last_run_id": latest_run.get("last_run_id"),
        "next_due_at": min(due_values, key=lambda value: parse_utc_timestamp(value))
        if due_values
        else None,
        "interval_seconds": None,
        "schedules": workers,
    }


def _merge_infrastructure(
    snapshots: Mapping[str, dict[str, Any]], worker: dict[str, Any]
) -> dict[str, Any]:
    screening = snapshots["screening"].get("infrastructure", {})
    watchlist = snapshots["watchlist"].get("infrastructure", {})
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


def _combined_manifest_hash(snapshots: Mapping[str, dict[str, Any]]) -> str:
    payload = {
        name: snapshot.get("manifest_hash") for name, snapshot in snapshots.items()
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _read_only_store(
    path: str | Path | None,
    *,
    env_name: str,
    cache: dict[str, KlineReadOnlyStore],
) -> KlineReadOnlyStore:
    value = str(path or os.environ.get(env_name) or "").strip()
    if not value:
        raise RuntimeError(f"{env_name} must be configured for the combined health view")
    resolved = str(Path(value).expanduser().resolve())
    if resolved not in cache:
        cache[resolved] = KlineReadOnlyStore(resolved)
    return cache[resolved]


def _screening_store(path: str | Path | None = None) -> KlineReadOnlyStore:
    return _read_only_store(path, env_name=SCREENING_DB_ENV, cache=_screening_store_cache)


def _market_store(path: str | Path | None = None) -> KlineReadOnlyStore:
    return _read_only_store(path, env_name=MARKET_DATA_DB_ENV, cache=_market_store_cache)


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
    snapshots = {
        "screening": build_mvp_health_matrix(
            screening_manifest,
            screening_store or _screening_store(),
            now=now,
            interval_seconds=4 * 60 * 60,
            scope=MATRIX_SCOPE_FULL,
        ),
        "watchlist": build_mvp_health_matrix(
            watchlist_manifest,
            market_store or _market_store(),
            now=now,
            interval_seconds=24 * 60 * 60,
            scope=MATRIX_SCOPE_WATCHLIST,
            free_source_ids=watchlist_free_source_ids,
        ),
    }
    cells = [
        {**cell, "dataset": dataset}
        for dataset, snapshot in snapshots.items()
        for cell in snapshot["cells"]
    ]
    statuses = {
        cell["status"] for cell in cells if cell["applicability"] == "applicable"
    }
    infrastructure_statuses = [
        snapshot.get("infrastructure", {}).get("database", {}).get("status")
        for snapshot in snapshots.values()
    ]
    if "failed" in infrastructure_statuses or statuses.intersection({"failed", "blocked"}):
        status = "failed"
    elif statuses.intersection({"partial", "stale", "unavailable"}):
        status = "partial"
    else:
        status = "ready"
    universes: dict[str, int] = {}
    for snapshot in snapshots.values():
        for universe, count in snapshot.get("scope", {}).get("universes", {}).items():
            universes[universe] = universes.get(universe, 0) + int(count)
    worker = _merge_worker(snapshots)
    runs = [
        {**run, "dataset": dataset}
        for dataset, snapshot in snapshots.items()
        for run in snapshot.get("runs", [])
    ]
    runs.sort(
        key=lambda run: parse_utc_timestamp(run.get("started_at"))
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
            "screening": snapshots["screening"]["manifest_version"],
            "watchlist": snapshots["watchlist"]["manifest_version"],
        },
        "scope": {
            "name": COMBINED_SCOPE,
            "instrument_count": sum(
                item["scope"]["instrument_count"] for item in snapshots.values()
            ),
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
