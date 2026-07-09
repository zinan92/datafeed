"""Data models — the canonical candle format everything normalizes into."""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field
from sqlalchemy import Boolean, Column, DateTime, Float, Index, Integer, String, Text, func
from sqlalchemy.orm import DeclarativeBase


# ── Enums ──────────────────────────────────────────────────


class AssetClass(str, Enum):
    A_SHARE = "a_share"
    US_STOCK = "us_stock"
    CRYPTO = "crypto"
    COMMODITY = "commodity"


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
    asset_class: AssetClass
    timeframe: Timeframe
    count: int
    # ── provenance / trust envelope ──
    schema_version: str = "kline-candles-v1"
    provider: str = ""  # concrete upstream, e.g. "binance_spot", "yahoo_finance"
    source_mode: str = ""  # path label, e.g. "binance_spot_public"
    requested_source: str = "auto"
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
    requested_source: Optional[str] = None
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


# ── SQLAlchemy ORM (storage layer) ─────────────────────────


class Base(DeclarativeBase):
    pass


class KlineRow(Base):
    """One K-line record in SQLite."""

    __tablename__ = "klines"

    id = Column(Integer, primary_key=True, autoincrement=True)
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
        Index("ix_kline_lookup", "ticker", "asset_class", "timeframe", "timestamp", unique=True),
        Index("ix_kline_ticker_tf", "ticker", "timeframe"),
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
