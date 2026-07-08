"""Data provenance + freshness — the trust header every response carries.

Two honest facts a candle service owes its consumers: *where did this come
from* and *how old is it*. This module maps each asset class to its upstream
identity and computes freshness where it is meaningful.

Every source here is research-grade — none is an execution venue for a live
order loop. The ``research_only`` / ``not_execution_venue`` flags make that
explicit so any downstream code that wires kline into a trading decision can
fail closed on the flag instead of silently trusting a spot/delayed feed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from kline.models import AssetClass, Timeframe

_TF_SECONDS: dict[Timeframe, int] = {
    Timeframe.MIN_1: 60,
    Timeframe.MIN_5: 300,
    Timeframe.MIN_30: 1800,
    Timeframe.HOUR_1: 3600,
    Timeframe.DAY: 86_400,
    Timeframe.WEEK: 604_800,
}

# A continuous-market bar is considered fresh within this many bar-intervals.
_FRESH_BAR_MULTIPLE = 3


@dataclass(frozen=True)
class ProviderMeta:
    """Identity of the upstream that serves an asset class."""

    name: str  # concrete upstream, e.g. "binance_spot"
    source_mode: str  # path label, e.g. "binance_spot_public"
    quality_flags: tuple[str, ...]
    continuous: bool  # 24/7 market? freshness is only wall-clock-meaningful when True


_PROVIDER_META: dict[AssetClass, ProviderMeta] = {
    AssetClass.CRYPTO: ProviderMeta(
        name="binance_spot",
        source_mode="binance_spot_public",
        quality_flags=("public_api", "spot", "research_only", "not_execution_venue"),
        continuous=True,
    ),
    AssetClass.US_STOCK: ProviderMeta(
        name="yahoo_finance",
        source_mode="yahoo_finance",
        quality_flags=("delayed_possible", "market_hours", "research_only"),
        continuous=False,
    ),
    AssetClass.COMMODITY: ProviderMeta(
        name="yahoo_finance",
        source_mode="yahoo_finance_futures",
        quality_flags=("continuous_contract", "market_hours", "research_only"),
        continuous=False,
    ),
    AssetClass.A_SHARE: ProviderMeta(
        name="tushare",
        source_mode="tushare_pro",
        quality_flags=("eod", "market_hours", "research_only"),
        continuous=False,
    ),
}


def provider_meta(asset_class: AssetClass) -> ProviderMeta:
    return _PROVIDER_META[asset_class]


def _parse_ts(ts: str) -> Optional[datetime]:
    """Parse a candle timestamp; treat a naive value as UTC (our convention)."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def freshness(
    latest_ts: str,
    meta: ProviderMeta,
    timeframe: Timeframe,
    *,
    now: Optional[datetime] = None,
) -> tuple[Optional[float], Optional[float], Optional[bool]]:
    """Return ``(age_seconds, max_age_seconds, fresh)``.

    ``age_seconds`` is always an honest fact. ``fresh`` is only asserted for
    continuous (24/7) markets where wall-clock age is meaningful; for
    market-hours sources it is ``None`` (unknown) — the consumer must apply its
    own market calendar. We never guess a freshness verdict we can't compute
    correctly.
    """
    reference = now or datetime.now(timezone.utc)
    dt = _parse_ts(latest_ts)
    if dt is None:
        return None, None, None
    age_seconds = max(0.0, (reference - dt).total_seconds())
    if not meta.continuous:
        return age_seconds, None, None
    max_age_seconds = float(_TF_SECONDS.get(timeframe, 3600) * _FRESH_BAR_MULTIPLE)
    return age_seconds, max_age_seconds, age_seconds <= max_age_seconds
