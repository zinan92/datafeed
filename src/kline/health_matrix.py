"""Read-only asset × timeframe health read model for the MVP dashboard."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from kline.mvp_manifest import MvpManifest, ManifestInstrument, manifest_digest
from kline.storage import StoragePort


MATRIX_TIMEFRAMES = ("15m", "1h", "4h", "1d", "1w")
MATRIX_STATUSES = ("ready", "partial", "stale", "failed", "blocked", "unavailable")
POLL_INTERVAL_SECONDS = 30
REQUEST_TIMEOUT_SECONDS = 10
SNAPSHOT_MAX_AGE_SECONDS = 900
# These six identities are the approved #69 vertical slice.  The runtime
# manifest remains the authority for their fields; #70 will widen this scope
# to every manifest instrument without changing the cell schema.
MVP_DEMO_INSTRUMENT_IDS = (
    "CN.A.600519",
    "CN.A.300750",
    "CN.A.688981",
    "US.EQ.AAPL",
    "US.EQ.NVDA",
    "US.EQ.TSLA",
)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _status_counts(cells: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    coverage: dict[str, dict[str, Any]] = {}
    for timeframe in MATRIX_TIMEFRAMES:
        states = {"applicable": 0, "not_applicable": 0, **{status: 0 for status in MATRIX_STATUSES}}
        for cell in cells:
            if cell["timeframe"] != timeframe:
                continue
            if cell["applicability"] == "not_applicable":
                states["not_applicable"] += 1
            else:
                states["applicable"] += 1
                states[cell["status"]] += 1
        states["ratio"] = (
            round(states["ready"] / states["applicable"], 4) if states["applicable"] else None
        )
        coverage[timeframe] = states
    return coverage


def _key(row: Mapping[str, Any]) -> tuple[str, str, str, str, str]:
    return (
        str(row.get("source_id") or ""),
        str(row.get("instrument_id") or ""),
        str(row.get("timeframe") or ""),
        str(row.get("adjustment_basis") or "raw_unadjusted"),
        str(row.get("manifest_version") or ""),
    )


def _freshness_stale(
    instrument: ManifestInstrument,
    timeframe: str,
    latest_timestamp: str | None,
    *,
    now: datetime,
) -> bool:
    """Apply a conservative SLA for continuous/futures cells.

    Session markets are not marked stale solely because they are closed; their
    next run is determined by the worker/calendar contract. Continuous assets
    use the same grace values as the approved dashboard spec.
    """

    if instrument.calendar_id not in {"crypto_24x7", "us_futures"}:
        return False
    latest = _parse_timestamp(latest_timestamp)
    if latest is None:
        return False
    grace = {
        "15m": timedelta(minutes=45),
        "1h": timedelta(minutes=90),
        "4h": timedelta(hours=5),
        "1d": timedelta(hours=26),
        "1w": timedelta(days=8),
    }[timeframe]
    return latest + grace < now.astimezone(timezone.utc)


def _redacted_error(error: str | None, *, code: str = "source_error") -> dict[str, Any] | None:
    if not error:
        return None
    text = str(error)
    lower = text.lower()
    for marker in ("token", "api_key", "apikey", "secret", "password"):
        start = lower.find(marker)
        if start >= 0:
            end = text.find(" ", start)
            text = f"{text[:start]}{marker}=<redacted>{text[end:] if end >= 0 else ''}"
            lower = text.lower()
    return {
        "code": code,
        "message": text[:500],
        "redacted_raw": text[:500],
        "next_step": "inspect the run receipt",
    }


def _redacted_value(value: Any) -> Any:
    """Keep persisted policy details useful without exposing credentials."""

    sensitive = ("token", "api_key", "apikey", "secret", "password", "authorization")
    if isinstance(value, Mapping):
        return {
            str(key): "<redacted>"
            if any(marker in str(key).lower() for marker in sensitive)
            else _redacted_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redacted_value(item) for item in value]
    if isinstance(value, str) and any(marker in value.lower() for marker in sensitive):
        return "<redacted>"
    return value


def _manifest_entitlement(instrument: ManifestInstrument) -> dict[str, Any]:
    blocked = instrument.source_status == "blocked_for_entitlement"
    return {
        "status": "blocked" if blocked else "unverified",
        "persistence_allowed": False if blocked else None,
        "derived_allowed": False if blocked else None,
        "non_display_allowed": False if blocked else None,
        "evidence_ref": "manifest://blocked" if blocked else "operator_review_required",
    }


def _cell(
    instrument: ManifestInstrument,
    timeframe: str,
    *,
    status: str,
    reason: str,
    latest: Mapping[str, Any] | None,
    observation: Mapping[str, Any] | None,
    quality: Mapping[str, Any] | None,
    transform: Mapping[str, Any] | None,
    watermark: Mapping[str, Any] | None,
    now: datetime,
) -> dict[str, Any]:
    applicable = (
        timeframe in instrument.required_timeframes
        or timeframe in instrument.blocked_timeframes
        or instrument.source_status == "blocked_for_entitlement"
    ) and timeframe not in instrument.not_applicable_timeframes
    if not applicable:
        return {
            "instrument_id": instrument.instrument_id,
            "display_symbol": instrument.display_symbol,
            "display_name": instrument.display_name,
            "provider_symbol": None,
            "asset_class": instrument.asset_class,
            "source_id": None,
            "source_mode": None,
            "entitlement": None,
            "timeframe": timeframe,
            "applicability": "not_applicable",
            "status": "not_applicable",
            "status_reason": "timeframe_not_required",
            "latest_closed_timestamp": None,
            "row_count": 0,
            "is_derived": None,
            "transform": None,
            "last_attempt_at": None,
            "last_success_at": None,
            "run_id": None,
            "policy": None,
            "coverage": None,
            "quality": None,
            "watermark": None,
            "error": None,
        }

    latest_timestamp = latest.get("latest_timestamp") if latest else None
    last_attempt = observation.get("observed_at") if observation else None
    last_success = last_attempt if observation and observation.get("success") else None
    policy = _redacted_value(observation.get("policy")) if observation else None
    quality_payload = {
        "status": quality.get("status") if quality else None,
        "gaps": int(quality.get("gaps", 0)) if quality else 0,
        "duplicates": int(quality.get("duplicates", 0)) if quality else 0,
        "invalid_rows": int(quality.get("invalid_rows", 0)) if quality else 0,
        "blocked_cells": int(quality.get("blocked_cells", 0)) if quality else 0,
        "details": quality.get("details", {}) if quality else {},
    }
    if quality is None:
        quality_payload["status"] = "missing"
    error = _redacted_error(
        (observation or {}).get("error"),
        code="source_error" if observation and not observation.get("success") else "quality_error",
    )
    if error is None and status in {"failed", "blocked"}:
        error = {
            "code": reason,
            "message": reason,
            "redacted_raw": reason,
            "next_step": "inspect the source entitlement or run receipt",
        }
    derived = bool(transform) or timeframe in {"4h", "1w"}
    run_id = (
        (observation or {}).get("run_id")
        or (quality or {}).get("run_id")
        or (watermark or {}).get("run_id")
        or (transform or {}).get("run_id")
    )
    return {
        "instrument_id": instrument.instrument_id,
        "display_symbol": instrument.display_symbol,
        "display_name": instrument.display_name,
        "provider_symbol": instrument.provider_symbol,
        "asset_class": instrument.asset_class,
        "source_id": instrument.source_id,
        "source_mode": instrument.source_id,
        "entitlement": _manifest_entitlement(instrument),
        "timeframe": timeframe,
        "applicability": "applicable",
        "status": status,
        "status_reason": reason,
        "latest_closed_timestamp": latest_timestamp,
        "row_count": int(latest.get("row_count", 0)) if latest else 0,
        "is_derived": derived,
        "transform": dict(transform) if transform else None,
        "last_attempt_at": last_attempt,
        "last_success_at": last_success,
        "run_id": run_id,
        "policy": policy,
        "coverage": {
            "expected_bars": None,
            "stored_bars": int(latest.get("row_count", 0)) if latest else 0,
            "ratio": None,
        },
        "quality": quality_payload,
        "watermark": dict(watermark) if watermark else None,
        "error": error,
    }


def _worker_payload(
    storage: StoragePort, *, now: datetime, interval_seconds: int
) -> dict[str, Any]:
    runs = storage.latest_mvp_runs(limit=6) if hasattr(storage, "latest_mvp_runs") else []
    latest = (
        runs[0]
        if runs
        else (storage.latest_mvp_run() if hasattr(storage, "latest_mvp_run") else None)
    )
    last_activity = (latest.get("completed_at") or latest.get("started_at")) if latest else None
    parsed_activity = _parse_timestamp(last_activity)
    next_due = (
        _iso(parsed_activity + timedelta(seconds=interval_seconds))
        if parsed_activity is not None
        else None
    )
    return {
        "status": "idle" if latest is None else "last_run",
        "last_attempt_at": latest.get("started_at") if latest else None,
        "last_success_at": latest.get("completed_at")
        if latest and latest.get("status") == "success"
        else None,
        "last_run_id": latest.get("run_id") if latest else None,
        "next_due_at": next_due,
        "interval_seconds": interval_seconds,
    }


def build_mvp_health_matrix(
    manifest: MvpManifest,
    storage: StoragePort,
    *,
    now: datetime | None = None,
    interval_seconds: int = 4 * 60 * 60,
    instrument_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Build the bounded source-aware matrix from persisted facts only.

    The default is the approved #69 3+3 slice.  Callers may pass an explicit
    manifest identity list for deterministic tests or the later full-manifest
    expansion; unknown identities fail closed instead of silently shrinking
    the requested matrix.
    """

    observed_at = now or datetime.now(timezone.utc)
    selected_ids = tuple(instrument_ids or MVP_DEMO_INSTRUMENT_IDS)
    by_id = {instrument.instrument_id: instrument for instrument in manifest.instruments}
    missing_ids = [instrument_id for instrument_id in selected_ids if instrument_id not in by_id]
    if missing_ids:
        raise ValueError(f"matrix instruments missing from manifest: {', '.join(missing_ids)}")
    selected_instruments = [by_id[instrument_id] for instrument_id in selected_ids]
    latest_rows = (
        storage.mvp_latest_closed_bars() if hasattr(storage, "mvp_latest_closed_bars") else []
    )
    observations = (
        storage.latest_mvp_source_observations()
        if hasattr(storage, "latest_mvp_source_observations")
        else []
    )
    qualities = (
        storage.latest_mvp_quality_receipts()
        if hasattr(storage, "latest_mvp_quality_receipts")
        else []
    )
    transforms = (
        storage.latest_mvp_transform_receipts()
        if hasattr(storage, "latest_mvp_transform_receipts")
        else []
    )
    watermarks = (
        storage.latest_mvp_watermarks() if hasattr(storage, "latest_mvp_watermarks") else []
    )
    latest_map = {_key(row): row for row in latest_rows}
    observation_map = {_key(row): row for row in observations}
    quality_map = {_key(row): row for row in qualities}
    transform_map = {
        (
            str(row.get("source_id") or ""),
            str(row.get("instrument_id") or ""),
            str(row.get("output_timeframe") or ""),
            "raw_unadjusted",
            str(row.get("manifest_version") or ""),
        ): row
        for row in transforms
    }
    watermark_map = {_key(row): row for row in watermarks}

    cells: list[dict[str, Any]] = []
    for instrument in selected_instruments:
        for timeframe in MATRIX_TIMEFRAMES:
            key = (
                instrument.source_id,
                instrument.instrument_id,
                timeframe,
                instrument.adjustment_basis,
                manifest.version,
            )
            latest = latest_map.get(key)
            observation = observation_map.get(key)
            quality = quality_map.get(key)
            transform = transform_map.get(key)
            watermark = watermark_map.get(key)
            if timeframe in instrument.not_applicable_timeframes:
                cells.append(
                    _cell(
                        instrument,
                        timeframe,
                        status="not_applicable",
                        reason="timeframe_not_required",
                        latest=None,
                        observation=None,
                        quality=None,
                        transform=None,
                        watermark=None,
                        now=observed_at,
                    )
                )
                continue
            if (
                instrument.source_status == "blocked_for_entitlement"
                or timeframe in instrument.blocked_timeframes
            ):
                status, reason = "blocked", "entitlement_blocked"
            elif timeframe not in instrument.required_timeframes:
                cells.append(
                    _cell(
                        instrument,
                        timeframe,
                        status="not_applicable",
                        reason="timeframe_not_required",
                        latest=None,
                        observation=None,
                        quality=None,
                        transform=None,
                        watermark=None,
                        now=observed_at,
                    )
                )
                continue
            elif observation and not observation.get("success"):
                status, reason = "failed", "source_observation_failed"
            elif quality and quality.get("status") == "blocked":
                status, reason = "blocked", "quality_blocked"
            elif quality and quality.get("status") == "fail":
                status, reason = "failed", "quality_failed"
            elif latest is None:
                status, reason = "unavailable", "no_persisted_closed_bar"
            elif timeframe in {"4h", "1w"} and transform is None:
                status, reason = "partial", "transform_receipt_missing"
            elif quality is None:
                status, reason = "partial", "quality_receipt_missing"
            elif _freshness_stale(
                instrument, timeframe, latest.get("latest_timestamp"), now=observed_at
            ):
                status, reason = "stale", "freshness_sla_exceeded"
            elif quality.get("status") == "partial":
                status, reason = "partial", "quality_partial"
            elif watermark is None:
                status, reason = "partial", "watermark_missing"
            else:
                status, reason = "ready", "closed_bar_quality_passed"
            cells.append(
                _cell(
                    instrument,
                    timeframe,
                    status=status,
                    reason=reason,
                    latest=latest,
                    observation=observation,
                    quality=quality,
                    transform=transform,
                    watermark=watermark,
                    now=observed_at,
                )
            )

    coverage = _status_counts(cells)
    statuses = {str(cell["status"]) for cell in cells if cell["applicability"] == "applicable"}
    latest_runs = storage.latest_mvp_runs(limit=6) if hasattr(storage, "latest_mvp_runs") else []
    latest_run = (
        latest_runs[0]
        if latest_runs
        else (storage.latest_mvp_run() if hasattr(storage, "latest_mvp_run") else None)
    )
    storage_health = storage.mvp_storage_health() if hasattr(storage, "mvp_storage_health") else {}
    backup = storage.latest_mvp_backup() if hasattr(storage, "latest_mvp_backup") else None
    infrastructure_status = "ready" if storage_health.get("status") in {"ok", "ready"} else "failed"
    if infrastructure_status == "failed" or statuses.intersection({"failed", "blocked"}):
        overall = "failed"
    elif statuses.intersection({"partial", "stale", "unavailable"}):
        overall = "partial"
    else:
        overall = "ready"
    universes: dict[str, int] = {}
    for instrument in selected_instruments:
        universes[instrument.universe] = universes.get(instrument.universe, 0) + 1
    return {
        "status": overall,
        "as_of": _iso(observed_at),
        "manifest_version": manifest.version,
        "manifest_hash": manifest_digest(manifest),
        "scope": {
            "name": "demo_3x3",
            "instrument_count": len(selected_instruments),
            "universes": universes,
        },
        "refresh": {
            "poll_interval_seconds": 30,
            "request_timeout_seconds": 10,
            "last_success_at": _iso(observed_at),
            "snapshot_age_seconds": 0,
            "snapshot_max_age_seconds": 900,
        },
        "worker": _worker_payload(storage, now=observed_at, interval_seconds=interval_seconds),
        "runs": latest_runs or ([latest_run] if latest_run else []),
        "infrastructure": {
            "worker": _worker_payload(storage, now=observed_at, interval_seconds=interval_seconds),
            "database": {
                "status": infrastructure_status,
                "path": "redacted",
                "filesystem": "local",
            },
            "ssd_mount_guard": {"status": "not_observed", "volume_id": "redacted"},
            "nas_backup": {
                "status": backup.get("status", "pending") if backup else "pending",
                "last_backup_at": backup.get("created_at") if backup else None,
                "restore_verified": bool(backup and backup.get("restore_verified")),
            },
        },
        "coverage": coverage,
        "cells": cells,
    }
