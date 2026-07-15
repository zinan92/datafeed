"""Ports for market-data adapters.

The domain talks to this module, not directly to Binance/Yahoo/TuShare classes.
New brokers should fit by providing a SourceManifest plus a MarketDataPort
implementation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Mapping, Protocol

from kline.models import AssetClass, Candle, InstrumentDefinition, Timeframe
from kline.providers.base import Provider, ProviderError


@dataclass(frozen=True)
class ProviderMeta:
    """Identity and trust semantics for a source."""

    name: str
    source_mode: str
    quality_flags: tuple[str, ...]
    continuous: bool
    execution_venue: bool = False
    realtime_supported: bool = False
    market_type: str = ""
    supported_symbols: tuple[str, ...] = ()


@dataclass(frozen=True)
class SourceManifest:
    """Stable capability manifest for one data source adapter."""

    source_id: str
    asset_class: AssetClass
    meta: ProviderMeta
    default_for_asset_class: bool = False
    ticker_aliases: Mapping[str, str] = field(default_factory=dict)
    canonical_instrument_ids: Mapping[str, str] = field(default_factory=dict)

    def canonical_ticker(self, ticker: str) -> str:
        normalized = ticker.upper().strip()
        return self.ticker_aliases.get(normalized, ticker)

    def canonical_instrument_id(self, ticker: str) -> str:
        normalized = ticker.upper().strip()
        provider_symbol = self.canonical_ticker(normalized).upper().strip()
        return self.canonical_instrument_ids.get(
            normalized,
            self.canonical_instrument_ids.get(provider_symbol, normalized),
        )


class MarketDataPort(Protocol):
    """Port every market-data adapter must implement."""

    @property
    def manifest(self) -> SourceManifest:
        ...

    @property
    def last_raw_response(self) -> dict[str, Any] | None:
        ...

    def canonical_ticker(self, ticker: str) -> str:
        ...

    def supported_timeframes(self) -> list[Timeframe]:
        ...

    async def fetch_candles(
        self,
        ticker: str,
        timeframe: Timeframe,
        *,
        start: str | None = None,
        end: str | None = None,
        limit: int = 500,
    ) -> list[Candle]:
        ...

    async def stream_candles(
        self,
        ticker: str,
        timeframe: Timeframe,
    ) -> AsyncIterator[Candle]:
        ...

    async def fetch_instrument_definition(self, ticker: str) -> InstrumentDefinition:
        ...


class ProviderBackedMarketDataAdapter:
    """Adapter wrapper for the existing provider classes."""

    def __init__(self, manifest: SourceManifest, provider: Provider) -> None:
        self._manifest = manifest
        self._provider = provider

    @property
    def manifest(self) -> SourceManifest:
        return self._manifest

    @property
    def last_raw_response(self) -> dict[str, Any] | None:
        raw = getattr(self._provider, "last_raw_response", None)
        return raw if isinstance(raw, dict) else None

    def canonical_ticker(self, ticker: str) -> str:
        return self._manifest.canonical_ticker(ticker)

    def supported_timeframes(self) -> list[Timeframe]:
        return self._provider.supported_timeframes()

    async def fetch_candles(
        self,
        ticker: str,
        timeframe: Timeframe,
        *,
        start: str | None = None,
        end: str | None = None,
        limit: int = 500,
    ) -> list[Candle]:
        canonical = self.canonical_ticker(ticker)
        return await self._provider.fetch(
            canonical,
            timeframe,
            start=start,
            end=end,
            limit=limit,
        )

    async def stream_candles(
        self,
        ticker: str,
        timeframe: Timeframe,
    ) -> AsyncIterator[Candle]:
        stream = getattr(self._provider, "stream", None)
        if stream is None:
            raise ProviderError(
                f"Source {self.manifest.source_id} does not support streaming",
                suggestions=["Use REST candles or choose a realtime streaming source"],
            )
        canonical = self.canonical_ticker(ticker)
        async for candle in stream(canonical, timeframe):
            yield candle

    async def fetch_instrument_definition(self, ticker: str) -> InstrumentDefinition:
        fetch = getattr(self._provider, "fetch_instrument_definition", None)
        if fetch is None:
            raise ProviderError(
                f"Source {self.manifest.source_id} does not expose instrument definitions",
                suggestions=["Choose an execution source with instrument metadata"],
            )
        canonical = self.canonical_ticker(ticker)
        return await fetch(canonical)
