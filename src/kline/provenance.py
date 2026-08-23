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

PHASE1_SOURCE_REGISTRY_VERSION = "weekly-macro-phase1-source-registry-v1"

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
        supported_symbols=("CL=F", "GC=F", "SI=F"),
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

BINANCE_USDM_RESEARCH_META = ProviderMeta(
    name="binance_usdm_futures",
    source_mode="binance_usdm_futures_research",
    quality_flags=("public_api", "usd_m_futures", "research_only", "not_execution_venue"),
    continuous=True,
    execution_venue=False,
    realtime_supported=True,
    market_type="usd_m_futures",
    supported_symbols=("BTCUSDT", "ETHUSDT"),
)

HYPERLIQUID_PERP_META = ProviderMeta(
    name="hyperliquid",
    source_mode="hyperliquid_perpetual_public",
    quality_flags=("public_api", "perpetual", "research_only", "not_execution_venue"),
    continuous=True,
    execution_venue=False,
    realtime_supported=False,
    market_type="perpetual_futures",
    supported_symbols=("BTC", "ETH", "HYPE"),
)

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
    supported_symbols=("DTWEXBGS", "DGS2", "DGS10", "T10Y2Y", "DFII10", "GVZCLS", "DFF"),
)

_SOURCE_ALIASES = {
    "auto": "auto",
    "binance_spot": "binance_spot_public",
    "binance_spot_public": "binance_spot_public",
    "binance_usdm": "binance_usdm_futures",
    "binance_usdm_futures": "binance_usdm_futures",
    "binance_usdm_futures_research": "binance_usdm_futures_research",
    "binance_futures_research": "binance_usdm_futures_research",
    "hyperliquid_perpetual_public": "hyperliquid_perpetual_public",
    "hyperliquid_perp": "hyperliquid_perpetual_public",
    "yahoo": "yahoo",
    "yahoo_finance": "yahoo_finance",
    "yahoo_finance_index": "yahoo_finance_index",
    "yahoo_finance_etf": "yahoo_finance_etf",
    "yahoo_finance_futures": "yahoo_finance_futures",
    "tushare": "tushare_pro",
    "tushare_pro": "tushare_pro",
    "tencent": "tencent_kline",
    "tencent_kline": "tencent_kline",
    "sina": "sina_index",
    "sina_index": "sina_index",
    "treasury": "treasury_official_csv",
    "treasury_official": "treasury_official_csv",
    "treasury_official_csv": "treasury_official_csv",
    "treasury_official_csv_derived": "treasury_official_csv_derived",
    "fred": "fred_public_csv",
    "fred_public_csv": "fred_public_csv",
}

_SOURCE_ASSET_CLASSES = {
    "binance_spot_public": AssetClass.CRYPTO,
    "binance_usdm_futures": AssetClass.COMMODITY,
    "binance_usdm_futures_research": AssetClass.CRYPTO,
    "hyperliquid_perpetual_public": AssetClass.CRYPTO,
    "yahoo_finance": AssetClass.US_STOCK,
    "yahoo_finance_futures": AssetClass.COMMODITY,
    "tushare_pro": AssetClass.A_SHARE,
    "tencent_kline": AssetClass.INDEX,
    "sina_index": AssetClass.INDEX,
    "treasury_official_csv": AssetClass.MACRO,
    "treasury_official_csv_derived": AssetClass.MACRO,
    "yahoo_finance_index": AssetClass.INDEX,
    "yahoo_finance_etf": AssetClass.ETF,
    "fred_public_csv_macro": AssetClass.MACRO,
    "fred_public_csv_flow": AssetClass.FLOW,
    "fred_public_csv_event": AssetClass.EVENT,
}

_SOURCE_META = {
    "binance_spot_public": _PROVIDER_META[AssetClass.CRYPTO],
    "binance_usdm_futures": BINANCE_USDM_FUTURES_META,
    "binance_usdm_futures_research": BINANCE_USDM_RESEARCH_META,
    "hyperliquid_perpetual_public": HYPERLIQUID_PERP_META,
    "yahoo_finance": _PROVIDER_META[AssetClass.US_STOCK],
    "yahoo_finance_futures": _PROVIDER_META[AssetClass.COMMODITY],
    "tushare_pro": _PROVIDER_META[AssetClass.A_SHARE],
}

_YAHOO_INDEX_META = ProviderMeta(
    name="yahoo_finance",
    source_mode="yahoo_finance_index",
    quality_flags=("delayed_possible", "market_hours", "research_only"),
    continuous=False,
    execution_venue=False,
    realtime_supported=False,
    market_type="index",
    supported_symbols=("DX-Y.NYB", "^GSPC", "^IXIC", "^VIX", "^N225", "^KS11"),
)
_YAHOO_ETF_META = ProviderMeta(
    name="yahoo_finance",
    source_mode="yahoo_finance_etf",
    quality_flags=("delayed_possible", "market_hours", "research_only"),
    continuous=False,
    execution_venue=False,
    realtime_supported=False,
    market_type="etf",
    supported_symbols=("SPY", "QQQ", "SCHD", "UUP"),
)

_TENCENT_INDEX_META = ProviderMeta(
    name="tencent_finance",
    source_mode="tencent_kline",
    quality_flags=("public_api", "market_hours", "research_only"),
    continuous=False,
    execution_venue=False,
    realtime_supported=False,
    market_type="index",
    supported_symbols=("sh000001", "sh000688", "sh000015"),
)
_SINA_INDEX_META = ProviderMeta(
    name="sina_finance",
    source_mode="sina_index",
    quality_flags=("public_api", "market_hours", "research_only"),
    continuous=False,
    execution_venue=False,
    realtime_supported=False,
    market_type="index",
    supported_symbols=("sh000001", "sh000688", "sh000015"),
)

_TREASURY_META = ProviderMeta(
    name="treasury_official",
    source_mode="treasury_official_csv",
    quality_flags=("official_api", "daily_level", "research_only"),
    continuous=False,
    execution_venue=False,
    realtime_supported=False,
    market_type="treasury_par_yield",
    supported_symbols=("2 Yr", "10 Yr"),
)
_TREASURY_DERIVED_META = ProviderMeta(
    name="treasury_official",
    source_mode="treasury_official_csv_derived",
    quality_flags=("official_api", "derived_level", "research_only"),
    continuous=False,
    execution_venue=False,
    realtime_supported=False,
    market_type="treasury_curve_spread",
    supported_symbols=("10 Yr-2 Yr",),
)

_SOURCE_MANIFESTS: dict[str, SourceManifest] = {
    "binance_spot_public": SourceManifest(
        source_id="binance_spot_public",
        asset_class=AssetClass.CRYPTO,
        meta=_PROVIDER_META[AssetClass.CRYPTO],
        default_for_asset_class=True,
        symbol_timeframes={
            "BTC": (
                Timeframe.MIN_1,
                Timeframe.MIN_5,
                Timeframe.MIN_15,
                Timeframe.MIN_30,
                Timeframe.HOUR_1,
                Timeframe.HOUR_4,
                Timeframe.DAY,
                Timeframe.WEEK,
            ),
            "BTCUSDT": (
                Timeframe.MIN_1,
                Timeframe.MIN_5,
                Timeframe.MIN_15,
                Timeframe.MIN_30,
                Timeframe.HOUR_1,
                Timeframe.HOUR_4,
                Timeframe.DAY,
                Timeframe.WEEK,
            ),
        },
    ),
    "binance_usdm_futures": SourceManifest(
        source_id="binance_usdm_futures",
        asset_class=AssetClass.COMMODITY,
        meta=BINANCE_USDM_FUTURES_META,
        ticker_aliases={"GOLD": "XAUUSDT", "XAUUSD": "XAUUSDT", "XAUUSDT": "XAUUSDT"},
        canonical_instrument_ids={"GOLD": "GOLD", "XAUUSD": "GOLD", "XAUUSDT": "GOLD"},
    ),
    "binance_usdm_futures_research": SourceManifest(
        source_id="binance_usdm_futures_research",
        asset_class=AssetClass.CRYPTO,
        meta=BINANCE_USDM_RESEARCH_META,
        default_for_asset_class=False,
        ticker_aliases={
            "BTC": "BTCUSDT",
            "BTCUSDT": "BTCUSDT",
            "ETH": "ETHUSDT",
            "ETHUSDT": "ETHUSDT",
        },
        canonical_instrument_ids={
            "BTC": "BTCUSDT.BINANCE",
            "BTCUSDT": "BTCUSDT.BINANCE",
            "ETH": "ETHUSDT.BINANCE",
            "ETHUSDT": "ETHUSDT.BINANCE",
        },
        symbol_timeframes={
            "BTCUSDT": (Timeframe.HOUR_4, Timeframe.DAY, Timeframe.WEEK),
            "ETHUSDT": (Timeframe.HOUR_4, Timeframe.DAY, Timeframe.WEEK),
        },
        enforce_symbol_allowlist=True,
    ),
    "hyperliquid_perpetual_public": SourceManifest(
        source_id="hyperliquid_perpetual_public",
        asset_class=AssetClass.CRYPTO,
        meta=HYPERLIQUID_PERP_META,
        default_for_asset_class=False,
        ticker_aliases={"BTC": "BTC", "ETH": "ETH", "HYPE": "HYPE"},
        canonical_instrument_ids={"BTC": "BTC.HYPERLIQUID", "ETH": "ETH.HYPERLIQUID", "HYPE": "HYPE.HYPERLIQUID"},
        symbol_timeframes={
            "BTC": (Timeframe.HOUR_4, Timeframe.DAY, Timeframe.WEEK),
            "ETH": (Timeframe.HOUR_4, Timeframe.DAY, Timeframe.WEEK),
            "HYPE": (Timeframe.HOUR_4, Timeframe.DAY, Timeframe.WEEK),
        },
        enforce_symbol_allowlist=True,
    ),
    "yahoo_finance": SourceManifest(
        source_id="yahoo_finance",
        asset_class=AssetClass.US_STOCK,
        meta=_PROVIDER_META[AssetClass.US_STOCK],
        default_for_asset_class=True,
        blocked_timeframes=(Timeframe.HOUR_4,),
    ),
    "yahoo_finance_futures": SourceManifest(
        source_id="yahoo_finance_futures",
        asset_class=AssetClass.COMMODITY,
        meta=_PROVIDER_META[AssetClass.COMMODITY],
        default_for_asset_class=True,
        ticker_aliases={
            "WTI": "CL=F",
            "OIL": "CL=F",
            "CRUDE": "CL=F",
            "GOLD": "GC=F",
            "XAUUSD": "GC=F",
            "SILVER": "SI=F",
            "XAGUSD": "SI=F",
            "BRENT": "BZ=F",
            "NATGAS": "NG=F",
            "COPPER": "HG=F",
            "PLATINUM": "PL=F",
            "CORN": "ZC=F",
            "WHEAT": "ZW=F",
            "SOYBEAN": "ZS=F",
            "CL=F": "CL=F",
            "GC=F": "GC=F",
            "SI=F": "SI=F",
        },
        canonical_instrument_ids={
            "WTI": "WTI",
            "OIL": "WTI",
            "CRUDE": "WTI",
            "CL=F": "WTI",
            "GOLD": "GOLD",
            "XAUUSD": "GOLD",
            "GC=F": "GOLD",
            "SILVER": "SILVER",
            "XAGUSD": "SILVER",
            "SI=F": "SILVER",
            "BRENT": "BRENT",
            "BZ=F": "BRENT",
            "NATGAS": "NATGAS",
            "NG=F": "NATGAS",
            "COPPER": "COPPER",
            "HG=F": "COPPER",
            "PLATINUM": "PLATINUM",
            "PL=F": "PLATINUM",
            "CORN": "CORN",
            "ZC=F": "CORN",
            "WHEAT": "WHEAT",
            "ZW=F": "WHEAT",
            "SOYBEAN": "SOYBEAN",
            "ZS=F": "SOYBEAN",
        },
        symbol_timeframes={
            "CL=F": (
                Timeframe.MIN_1,
                Timeframe.MIN_5,
                Timeframe.MIN_15,
                Timeframe.MIN_30,
                Timeframe.HOUR_1,
                Timeframe.HOUR_4,
                Timeframe.DAY,
                Timeframe.WEEK,
            ),
            "GC=F": (
                Timeframe.MIN_1,
                Timeframe.MIN_5,
                Timeframe.MIN_15,
                Timeframe.MIN_30,
                Timeframe.HOUR_1,
                Timeframe.HOUR_4,
                Timeframe.DAY,
                Timeframe.WEEK,
            ),
            "SI=F": (
                Timeframe.MIN_1,
                Timeframe.MIN_5,
                Timeframe.MIN_15,
                Timeframe.MIN_30,
                Timeframe.HOUR_1,
                Timeframe.HOUR_4,
                Timeframe.DAY,
                Timeframe.WEEK,
            ),
            "BZ=F": (Timeframe.DAY, Timeframe.WEEK),
            "NG=F": (Timeframe.DAY, Timeframe.WEEK),
            "HG=F": (Timeframe.DAY, Timeframe.WEEK),
            "PL=F": (Timeframe.DAY, Timeframe.WEEK),
            "ZC=F": (Timeframe.DAY, Timeframe.WEEK),
            "ZW=F": (Timeframe.DAY, Timeframe.WEEK),
            "ZS=F": (Timeframe.DAY, Timeframe.WEEK),
        },
        enforce_symbol_allowlist=True,
    ),
    "tushare_pro": SourceManifest(
        source_id="tushare_pro",
        asset_class=AssetClass.A_SHARE,
        meta=_PROVIDER_META[AssetClass.A_SHARE],
        default_for_asset_class=True,
    ),
    "tencent_kline": SourceManifest(
        source_id="tencent_kline",
        asset_class=AssetClass.INDEX,
        meta=_TENCENT_INDEX_META,
        ticker_aliases={
            "SH000001": "sh000001",
            "000001.SH": "sh000001",
            "SH000688": "sh000688",
            "000688.SH": "sh000688",
            "SH000015": "sh000015",
            "000015.SH": "sh000015",
        },
        canonical_instrument_ids={
            "SH000001": "shanghai",
            "000001.SH": "shanghai",
            "SH000688": "star50",
            "000688.SH": "star50",
            "SH000015": "china_dividend",
            "000015.SH": "china_dividend",
            "sh000001": "shanghai",
            "sh000688": "star50",
            "sh000015": "china_dividend",
        },
        symbol_timeframes={
            "SH000001": (Timeframe.DAY, Timeframe.WEEK),
            "SH000688": (Timeframe.DAY, Timeframe.WEEK),
            "SH000015": (Timeframe.DAY, Timeframe.WEEK),
        },
        enforce_symbol_allowlist=True,
    ),
    "sina_index": SourceManifest(
        source_id="sina_index",
        asset_class=AssetClass.INDEX,
        meta=_SINA_INDEX_META,
        ticker_aliases={
            "SH000001": "sh000001",
            "000001.SH": "sh000001",
            "SH000688": "sh000688",
            "000688.SH": "sh000688",
            "SH000015": "sh000015",
            "000015.SH": "sh000015",
        },
        canonical_instrument_ids={
            "SH000001": "shanghai",
            "000001.SH": "shanghai",
            "SH000688": "star50",
            "000688.SH": "star50",
            "SH000015": "china_dividend",
            "000015.SH": "china_dividend",
            "sh000001": "shanghai",
            "sh000688": "star50",
            "sh000015": "china_dividend",
        },
        symbol_timeframes={
            "SH000001": (Timeframe.DAY, Timeframe.WEEK),
            "SH000688": (Timeframe.DAY, Timeframe.WEEK),
            "SH000015": (Timeframe.DAY, Timeframe.WEEK),
        },
        enforce_symbol_allowlist=True,
    ),
    "treasury_official_csv": SourceManifest(
        source_id="treasury_official_csv",
        asset_class=AssetClass.MACRO,
        meta=_TREASURY_META,
        default_for_asset_class=False,
        ticker_aliases={
            "US2Y": "2 Yr",
            "DGS2": "2 Yr",
            "2Y": "2 Yr",
            "US10Y": "10 Yr",
            "DGS10": "10 Yr",
            "10Y": "10 Yr",
        },
        canonical_instrument_ids={
            "US2Y": "us2y",
            "DGS2": "us2y",
            "2Y": "us2y",
            "2 YR": "us2y",
            "US10Y": "us10y",
            "DGS10": "us10y",
            "10Y": "us10y",
            "10 YR": "us10y",
        },
        symbol_timeframes={
            "2 YR": (Timeframe.DAY, Timeframe.WEEK),
            "10 YR": (Timeframe.DAY, Timeframe.WEEK),
        },
        enforce_symbol_allowlist=True,
    ),
    "treasury_official_csv_derived": SourceManifest(
        source_id="treasury_official_csv_derived",
        asset_class=AssetClass.MACRO,
        meta=_TREASURY_DERIVED_META,
        ticker_aliases={
            "US2S10S": "10 Yr-2 Yr",
            "T10Y2Y": "10 Yr-2 Yr",
            "2S10S": "10 Yr-2 Yr",
        },
        canonical_instrument_ids={
            "US2S10S": "us2s10s",
            "T10Y2Y": "us2s10s",
            "2S10S": "us2s10s",
            "10 YR-2 YR": "us2s10s",
        },
        symbol_timeframes={"10 YR-2 YR": (Timeframe.DAY, Timeframe.WEEK)},
        enforce_symbol_allowlist=True,
    ),
    "yahoo_finance_index": SourceManifest(
        source_id="yahoo_finance_index",
        asset_class=AssetClass.INDEX,
        meta=_YAHOO_INDEX_META,
        default_for_asset_class=True,
        ticker_aliases={"DXY": "DX-Y.NYB"},
        canonical_instrument_ids={
            "DXY": "dxy",
            "DX-Y.NYB": "dxy",
            "^GSPC": "sp500",
            "^IXIC": "nasdaq",
            "^VIX": "vix",
            "^N225": "nikkei",
            "^KS11": "kospi",
        },
        symbol_timeframes={
            "DX-Y.NYB": (Timeframe.DAY, Timeframe.WEEK, Timeframe.HOUR_4),
            "^GSPC": (Timeframe.DAY, Timeframe.WEEK),
            "^IXIC": (Timeframe.DAY, Timeframe.WEEK),
            "^VIX": (Timeframe.DAY, Timeframe.WEEK),
            "^N225": (Timeframe.DAY, Timeframe.WEEK),
            "^KS11": (Timeframe.DAY, Timeframe.WEEK),
        },
        enforce_symbol_allowlist=True,
    ),
    "yahoo_finance_etf": SourceManifest(
        source_id="yahoo_finance_etf",
        asset_class=AssetClass.ETF,
        meta=_YAHOO_ETF_META,
        default_for_asset_class=True,
        symbol_timeframes={
            "SPY": (Timeframe.DAY, Timeframe.WEEK),
            "QQQ": (Timeframe.DAY, Timeframe.WEEK),
            "SCHD": (Timeframe.DAY, Timeframe.WEEK),
            "UUP": (Timeframe.HOUR_4, Timeframe.DAY, Timeframe.WEEK),
        },
        enforce_symbol_allowlist=True,
    ),
}


def fred_source_manifest(asset_class: AssetClass) -> SourceManifest:
    aliases = {
        "DXY": "DTWEXBGS",
        "US2Y": "DGS2",
        "US10Y": "DGS10",
        "US2S10S": "T10Y2Y",
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
        extra = _EXTRA_SOURCE_MANIFESTS[source_key]
        if extra.asset_class == asset_class:
            return source_key
        # The generic yahoo source is registered for US stocks at startup;
        # preserve asset-class-specific aliases for indexes and ETFs.
        if source_key not in {"yahoo_finance", "yahoo"}:
            return source_key
    normalized = _SOURCE_ALIASES.get(source_key)
    if normalized is None:
        raise KeyError(source)
    if normalized == "auto":
        return default_source(asset_class)
    if normalized == "yahoo":
        if asset_class == AssetClass.COMMODITY:
            return "yahoo_finance_futures"
        if asset_class == AssetClass.INDEX:
            return "yahoo_finance_index"
        if asset_class == AssetClass.ETF:
            return "yahoo_finance_etf"
        return "yahoo_finance"
    if normalized == "yahoo_finance":
        if asset_class == AssetClass.INDEX:
            return "yahoo_finance_index"
        if asset_class == AssetClass.ETF:
            return "yahoo_finance_etf"
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
