"""Provider and store registry — wired once at startup."""

from __future__ import annotations

from kline.config import Settings, ensure_data_dir, get_settings
from kline.models import AssetClass
from kline.plugin_loader import load_configured_adapters, load_entrypoint_adapters
from kline.ports import MarketDataPort, ProviderBackedMarketDataAdapter
from kline.providers.ashare import AShareProvider
from kline.providers.base import Provider, ProviderError
from kline.providers.binance_usdm import BinanceUsdmFuturesProvider
from kline.providers.commodity import CommodityProvider
from kline.providers.crypto import CryptoProvider
from kline.providers.fred import FredCsvProvider
from kline.providers.us import USStockProvider
from kline.provenance import (
    all_source_manifests,
    normalize_source,
    register_source_manifest,
    source_manifest,
    fred_source_manifest,
)
from kline.store import KlineStore

_store: KlineStore | None = None
_providers: dict[AssetClass, Provider] = {}
_live_providers: dict[str, Provider] = {}
_adapters: dict[str, MarketDataPort] = {}


def init(settings: Settings | None = None) -> None:
    """Initialize store and providers. Called once at app startup."""
    global _store, _providers, _live_providers, _adapters
    s = settings or get_settings()
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
            source_manifest("yahoo_finance_etf", AssetClass.ETF),
            _providers[AssetClass.ETF],
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

    # A-share requires TuShare token
    if s.tushare_token:
        _providers[AssetClass.A_SHARE] = AShareProvider(s.tushare_token)
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
                "Use auto, binance_spot_public, binance_usdm_futures, yahoo_finance, "
                "yahoo_finance_futures, tushare_pro, or a registered adapter source"
            ],
        ) from e

    adapter = _adapters.get(normalized)
    if adapter is None:
        raise ProviderError(
            f"Source adapter not configured: {normalized}",
            suggestions=[
                f"Register an adapter for {normalized}",
                "For A-share data, set KLINE_TUSHARE_TOKEN if source=tushare_pro",
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
        sources[source_id] = {
            "available": adapter is not None,
            "asset_class": manifest.asset_class.value,
            "provider": meta.name,
            "source_mode": meta.source_mode,
            "market_type": meta.market_type,
            "realtime_supported": meta.realtime_supported,
            "execution_venue": meta.execution_venue,
            "supported_symbols": list(meta.supported_symbols),
            "supported_timeframes": [t.value for t in adapter.supported_timeframes()]
            if adapter
            else [],
            "quality_flags": list(meta.quality_flags),
        }

    return {"sources": sources}
