"""Read-only asset × timeframe health read model for the MVP dashboard."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math
from typing import Any, Mapping, Sequence

from kline.market_calendar import calendar_spec, is_trading_session
from kline.models import AssetClass
from kline.mvp_manifest import MvpManifest, ManifestInstrument, manifest_digest
from kline.storage import StoragePort


MATRIX_TIMEFRAMES = ("15m", "1h", "4h", "1d", "1w")
MATRIX_STATUSES = ("ready", "partial", "stale", "failed", "blocked", "unavailable")
MATRIX_SCOPE_DEMO = "demo_3x3"
MATRIX_SCOPE_FULL = "full_216"
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
    """Apply calendar-aware freshness without treating a closed session as stale."""

    latest = _parse_timestamp(latest_timestamp)
    if latest is None:
        return False
    expected = _expected_latest_closed_start(instrument, timeframe, now)
    if expected is None:
        return False
    grace = {
        "cn_a": {
            "15m": timedelta(minutes=45),
            "1h": timedelta(minutes=90),
            "4h": timedelta(hours=5),
            "1d": timedelta(hours=26),
            "1w": timedelta(days=8),
        },
        "us_equities": {
            "15m": timedelta(minutes=45),
            "1h": timedelta(minutes=90),
            "4h": timedelta(hours=5),
            "1d": timedelta(hours=30),
            "1w": timedelta(days=8),
        },
        "crypto_24x7": {
            "15m": timedelta(minutes=45),
            "1h": timedelta(minutes=90),
            "4h": timedelta(hours=5),
            "1d": timedelta(hours=26),
            "1w": timedelta(days=8),
        },
        "us_futures": {
            "15m": timedelta(minutes=45),
            "1h": timedelta(minutes=90),
            "4h": timedelta(hours=5),
            "1d": timedelta(hours=30),
            "1w": timedelta(days=8),
        },
    }.get(instrument.calendar_id, {})
    return latest + grace.get(timeframe, timedelta(0)) < expected


def _expected_latest_closed_start(
    instrument: ManifestInstrument, timeframe: str, now: datetime
) -> datetime | None:
    """Return the latest expected closed bar start for a manifest calendar."""

    try:
        spec = calendar_spec(instrument.calendar_id)
    except (KeyError, ValueError):
        return None
    try:
        zone = spec.zone
    except Exception:
        return None
    local_now = now.astimezone(zone)
    interval = {
        "15m": timedelta(minutes=15),
        "1h": timedelta(hours=1),
        "4h": timedelta(hours=4),
    }.get(timeframe)
    if spec.continuous:
        if timeframe == "1w":
            monday = local_now.date() - timedelta(days=local_now.weekday())
            return datetime.combine(monday, datetime.min.time(), tzinfo=zone)
        if timeframe == "1d":
            return datetime.combine(local_now.date(), datetime.min.time(), tzinfo=zone)
        if interval is None:
            return None
        elapsed_seconds = local_now.hour * 3600 + local_now.minute * 60 + local_now.second
        bucket_seconds = int(interval.total_seconds())
        start_seconds = math.floor(elapsed_seconds / bucket_seconds) * bucket_seconds
        current_bucket = datetime.combine(local_now.date(), datetime.min.time(), tzinfo=zone)
        return current_bucket + timedelta(seconds=start_seconds) - interval

    if timeframe == "1w":
        for offset in range(0, 15):
            candidate = local_now.date() - timedelta(days=offset)
            if candidate.weekday() != 4 or not is_trading_session(
                instrument.calendar_id, candidate
            ):
                continue
            close = spec.sessions[-1].close_time
            close_at = datetime.combine(candidate, close, tzinfo=zone)
            if close_at <= local_now:
                return datetime.combine(
                    candidate - timedelta(days=4), datetime.min.time(), tzinfo=zone
                )
        return None
    for offset in range(0, 15):
        candidate = local_now.date() - timedelta(days=offset)
        if not is_trading_session(instrument.calendar_id, candidate):
            continue
        if timeframe == "1d":
            close = datetime.combine(candidate, spec.sessions[-1].close_time, tzinfo=zone)
            if close <= local_now:
                return datetime.combine(candidate, datetime.min.time(), tzinfo=zone)
            continue
        if interval is None:
            continue
        latest_start: datetime | None = None
        for window in spec.sessions:
            open_at = datetime.combine(candidate, window.open_time, tzinfo=zone)
            close_at = datetime.combine(candidate, window.close_time, tzinfo=zone)
            if local_now < open_at:
                continue
            effective_now = min(local_now, close_at)
            completed = math.floor(
                (effective_now - open_at).total_seconds() / interval.total_seconds()
            )
            if completed <= 0:
                continue
            candidate_start = open_at + interval * (completed - 1)
            latest_start = max(latest_start, candidate_start) if latest_start else candidate_start
        if latest_start is not None:
            return latest_start
    return None


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


def _safe_run(run: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(run)
    if payload.get("error"):
        payload["error"] = _redacted_error(str(payload["error"]), code="run_error")
    return payload


def _manifest_entitlement(
    instrument: ManifestInstrument, receipt: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    blocked = instrument.source_status == "blocked_for_entitlement"
    if receipt is not None:
        return _redacted_value(dict(receipt))
    return {
        "status": "blocked" if blocked else "unverified",
        "persistence_allowed": False if blocked else None,
        "derived_allowed": False if blocked else None,
        "non_display_allowed": False if blocked else None,
        "evidence_ref": "manifest://blocked" if blocked else "operator_review_required",
    }


def _source_mode(instrument: ManifestInstrument) -> str:
    """Resolve the provider's declared mode without conflating it with identity."""

    try:
        from kline.provenance import source_manifest

        return source_manifest(
            instrument.source_id, AssetClass(instrument.asset_class)
        ).meta.source_mode
    except (KeyError, ValueError, TypeError):
        return instrument.source_id


def _entitlement_block_reason(
    instrument: ManifestInstrument,
    timeframe: str,
    receipt: Mapping[str, Any] | None,
    *,
    derived: bool,
) -> str | None:
    if instrument.source_status == "blocked_for_entitlement":
        return "entitlement_blocked"
    if receipt is None:
        return "entitlement_unverified"
    status = str(receipt.get("status") or "unverified").casefold()
    if status in {"blocked", "missing", "unverified"}:
        return f"entitlement_{status}"
    if status == "expired":
        return "entitlement_expired"
    if receipt.get("persistence_allowed") is False:
        return "persistence_not_allowed"
    if derived and receipt.get("derived_allowed") is False:
        return "derived_not_allowed"
    permissions = receipt.get("timeframe_permissions")
    if isinstance(permissions, Sequence) and not isinstance(permissions, (str, bytes)):
        if timeframe not in {str(item) for item in permissions}:
            return "timeframe_not_permitted"
    else:
        return "timeframe_permission_unverified"
    return None


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
    entitlement: Mapping[str, Any] | None,
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
        "details": _redacted_value(quality.get("details", {})) if quality else {},
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
        "source_mode": _source_mode(instrument),
        "entitlement": _manifest_entitlement(instrument, entitlement),
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
    next_due = None
    if parsed_activity is not None:
        due = parsed_activity + timedelta(seconds=interval_seconds)
        reference = now.astimezone(timezone.utc)
        next_due = _iso(max(due, reference))
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
    scope: str = MATRIX_SCOPE_FULL,
    instrument_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Build the source-aware matrix from persisted facts only.

    The API defaults to the full runtime manifest for #70.  The approved #69
    slice remains available through ``scope="demo_3x3"`` or an explicit
    identity list. Unknown identities fail closed instead of silently
    shrinking the requested matrix.
    """

    observed_at = now or datetime.now(timezone.utc)
    by_id = {instrument.instrument_id: instrument for instrument in manifest.instruments}
    if instrument_ids is not None:
        selected_ids = tuple(instrument_ids)
        scope_name = MATRIX_SCOPE_DEMO if selected_ids == MVP_DEMO_INSTRUMENT_IDS else "custom"
    elif scope == MATRIX_SCOPE_DEMO:
        selected_ids = MVP_DEMO_INSTRUMENT_IDS
        scope_name = MATRIX_SCOPE_DEMO
    elif scope == MATRIX_SCOPE_FULL:
        selected_ids = tuple(instrument.instrument_id for instrument in manifest.instruments)
        scope_name = MATRIX_SCOPE_FULL
    else:
        raise ValueError(f"unsupported matrix scope: {scope}")
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
    entitlements = (
        storage.latest_mvp_entitlement_receipts()
        if hasattr(storage, "latest_mvp_entitlement_receipts")
        else []
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
    entitlement_map = {
        str(row.get("source_id") or ""): row for row in entitlements if row.get("source_id")
    }

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
            entitlement = entitlement_map.get(instrument.source_id)
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
                        entitlement=None,
                        now=observed_at,
                    )
                )
                continue
            entitlement_reason = _entitlement_block_reason(
                instrument,
                timeframe,
                entitlement,
                derived=bool(transform) or timeframe in {"4h", "1w"},
            )
            if entitlement_reason or timeframe in instrument.blocked_timeframes:
                status, reason = "blocked", entitlement_reason or "timeframe_blocked"
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
                        entitlement=None,
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
                    entitlement=entitlement,
                    now=observed_at,
                )
            )

    coverage = _status_counts(cells)
    statuses = {str(cell["status"]) for cell in cells if cell["applicability"] == "applicable"}
    persisted_runs = (
        storage.latest_mvp_runs(limit=24) if hasattr(storage, "latest_mvp_runs") else []
    )
    persisted_runs = [_safe_run(run) for run in persisted_runs]
    run_cutoff = observed_at.astimezone(timezone.utc) - timedelta(hours=24)
    recent_runs = [
        run
        for run in persisted_runs
        if (started := _parse_timestamp(run.get("started_at"))) is None or started >= run_cutoff
    ]
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
            "name": scope_name,
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
        "runs": recent_runs,
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
