"""Data models — the canonical candle format everything normalizes into."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import Boolean, Column, DateTime, Float, Index, Integer, String, Text, func
from sqlalchemy.orm import DeclarativeBase


# ── Enums ──────────────────────────────────────────────────


class AssetClass(str, Enum):
    A_SHARE = "a_share"
    US_STOCK = "us_stock"
    CRYPTO = "crypto"
    COMMODITY = "commodity"
    FUTURES = "futures"
    FOREX = "forex"
    INDEX = "index"
    ETF = "etf"
    MACRO = "macro"
    FLOW = "flow"
    EVENT = "event"


class Timeframe(str, Enum):
    MIN_1 = "1m"
    MIN_5 = "5m"
    MIN_15 = "15m"
    MIN_30 = "30m"
    HOUR_1 = "1h"
    HOUR_4 = "4h"
    DAY = "1d"
    WEEK = "1w"


class CachePolicy(str, Enum):
    ALLOW = "allow"
    BYPASS = "bypass"
    REQUIRE = "require"


class QualityPolicy(str, Enum):
    STANDARD = "standard"
    STRICT = "strict"


class FallbackPolicy(str, Enum):
    NONE = "none"
    EXPLICIT = "explicit"


# ── Pydantic schemas (API layer) ──────────────────────────


class Candle(BaseModel):
    """One OHLCV candle — the universal output format."""

    timestamp: str  # ISO 8601: "2026-03-28" (daily) or "2026-03-28T14:30:00" (intraday)
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: Optional[float] = None
    # Provenance — stamped on the way out so a consumer never has to guess
    # where a bar came from. Optional so cached/ORM rows stay constructible.
    provider: Optional[str] = None
    quality_flags: list[str] = Field(default_factory=list)

    @field_validator("timestamp")
    @classmethod
    def normalize_timestamp(cls, value: str) -> str:
        text = value.strip()
        if "T" not in text and " " not in text:
            return text
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat()


class TimeframeTransform(BaseModel):
    """How the public timeframe was produced from the upstream bars."""

    raw_timeframe: Timeframe
    timeframe_origin: Literal["native", "aggregated"]
    aggregation: dict[str, Any] = Field(default_factory=dict)


class CandleResponse(BaseModel):
    """API response envelope.

    Carries a provenance/trust header alongside the candles so a downstream
    consumer (chart, backtest, agent) can tell where the data came from, how
    fresh it is, and — critically — that it is real market data, never a
    fabricated placeholder. kline has no synthetic path: ``is_synthetic`` is
    always ``False``. The field exists so consumers can rely on a single,
    uniform OHLCV contract across data sources.
    """

    ticker: str
    instrument_id: str = ""
    provider_symbol: str = ""
    asset_class: AssetClass
    timeframe: Timeframe
    count: int
    raw_timeframe: Optional[Timeframe] = None
    timeframe_origin: Optional[Literal["native", "aggregated"]] = None
    aggregation: dict[str, Any] = Field(default_factory=dict)
    source_identity: dict[str, Any] = Field(default_factory=dict)
    # ── provenance / trust envelope ──
    schema_version: str = "kline-candles-v1"
    provider: str = ""  # concrete upstream, e.g. "binance_spot", "yahoo_finance"
    source_mode: str = ""  # path label, e.g. "binance_spot_public"
    requested_source: str = "auto"
    selected_source: str = ""
    selection_reason: str = "requested_or_default"
    attempted_sources: list[str] = Field(default_factory=list)
    cache_policy: CachePolicy = CachePolicy.ALLOW
    quality_policy: QualityPolicy = QualityPolicy.STANDARD
    fallback_policy: FallbackPolicy = FallbackPolicy.NONE
    require_execution_venue: bool = False
    quality_flags: list[str] = Field(default_factory=list)
    is_synthetic: bool = False  # kline never fabricates data — always False
    served_from: str = ""  # "cache" | "upstream" | "websocket"
    fresh: Optional[bool] = None  # only asserted for continuous (24/7) markets
    latest_timestamp: Optional[str] = None
    age_seconds: Optional[float] = None
    max_age_seconds: Optional[float] = None
    execution_venue: bool = False
    reject_reason: Optional[str] = None
    access_issues: list[str] = Field(default_factory=list)
    candles: list[Candle]


class ErrorResponse(BaseModel):
    """Standard error format."""

    error: str
    detail: Optional[str] = None
    suggestions: Optional[list[str]] = None
    provider: Optional[str] = None
    source_mode: Optional[str] = None
    provider_symbol: Optional[str] = None
    timeframe: Optional[Timeframe] = None
    raw_timeframe: Optional[Timeframe] = None
    timeframe_origin: Optional[Literal["native", "aggregated"]] = None
    aggregation: dict[str, Any] = Field(default_factory=dict)
    source_identity: dict[str, Any] = Field(default_factory=dict)
    requested_source: Optional[str] = None
    selected_source: Optional[str] = None
    attempted_sources: list[str] = Field(default_factory=list)
    cache_policy: Optional[CachePolicy] = None
    quality_policy: Optional[QualityPolicy] = None
    fallback_policy: Optional[FallbackPolicy] = None
    require_execution_venue: bool = False
    served_from: Optional[str] = None
    is_synthetic: bool = False
    fresh: Optional[bool] = None
    latest_timestamp: Optional[str] = None
    age_seconds: Optional[float] = None
    max_age_seconds: Optional[float] = None
    quality_flags: list[str] = Field(default_factory=list)
    execution_venue: bool = False
    reject_reason: Optional[str] = None
    access_issues: list[str] = Field(default_factory=list)


class InstrumentDefinition(BaseModel):
    """Versioned execution instrument metadata from an upstream venue."""

    schema_version: str = "instrument-definition-v1"
    instrument_id: str
    venue: str
    symbol: str
    asset_class: AssetClass
    market_type: str
    contract_type: str
    status: str
    base_currency: str
    quote_currency: str
    settlement_currency: str
    margin_currency: str
    is_inverse: bool
    price_precision: int
    price_increment: str
    min_price: str
    max_price: str
    size_precision: int
    size_increment: str
    min_quantity: str
    max_quantity: str
    market_size_increment: str
    market_min_quantity: str
    market_max_quantity: str
    min_notional: str
    margin_init_rate: str
    margin_maint_rate: str
    maker_fee_rate: Optional[str] = None
    taker_fee_rate: Optional[str] = None
    contract_multiplier: Optional[str] = None
    order_types: list[str] = Field(default_factory=list)
    time_in_force: list[str] = Field(default_factory=list)
    provider: str
    source_mode: str
    served_from: str = "upstream"
    execution_venue: bool
    is_synthetic: bool = False
    upstream_server_time: Optional[int] = None
    observed_at: str
    missing_fields: list[str] = Field(default_factory=list)
    derived_fields: dict[str, str] = Field(default_factory=dict)


# ── SQLAlchemy ORM (storage layer) ─────────────────────────


class Base(DeclarativeBase):
    pass


class KlineRow(Base):
    """One K-line record in SQLite."""

    __tablename__ = "klines"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source_id = Column(
        String, nullable=False, default="legacy_unknown", server_default="legacy_unknown"
    )
    ticker = Column(String, nullable=False)
    asset_class = Column(String, nullable=False)
    timeframe = Column(String, nullable=False)
    timestamp = Column(String, nullable=False)  # ISO 8601
    open = Column(Float, nullable=False)
    high = Column(Float, nullable=False)
    low = Column(Float, nullable=False)
    close = Column(Float, nullable=False)
    volume = Column(Float, nullable=False, default=0)
    amount = Column(Float, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index(
            "ix_kline_lookup",
            "source_id",
            "ticker",
            "asset_class",
            "timeframe",
            "timestamp",
            unique=True,
        ),
        Index("ix_kline_ticker_tf", "source_id", "ticker", "timeframe"),
    )

    def to_candle(self) -> Candle:
        return Candle(
            timestamp=self.timestamp,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
            amount=self.amount,
        )


class RawUpstreamResponse(Base):
    """Raw upstream payload captured for debugging provider behavior."""

    __tablename__ = "raw_upstream_responses"

    id = Column(Integer, primary_key=True, autoincrement=True)
    provider = Column(String, nullable=False)
    source_mode = Column(String, nullable=False)
    ticker = Column(String, nullable=False)
    asset_class = Column(String, nullable=False)
    timeframe = Column(String, nullable=False)
    served_from = Column(String, nullable=False)
    execution_venue = Column(Boolean, nullable=False, default=False)
    request_params = Column(Text, nullable=False)
    response_body = Column(Text, nullable=False)
    status_code = Column(Integer, nullable=True)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now())

    __table_args__ = (
        Index("ix_raw_response_lookup", "provider", "ticker", "timeframe", "created_at"),
    )


class SourceObservation(Base):
    """Auditable result of one upstream source request."""

    __tablename__ = "source_observations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source_id = Column(String, nullable=False)
    provider = Column(String, nullable=False)
    asset_class = Column(String, nullable=False)
    ticker = Column(String, nullable=False)
    timeframe = Column(String, nullable=False)
    success = Column(Boolean, nullable=False)
    served_from = Column(String, nullable=False, default="upstream")
    candle_count = Column(Integer, nullable=False, default=0)
    latest_timestamp = Column(String, nullable=True)
    latency_ms = Column(Float, nullable=True)
    error = Column(Text, nullable=True)
    quality_flags = Column(Text, nullable=False, default="[]")
    observed_at = Column(DateTime, server_default=func.now(), nullable=False)

    __table_args__ = (
        Index("ix_source_observation_latest", "source_id", "observed_at"),
        Index(
            "ix_source_observation_instrument",
            "source_id",
            "ticker",
            "timeframe",
            "observed_at",
        ),
    )


class MvpCandleRow(Base):
    """Source-aware MVP candle; isolated from the legacy ``klines`` table."""

    __tablename__ = "mvp_candles"

    id = Column(Integer, primary_key=True, autoincrement=True)
    instrument_id = Column(String, nullable=False)
    display_symbol = Column(String, nullable=False)
    provider_symbol = Column(String, nullable=False)
    source_id = Column(String, nullable=False)
    asset_class = Column(String, nullable=False)
    timeframe = Column(String, nullable=False)
    adjustment_basis = Column(String, nullable=False)
    manifest_version = Column(String, nullable=False)
    timestamp = Column(String, nullable=False)
    open = Column(Float, nullable=False)
    high = Column(Float, nullable=False)
    low = Column(Float, nullable=False)
    close = Column(Float, nullable=False)
    volume = Column(Float, nullable=True)
    amount = Column(Float, nullable=True)
    volume_semantics = Column(String, nullable=False)
    is_derived = Column(Boolean, nullable=False, default=False)
    transform_receipt_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index(
            "uq_mvp_candle_identity",
            "source_id",
            "instrument_id",
            "timeframe",
            "adjustment_basis",
            "manifest_version",
            "timestamp",
            unique=True,
        ),
        Index(
            "ix_mvp_candle_series",
            "source_id",
            "instrument_id",
            "timeframe",
            "manifest_version",
        ),
    )


class MvpRunRow(Base):
    """One ingestion attempt, including the manifest identity it ran."""

    __tablename__ = "mvp_runs"

    run_id = Column(String, primary_key=True)
    manifest_version = Column(String, nullable=False)
    manifest_hash = Column(String, nullable=False)
    status = Column(String, nullable=False)
    started_at = Column(String, nullable=False)
    completed_at = Column(String, nullable=True)
    window_start = Column(String, nullable=True)
    window_end = Column(String, nullable=True)
    policy_json = Column(Text, nullable=False, default="{}")
    receipt_hash = Column(String, nullable=True)
    error = Column(Text, nullable=True)
    candle_count = Column(Integer, nullable=False, default=0)
    observation_count = Column(Integer, nullable=False, default=0)
    quality_count = Column(Integer, nullable=False, default=0)
    transform_count = Column(Integer, nullable=False, default=0)
    watermark_count = Column(Integer, nullable=False, default=0)
    committed_at = Column(String, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)


class MvpWatermarkRow(Base):
    """Last closed candle promoted for one exact source-aware series."""

    __tablename__ = "mvp_watermarks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    instrument_id = Column(String, nullable=False)
    display_symbol = Column(String, nullable=False)
    provider_symbol = Column(String, nullable=False)
    source_id = Column(String, nullable=False)
    asset_class = Column(String, nullable=False)
    timeframe = Column(String, nullable=False)
    adjustment_basis = Column(String, nullable=False)
    manifest_version = Column(String, nullable=False)
    last_closed_timestamp = Column(String, nullable=False)
    cursor = Column(String, nullable=True)
    run_id = Column(String, nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    __table_args__ = (
        Index(
            "uq_mvp_watermark_identity",
            "source_id",
            "instrument_id",
            "timeframe",
            "adjustment_basis",
            "manifest_version",
            unique=True,
        ),
    )


class MvpSourceObservationRow(Base):
    """Request-level observation with identity, window, policy and response hash."""

    __tablename__ = "mvp_source_observations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(String, nullable=False)
    manifest_version = Column(String, nullable=False)
    instrument_id = Column(String, nullable=False)
    display_symbol = Column(String, nullable=False)
    provider_symbol = Column(String, nullable=False)
    source_id = Column(String, nullable=False)
    asset_class = Column(String, nullable=False)
    timeframe = Column(String, nullable=False)
    success = Column(Boolean, nullable=False)
    served_from = Column(String, nullable=False)
    request_start = Column(String, nullable=True)
    request_end = Column(String, nullable=True)
    response_hash = Column(String, nullable=True)
    policy_json = Column(Text, nullable=False, default="{}")
    candle_count = Column(Integer, nullable=False, default=0)
    latest_timestamp = Column(String, nullable=True)
    latency_ms = Column(Float, nullable=True)
    error = Column(Text, nullable=True)
    observed_at = Column(String, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    __table_args__ = (
        Index("ix_mvp_source_observation_run", "run_id", "source_id", "instrument_id", "timeframe"),
    )


class MvpQualityReceiptRow(Base):
    """Quality gate receipt attached to one run/series."""

    __tablename__ = "mvp_quality_receipts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(String, nullable=False)
    manifest_version = Column(String, nullable=False)
    instrument_id = Column(String, nullable=False)
    source_id = Column(String, nullable=False)
    timeframe = Column(String, nullable=False)
    status = Column(String, nullable=False)
    gaps = Column(Integer, nullable=False, default=0)
    duplicates = Column(Integer, nullable=False, default=0)
    invalid_rows = Column(Integer, nullable=False, default=0)
    blocked_cells = Column(Integer, nullable=False, default=0)
    details_json = Column(Text, nullable=False, default="{}")
    receipt_hash = Column(String, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)


class MvpTransformReceiptRow(Base):
    """Auditable input/output identity for a derived timeframe."""

    __tablename__ = "mvp_transform_receipts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(String, nullable=False)
    manifest_version = Column(String, nullable=False)
    instrument_id = Column(String, nullable=False)
    source_id = Column(String, nullable=False)
    output_timeframe = Column(String, nullable=False)
    input_timeframe = Column(String, nullable=False)
    aggregation_rule_version = Column(String, nullable=False)
    input_start = Column(String, nullable=False)
    input_end = Column(String, nullable=False)
    input_hash = Column(String, nullable=False)
    output_hash = Column(String, nullable=False)
    bucket_anchor = Column(String, nullable=True)
    partial_bucket_policy = Column(String, nullable=True)
    partial_bucket_count = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)


class MvpEntitlementReceiptRow(Base):
    """Source licence/permission receipt; absence must remain a blocked state."""

    __tablename__ = "mvp_entitlement_receipts"

    receipt_id = Column(String, primary_key=True)
    source_id = Column(String, nullable=False)
    status = Column(String, nullable=False)
    allowed_history_json = Column(Text, nullable=False)
    timeframe_permissions_json = Column(Text, nullable=False)
    persistence_allowed = Column(Boolean, nullable=False)
    derived_allowed = Column(Boolean, nullable=False)
    non_display_allowed = Column(Boolean, nullable=False)
    valid_from = Column(String, nullable=True)
    valid_to = Column(String, nullable=True)
    evidence_ref = Column(String, nullable=False)
    receipt_hash = Column(String, nullable=False)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    __table_args__ = (Index("ix_mvp_entitlement_source", "source_id", "valid_from", "valid_to"),)


class MvpBackupReceiptRow(Base):
    """Completed consistent-backup receipt, including restore verification."""

    __tablename__ = "mvp_backup_receipts"

    backup_id = Column(String, primary_key=True)
    run_id = Column(String, nullable=False)
    destination = Column(String, nullable=False)
    status = Column(String, nullable=False)
    checksum = Column(String, nullable=False)
    size_bytes = Column(Integer, nullable=False)
    restore_verified = Column(Boolean, nullable=False)
    policy_json = Column(Text, nullable=False, default="{}")
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
