"""Storage seam for the Market Data Database MVP.

The public interface deliberately models an ingestion run rather than exposing
SQLAlchemy sessions.  SQLite is one adapter behind this seam today; a future
server-backed adapter can preserve the same identity and atomicity contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import math
from typing import Any, Mapping, Protocol, Sequence


MVP_TIMEFRAMES = frozenset({"15m", "4h", "1d", "1w"})
VOLUME_SEMANTICS = frozenset({"traded", "quote_derived", "not_applicable"})


class StorageError(ValueError):
    """The storage contract rejected a write before or during a transaction."""


def _value(value: Any) -> str:
    raw = getattr(value, "value", value)
    if not isinstance(raw, str) or not raw.strip():
        raise StorageError("storage identity values must be non-empty strings")
    return raw.strip()


def _timestamp(value: str, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StorageError(f"{field_name} must be a non-empty ISO timestamp")
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise StorageError(f"{field_name} must be an ISO timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


@dataclass(frozen=True)
class CandleSeriesKey:
    """Stable identity for one persisted candle series."""

    instrument_id: str
    display_symbol: str
    provider_symbol: str
    source_id: str
    asset_class: str
    timeframe: str
    adjustment_basis: str
    manifest_version: str

    def __post_init__(self) -> None:
        for field_name in (
            "instrument_id",
            "display_symbol",
            "provider_symbol",
            "source_id",
            "asset_class",
            "adjustment_basis",
            "manifest_version",
        ):
            object.__setattr__(self, field_name, _value(getattr(self, field_name)))
        timeframe = _value(self.timeframe)
        if timeframe not in MVP_TIMEFRAMES:
            raise StorageError(f"unsupported MVP timeframe: {timeframe}")
        object.__setattr__(self, "timeframe", timeframe)


@dataclass(frozen=True)
class MvpCandle:
    """One source-aware candle, including the nullable volume semantics."""

    key: CandleSeriesKey
    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: float | None
    amount: float | None = None
    volume_semantics: str = "traded"
    is_derived: bool = False
    transform_receipt_id: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp", _timestamp(self.timestamp, field_name="timestamp"))
        numeric = (self.open, self.high, self.low, self.close)
        if not all(
            isinstance(value, (int, float)) and math.isfinite(float(value)) for value in numeric
        ):
            raise StorageError("OHLC values must be finite numbers")
        if self.high < max(self.open, self.close) or self.low > min(self.open, self.close):
            raise StorageError("OHLC invariant failed")
        semantics = _value(self.volume_semantics)
        if semantics not in VOLUME_SEMANTICS:
            raise StorageError(f"unknown volume_semantics: {semantics}")
        if semantics == "not_applicable" and self.volume is not None:
            raise StorageError("not_applicable volume must be NULL")
        if semantics != "not_applicable" and self.volume is None:
            raise StorageError("traded/quote_derived volume must be present")
        if self.volume is not None and (
            not isinstance(self.volume, (int, float))
            or not math.isfinite(float(self.volume))
            or self.volume < 0
        ):
            raise StorageError("volume must be a finite non-negative number or NULL")
        if not isinstance(self.is_derived, bool):
            raise StorageError("is_derived must be boolean")
        object.__setattr__(self, "volume_semantics", semantics)


@dataclass(frozen=True)
class SourceObservationWrite:
    """One upstream observation receipt attached to a run."""

    run_id: str
    key: CandleSeriesKey
    success: bool
    request_start: str | None
    request_end: str | None
    response_hash: str | None
    policy: Mapping[str, Any] = field(default_factory=dict)
    candle_count: int = 0
    latest_timestamp: str | None = None
    latency_ms: float | None = None
    served_from: str = "upstream"
    error: str | None = None
    observed_at: str | None = None


@dataclass(frozen=True)
class QualityReceiptWrite:
    run_id: str
    key: CandleSeriesKey
    status: str
    gaps: int = 0
    duplicates: int = 0
    invalid_rows: int = 0
    blocked_cells: int = 0
    details: Mapping[str, Any] = field(default_factory=dict)
    receipt_hash: str | None = None


@dataclass(frozen=True)
class TransformReceiptWrite:
    run_id: str
    manifest_version: str
    instrument_id: str
    source_id: str
    output_timeframe: str
    input_timeframe: str
    aggregation_rule_version: str
    input_start: str
    input_end: str
    input_hash: str
    output_hash: str


@dataclass(frozen=True)
class WatermarkWrite:
    key: CandleSeriesKey
    last_closed_timestamp: str
    cursor: str | None
    run_id: str


@dataclass(frozen=True)
class EntitlementReceiptWrite:
    receipt_id: str
    source_id: str
    status: str
    allowed_history: Mapping[str, Any]
    timeframe_permissions: Sequence[str]
    persistence_allowed: bool
    derived_allowed: bool
    non_display_allowed: bool
    valid_from: str | None
    valid_to: str | None
    evidence_ref: str
    receipt_hash: str


@dataclass(frozen=True)
class BackupReceiptWrite:
    backup_id: str
    run_id: str
    destination: str
    status: str
    checksum: str
    size_bytes: int
    restore_verified: bool
    policy: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MvpRunWrite:
    """Atomic unit submitted to the storage adapter."""

    run_id: str
    manifest_version: str
    manifest_hash: str
    started_at: str
    window_start: str | None
    window_end: str | None
    policy: Mapping[str, Any]
    candles: Sequence[MvpCandle] = ()
    source_observations: Sequence[SourceObservationWrite] = ()
    quality_receipts: Sequence[QualityReceiptWrite] = ()
    transform_receipts: Sequence[TransformReceiptWrite] = ()
    watermarks: Sequence[WatermarkWrite] = ()
    entitlement_receipts: Sequence[EntitlementReceiptWrite] = ()
    backup_receipts: Sequence[BackupReceiptWrite] = ()
    status: str = "success"
    completed_at: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class MvpRunReceipt:
    run_id: str
    status: str
    manifest_version: str
    manifest_hash: str
    candle_count: int
    observation_count: int
    quality_count: int
    transform_count: int
    watermark_count: int
    committed_at: str


@dataclass(frozen=True)
class WatermarkState:
    key: CandleSeriesKey
    last_closed_timestamp: str
    cursor: str | None
    run_id: str


class StoragePort(Protocol):
    """Deep storage seam used by the ingestion orchestrator."""

    def commit_mvp_run(self, write: MvpRunWrite) -> MvpRunReceipt:
        """Atomically persist a run, its observations/receipts, candles and watermarks."""
        ...

    def query_mvp_candles(
        self,
        key: CandleSeriesKey,
        *,
        start: str | None = None,
        end: str | None = None,
        limit: int = 500,
    ) -> list[MvpCandle]:
        """Read only the exact source-aware series identified by ``key``."""
        ...

    def get_mvp_watermark(self, key: CandleSeriesKey) -> WatermarkState | None:
        """Return the last atomically promoted closed-bar watermark."""
        ...
