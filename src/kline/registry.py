"""Provider and store registry — wired once at startup."""

from __future__ import annotations

import os
from pathlib import Path

from kline.config import Settings, ensure_data_dir, get_settings
from kline.models import AssetClass, Timeframe
from kline.plugin_loader import load_configured_adapters, load_entrypoint_adapters
from kline.ports import MarketDataPort, ProviderBackedMarketDataAdapter
from kline.providers.ashare import TencentIndexProvider
from kline.providers.tushare_mvp import TuShareEntitlement, TuShareMvpProvider
from kline.providers.base import Provider, ProviderError
from kline.providers.binance_usdm import BinanceUsdmFuturesProvider
from kline.providers.commodity import CommodityProvider
from kline.providers.crypto import CryptoProvider
from kline.providers.fred import FredCsvProvider
from kline.providers.hyperliquid import HyperliquidPerpetualProvider
from kline.providers.sina import SinaIndexProvider
from kline.providers.treasury import TreasuryCsvProvider
from kline.providers.us import USStockProvider
from kline.providers.us_authorized import AuthorizedUSProvider, USDataEntitlement
from kline.provenance import (
    all_source_manifests,
    normalize_source,
    register_source_manifest,
    source_manifest,
    fred_source_manifest,
    PHASE1_SOURCE_REGISTRY_VERSION,
)
from kline.store import KlineStore

_store: KlineStore | None = None
_settings: Settings | None = None
_providers: dict[AssetClass, Provider] = {}
_live_providers: dict[str, Provider] = {}
_adapters: dict[str, MarketDataPort] = {}


def init(settings: Settings | None = None) -> None:
    """Initialize store and providers. Called once at app startup."""
    global _store, _providers, _live_providers, _adapters, _settings
    s = settings or get_settings()
    _settings = s
    ensure_data_dir(s)

    _store = KlineStore(s.db_path)
    _providers = {}
    _live_providers = {}
    _adapters = {}

    # Always available
    _providers[AssetClass.US_STOCK] = USStockProvider()
    _providers[AssetClass.INDEX] = USStockProvider()
    _providers[AssetClass.ETF] = USStockProvider()
    _providers[AssetClass.CRYPTO] = CryptoProvider(timeout=s.request_timeout)
    _providers[AssetClass.COMMODITY] = CommodityProvider()
    _live_providers["binance_usdm_futures"] = BinanceUsdmFuturesProvider(timeout=s.request_timeout)

    register_adapter(
        ProviderBackedMarketDataAdapter(
            source_manifest("yahoo_finance", AssetClass.US_STOCK),
            _providers[AssetClass.US_STOCK],
        )
    )
    register_adapter(
        ProviderBackedMarketDataAdapter(
            source_manifest("yahoo_finance_index", AssetClass.INDEX),
            _providers[AssetClass.INDEX],
        )
    )
    register_adapter(
        ProviderBackedMarketDataAdapter(
            source_manifest("tencent_kline", AssetClass.INDEX),
            TencentIndexProvider(timeout=s.request_timeout),
        )
    )
    register_adapter(
        ProviderBackedMarketDataAdapter(
            source_manifest("sina_index", AssetClass.INDEX),
            SinaIndexProvider(timeout=s.request_timeout),
        )
    )
    register_adapter(
        ProviderBackedMarketDataAdapter(
            source_manifest("yahoo_finance_etf", AssetClass.ETF),
            _providers[AssetClass.ETF],
        )
    )
    us_entitlement = None
    if s.us_data_entitlement_path:
        us_entitlement = USDataEntitlement.from_json_file(s.us_data_entitlement_path)
    if s.us_data_source != "us_authorized_pending":
        raise ProviderError(
            "US MVP source must be registered explicitly before changing us_data_source"
        )
    register_adapter(
        ProviderBackedMarketDataAdapter(
            source_manifest("us_authorized_pending", AssetClass.US_STOCK),
            AuthorizedUSProvider(
                s.us_data_token,
                entitlement=us_entitlement,
                manifest=Path(__file__).resolve().parents[2] / "configs" / "mvp_manifest.json",
                source_id="us_authorized_pending",
            ),
        )
    )
    for factor_asset_class in (AssetClass.MACRO, AssetClass.FLOW, AssetClass.EVENT):
        register_adapter(
            ProviderBackedMarketDataAdapter(
                fred_source_manifest(factor_asset_class),
                FredCsvProvider(timeout=s.request_timeout),
            )
        )
    register_adapter(
        ProviderBackedMarketDataAdapter(
            source_manifest("treasury_official_csv", AssetClass.MACRO),
            TreasuryCsvProvider(timeout=s.request_timeout),
        )
    )
    register_adapter(
        ProviderBackedMarketDataAdapter(
            source_manifest("treasury_official_csv_derived", AssetClass.MACRO),
            TreasuryCsvProvider(timeout=s.request_timeout, derived_spread=True),
        )
    )
    register_adapter(
        ProviderBackedMarketDataAdapter(
            source_manifest("binance_spot_public", AssetClass.CRYPTO),
            _providers[AssetClass.CRYPTO],
        )
    )
    register_adapter(
        ProviderBackedMarketDataAdapter(
            source_manifest("yahoo_finance_futures", AssetClass.COMMODITY),
            _providers[AssetClass.COMMODITY],
        )
    )
    register_adapter(
        ProviderBackedMarketDataAdapter(
            source_manifest("binance_usdm_futures", AssetClass.COMMODITY),
            _live_providers["binance_usdm_futures"],
        )
    )
    register_adapter(
        ProviderBackedMarketDataAdapter(
            source_manifest("binance_usdm_futures_research", AssetClass.CRYPTO),
            BinanceUsdmFuturesProvider(
                timeout=s.request_timeout, allowed_symbols={"BTCUSDT", "ETHUSDT"}
            ),
        )
    )
    register_adapter(
        ProviderBackedMarketDataAdapter(
            source_manifest("hyperliquid_perpetual_public", AssetClass.CRYPTO),
            HyperliquidPerpetualProvider(timeout=s.request_timeout),
        )
    )

    # A-share is always represented by the MVP adapter. Without both an
    # operator token and an entitlement receipt, fetches remain typed
    # blocked_for_entitlement and cannot write/promote candles.
    entitlement = None
    if s.tushare_entitlement_path:
        entitlement = TuShareEntitlement.from_json_file(s.tushare_entitlement_path)
    _providers[AssetClass.A_SHARE] = TuShareMvpProvider(
        s.tushare_token,
        entitlement=entitlement,
        manifest=Path(__file__).resolve().parents[2] / "configs" / "mvp_manifest.json",
    )
    register_adapter(
        ProviderBackedMarketDataAdapter(
            source_manifest("tushare_pro", AssetClass.A_SHARE),
            _providers[AssetClass.A_SHARE],
        )
    )

    external_adapters = load_configured_adapters(s.adapter_config_path)
    if s.load_entrypoint_adapters:
        external_adapters.extend(load_entrypoint_adapters())
    for adapter in external_adapters:
        register_adapter(adapter)


def get_store() -> KlineStore:
    if _store is None:
        init()
    return _store  # type: ignore[return-value]


def get_provider(asset_class: AssetClass) -> Provider:
    if not _providers:
        init()
    provider = _providers.get(asset_class)
    if provider is None:
        if asset_class == AssetClass.A_SHARE:
            raise ProviderError(
                "A-share provider not configured",
                suggestions=["Set KLINE_TUSHARE_TOKEN in .env"],
            )
        raise ProviderError(f"No provider for {asset_class.value}")
    return provider


def get_live_provider(source_mode: str = "binance_usdm_futures") -> Provider:
    if not _live_providers:
        init()
    provider = _live_providers.get(source_mode)
    if provider is None:
        raise ProviderError(f"No live provider for {source_mode}")
    return provider


def register_adapter(adapter: MarketDataPort) -> None:
    """Register a source adapter. This is the broker/plugin extension point."""
    source_id = adapter.manifest.source_id
    if source_id in _adapters:
        raise ProviderError(f"Duplicate source adapter: {source_id}")
    _adapters[source_id] = adapter
    register_source_manifest(adapter.manifest)


def get_adapter_for_source(source: str, asset_class: AssetClass) -> MarketDataPort:
    try:
        normalized = normalize_source(source, asset_class)
        manifest = source_manifest(normalized, asset_class)
    except (KeyError, ValueError) as e:
        raise ProviderError(
            f"Unknown source: {source}",
            suggestions=[
                "Use auto, tencent_kline, treasury_official_csv, "
                "sina_index, "
                "treasury_official_csv_derived, binance_spot_public, "
                "binance_usdm_futures, binance_usdm_futures_research, "
                "hyperliquid_perpetual_public, yahoo_finance, yahoo_finance_futures, "
                "tushare_pro, or a registered adapter source"
            ],
        ) from e

    adapter = _adapters.get(normalized)
    if adapter is None:
        raise ProviderError(
            f"Source adapter not configured: {normalized}",
            suggestions=[
                f"Register an adapter for {normalized}",
                "For A-share equity data, set KLINE_TUSHARE_TOKEN if source=tushare_pro",
            ],
        )
    if adapter.manifest.asset_class != manifest.asset_class:
        raise ProviderError(
            f"Source {normalized} is for {adapter.manifest.asset_class.value}, "
            f"not {asset_class.value}",
        )
    return adapter


def get_provider_for_source(source: str, asset_class: AssetClass) -> Provider:
    """Return a source adapter for backward-compatible callers."""
    return get_adapter_for_source(source, asset_class)  # type: ignore[return-value]


def provider_status() -> dict:
    if not _adapters:
        init()

    sources: dict[str, dict] = {}
    for source_id, manifest in all_source_manifests().items():
        meta = manifest.meta
        adapter = _adapters.get(source_id)
        if manifest.symbol_timeframes:
            common = set.intersection(
                *(set(timeframes) for timeframes in manifest.symbol_timeframes.values())
            )
            supported_timeframes = [
                timeframe.value for timeframe in Timeframe if timeframe in common
            ]
        else:
            supported_timeframes = (
                [timeframe.value for timeframe in adapter.supported_timeframes()] if adapter else []
            )
        sources[source_id] = {
            "available": False,
            "configured": adapter is not None,
            "availability_basis": "not_live_probed",
            "asset_class": manifest.asset_class.value,
            "provider": meta.name,
            "source_mode": meta.source_mode,
            "market_type": meta.market_type,
            "realtime_supported": meta.realtime_supported,
            "execution_venue": meta.execution_venue,
            "supported_symbols": list(meta.supported_symbols),
            "supported_timeframes": supported_timeframes,
            "supported_timeframes_by_symbol": {
                symbol: [timeframe.value for timeframe in timeframes]
                for symbol, timeframes in manifest.symbol_timeframes.items()
            },
            "quality_flags": list(meta.quality_flags),
        }

    return {"sources": sources}


def runtime_status() -> dict[str, str]:
    """Return runtime/build identity without probing or mutating external state."""

    from kline import __version__

    settings = _settings or get_settings()
    build_sha = os.environ.get("KLINE_BUILD_SHA", "").strip() or "unknown"
    runtime_root = os.environ.get("KLINE_RUNTIME_ROOT", "").strip()
    return {
        "service_version": __version__,
        "runtime_root": runtime_root or str(Path.cwd().resolve()),
        "module_root": str(Path(__file__).resolve().parents[2]),
        "working_directory": str(Path.cwd().resolve()),
        "build_sha": build_sha,
        "registry_version": PHASE1_SOURCE_REGISTRY_VERSION,
        "database_path": str(Path(settings.db_path).resolve()),
        "identity_status": "declared" if build_sha != "unknown" else "unknown",
    }
