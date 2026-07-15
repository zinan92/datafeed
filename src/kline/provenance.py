"""Data provenance + freshness — the trust header every response carries.

Two honest facts a candle service owes its consumers: *where did this come
from* and *how old is it*. This module maps each asset class to its upstream
identity and computes freshness where it is meaningful.

The same OHLCV shape can hide materially different source semantics. This
module keeps the source identity explicit so consumers can choose their own
cache, freshness, fallback, and execution-venue policies without guessing.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from kline.models import AssetClass, Timeframe
from kline.ports import ProviderMeta, SourceManifest

_TF_SECONDS: dict[Timeframe, int] = {
    Timeframe.MIN_1: 60,
    Timeframe.MIN_5: 300,
    Timeframe.MIN_15: 900,
    Timeframe.MIN_30: 1800,
    Timeframe.HOUR_1: 3600,
    Timeframe.HOUR_4: 14_400,
    Timeframe.DAY: 86_400,
    Timeframe.WEEK: 604_800,
}

# A continuous-market bar is considered fresh within this many bar-intervals.
_FRESH_BAR_MULTIPLE = 3


_PROVIDER_META: dict[AssetClass, ProviderMeta] = {
    AssetClass.CRYPTO: ProviderMeta(
        name="binance_spot",
        source_mode="binance_spot_public",
        quality_flags=("public_api", "spot", "research_only", "not_execution_venue"),
        continuous=True,
        execution_venue=False,
        realtime_supported=True,
        market_type="spot",
    ),
    AssetClass.US_STOCK: ProviderMeta(
        name="yahoo_finance",
        source_mode="yahoo_finance",
        quality_flags=("delayed_possible", "market_hours", "research_only"),
        continuous=False,
        execution_venue=False,
        realtime_supported=False,
        market_type="equity",
    ),
    AssetClass.COMMODITY: ProviderMeta(
        name="yahoo_finance",
        source_mode="yahoo_finance_futures",
        quality_flags=("continuous_contract", "market_hours", "research_only"),
        continuous=False,
        execution_venue=False,
        realtime_supported=False,
        market_type="futures_continuous_contract",
    ),
    AssetClass.A_SHARE: ProviderMeta(
        name="tushare",
        source_mode="tushare_pro",
        quality_flags=("eod", "market_hours", "research_only"),
        continuous=False,
        execution_venue=False,
        realtime_supported=False,
        market_type="equity",
    ),
}

BINANCE_USDM_FUTURES_META = ProviderMeta(
    name="binance_usdm_futures",
    source_mode="binance_usdm_futures",
    quality_flags=("public_api", "usd_m_futures", "live", "execution_venue"),
    continuous=True,
    execution_venue=True,
    realtime_supported=True,
    market_type="usd_m_futures",
    supported_symbols=("XAUUSDT",),
)

FRED_META = ProviderMeta(
    name="fred",
    source_mode="fred_public_csv",
    quality_flags=("public_api", "daily_factor", "research_only"),
    continuous=False,
    execution_venue=False,
    realtime_supported=False,
    market_type="economic_series",
    supported_symbols=("DTWEXBGS", "DFII10", "GVZCLS", "DFF"),
)

_SOURCE_ALIASES = {
    "auto": "auto",
    "binance_spot": "binance_spot_public",
    "binance_spot_public": "binance_spot_public",
    "binance_usdm": "binance_usdm_futures",
    "binance_usdm_futures": "binance_usdm_futures",
    "yahoo": "yahoo",
    "yahoo_finance": "yahoo_finance",
    "yahoo_finance_futures": "yahoo_finance_futures",
    "tushare": "tushare_pro",
    "tushare_pro": "tushare_pro",
    "fred": "fred_public_csv",
    "fred_public_csv": "fred_public_csv",
}

_SOURCE_ASSET_CLASSES = {
    "binance_spot_public": AssetClass.CRYPTO,
    "binance_usdm_futures": AssetClass.COMMODITY,
    "yahoo_finance": AssetClass.US_STOCK,
    "yahoo_finance_futures": AssetClass.COMMODITY,
    "tushare_pro": AssetClass.A_SHARE,
}

_SOURCE_META = {
    "binance_spot_public": _PROVIDER_META[AssetClass.CRYPTO],
    "binance_usdm_futures": BINANCE_USDM_FUTURES_META,
    "yahoo_finance": _PROVIDER_META[AssetClass.US_STOCK],
    "yahoo_finance_futures": _PROVIDER_META[AssetClass.COMMODITY],
    "tushare_pro": _PROVIDER_META[AssetClass.A_SHARE],
}

_SOURCE_MANIFESTS: dict[str, SourceManifest] = {
    "binance_spot_public": SourceManifest(
        source_id="binance_spot_public",
        asset_class=AssetClass.CRYPTO,
        meta=_PROVIDER_META[AssetClass.CRYPTO],
        default_for_asset_class=True,
    ),
    "binance_usdm_futures": SourceManifest(
        source_id="binance_usdm_futures",
        asset_class=AssetClass.COMMODITY,
        meta=BINANCE_USDM_FUTURES_META,
        ticker_aliases={"GOLD": "XAUUSDT", "XAUUSD": "XAUUSDT", "XAUUSDT": "XAUUSDT"},
        canonical_instrument_ids={"GOLD": "GOLD", "XAUUSD": "GOLD", "XAUUSDT": "GOLD"},
    ),
    "yahoo_finance": SourceManifest(
        source_id="yahoo_finance",
        asset_class=AssetClass.US_STOCK,
        meta=_PROVIDER_META[AssetClass.US_STOCK],
        default_for_asset_class=True,
    ),
    "yahoo_finance_futures": SourceManifest(
        source_id="yahoo_finance_futures",
        asset_class=AssetClass.COMMODITY,
        meta=_PROVIDER_META[AssetClass.COMMODITY],
        default_for_asset_class=True,
        ticker_aliases={"GOLD": "GC=F", "XAUUSD": "GC=F", "GC=F": "GC=F"},
        canonical_instrument_ids={"GOLD": "GOLD", "XAUUSD": "GOLD", "GC=F": "GOLD"},
    ),
    "tushare_pro": SourceManifest(
        source_id="tushare_pro",
        asset_class=AssetClass.A_SHARE,
        meta=_PROVIDER_META[AssetClass.A_SHARE],
        default_for_asset_class=True,
    ),
}


def fred_source_manifest(asset_class: AssetClass) -> SourceManifest:
    aliases = {
        "DXY": "DTWEXBGS",
        "US10Y_REAL": "DFII10",
        "GLD_FLOW": "GVZCLS",
        "FED_CPI_EVENTS": "DFF",
    }
    return SourceManifest(
        source_id=f"fred_public_csv_{asset_class.value}",
        asset_class=asset_class,
        meta=ProviderMeta(**{**FRED_META.__dict__, "source_mode": f"fred_public_csv_{asset_class.value}"}),
        default_for_asset_class=True,
        ticker_aliases=aliases,
        canonical_instrument_ids={key: key for key in aliases},
    )

_EXTRA_SOURCE_MANIFESTS: dict[str, SourceManifest] = {}


def provider_meta(asset_class: AssetClass) -> ProviderMeta:
    if asset_class in _PROVIDER_META:
        return _PROVIDER_META[asset_class]
    defaults = [
        manifest
        for manifest in all_source_manifests().values()
        if manifest.asset_class == asset_class and manifest.default_for_asset_class
    ]
    if not defaults:
        raise KeyError(f"No default source configured for {asset_class.value}")
    return defaults[0].meta


def default_source(asset_class: AssetClass) -> str:
    return provider_meta(asset_class).source_mode


def normalize_source(source: str, asset_class: AssetClass) -> str:
    source_key = source.strip().lower()
    if source_key in _EXTRA_SOURCE_MANIFESTS:
        return source_key
    normalized = _SOURCE_ALIASES.get(source_key)
    if normalized is None:
        raise KeyError(source)
    if normalized == "auto":
        return default_source(asset_class)
    if normalized == "yahoo":
        return "yahoo_finance_futures" if asset_class == AssetClass.COMMODITY else "yahoo_finance"
    return normalized


def source_asset_class(source: str) -> AssetClass:
    if source in _EXTRA_SOURCE_MANIFESTS:
        return _EXTRA_SOURCE_MANIFESTS[source].asset_class
    return _SOURCE_ASSET_CLASSES[source]


def source_manifest(source: str, asset_class: AssetClass) -> SourceManifest:
    normalized = normalize_source(source, asset_class)
    manifest = _EXTRA_SOURCE_MANIFESTS.get(normalized) or _SOURCE_MANIFESTS[normalized]
    if manifest.asset_class != asset_class:
        raise ValueError(
            f"source {normalized} is for {manifest.asset_class.value}, not {asset_class.value}"
        )
    return manifest


def source_meta(source: str, asset_class: AssetClass) -> ProviderMeta:
    return source_manifest(source, asset_class).meta


def canonical_ticker_for_source(source: str, asset_class: AssetClass, ticker: str) -> str:
    return source_manifest(source, asset_class).canonical_ticker(ticker)


def register_source_manifest(manifest: SourceManifest) -> None:
    _EXTRA_SOURCE_MANIFESTS[manifest.source_id] = manifest


def all_source_manifests() -> dict[str, SourceManifest]:
    return {**_SOURCE_MANIFESTS, **_EXTRA_SOURCE_MANIFESTS}


def live_provider_meta(source_mode: str = "binance_usdm_futures") -> ProviderMeta:
    if source_mode != BINANCE_USDM_FUTURES_META.source_mode:
        raise KeyError(source_mode)
    return BINANCE_USDM_FUTURES_META


def timeframe_seconds(timeframe: Timeframe) -> int:
    return _TF_SECONDS[timeframe]


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
