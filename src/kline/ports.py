"""Ports for market-data adapters.

The domain talks to this module, not directly to Binance/Yahoo/TuShare classes.
New brokers should fit by providing a SourceManifest plus a MarketDataPort
implementation.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Mapping, Protocol

from kline.models import AssetClass, Candle, InstrumentDefinition, Timeframe, TimeframeTransform
from kline.providers.base import Provider, ProviderError
from kline.storage import StoragePort

__all__ = [
    "FetchReceipt",
    "MarketDataPort",
    "ProviderBackedMarketDataAdapter",
    "ProviderMeta",
    "SourceManifest",
    "StoragePort",
]


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
    symbol_timeframes: Mapping[str, tuple[Timeframe, ...]] = field(default_factory=dict)
    blocked_timeframes: tuple[Timeframe, ...] = ()
    enforce_symbol_allowlist: bool = False

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

    def supports_timeframe(self, ticker: str, timeframe: Timeframe) -> bool:
        """Return whether this source explicitly serves a symbol/timeframe pair."""

        normalized = self.canonical_ticker(ticker).upper().strip()
        if timeframe in self.blocked_timeframes:
            return False
        allowed = self.symbol_timeframes.get(normalized)
        if self.symbol_timeframes:
            if allowed is None:
                return not self.enforce_symbol_allowlist and timeframe != Timeframe.HOUR_4
            return timeframe in allowed
        return True


@dataclass(frozen=True)
class FetchReceipt:
    """Immutable per-request result and provenance snapshot."""

    candles: list[Candle]
    timeframe_transform: TimeframeTransform | None
    source_identity: Mapping[str, Any]
    raw_response: dict[str, Any] | None
    attempts: tuple[Mapping[str, Any], ...] = ()


class MarketDataPort(Protocol):
    """Port every market-data adapter must implement."""

    @property
    def manifest(self) -> SourceManifest: ...

    @property
    def last_raw_response(self) -> dict[str, Any] | None: ...

    @property
    def timeframe_transform(self) -> TimeframeTransform | None: ...

    @property
    def source_identity(self) -> Mapping[str, Any] | None: ...

    def canonical_ticker(self, ticker: str) -> str: ...

    def supported_timeframes(self) -> list[Timeframe]: ...

    async def fetch_candles(
        self,
        ticker: str,
        timeframe: Timeframe,
        *,
        start: str | None = None,
        end: str | None = None,
        limit: int = 500,
    ) -> list[Candle]: ...

    async def fetch_candles_with_receipt(
        self,
        ticker: str,
        timeframe: Timeframe,
        *,
        start: str | None = None,
        end: str | None = None,
        limit: int = 500,
    ) -> FetchReceipt: ...

    async def stream_candles(
        self,
        ticker: str,
        timeframe: Timeframe,
    ) -> AsyncIterator[Candle]: ...

    async def fetch_instrument_definition(self, ticker: str) -> InstrumentDefinition: ...


class ProviderBackedMarketDataAdapter:
    """Adapter wrapper for the existing provider classes."""

    def __init__(self, manifest: SourceManifest, provider: Provider) -> None:
        self._manifest = manifest
        self._provider = provider
        self._fetch_lock = asyncio.Lock()

    @property
    def manifest(self) -> SourceManifest:
        return self._manifest

    @property
    def last_raw_response(self) -> dict[str, Any] | None:
        raw = getattr(self._provider, "last_raw_response", None)
        return raw if isinstance(raw, dict) else None

    @property
    def timeframe_transform(self) -> TimeframeTransform | None:
        value = getattr(self._provider, "timeframe_transform", None)
        return value if isinstance(value, TimeframeTransform) else None

    @property
    def source_identity(self) -> Mapping[str, Any] | None:
        value = getattr(self._provider, "source_identity", None)
        return value if isinstance(value, Mapping) else None

    @property
    def last_attempts(self) -> tuple[Mapping[str, Any], ...]:
        value = getattr(self._provider, "last_attempts", ()) or ()
        return tuple(item for item in value if isinstance(item, Mapping))

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
        return (
            await self.fetch_candles_with_receipt(
                ticker,
                timeframe,
                start=start,
                end=end,
                limit=limit,
            )
        ).candles

    async def fetch_candles_with_receipt(
        self,
        ticker: str,
        timeframe: Timeframe,
        *,
        start: str | None = None,
        end: str | None = None,
        limit: int = 500,
    ) -> FetchReceipt:
        async with self._fetch_lock:
            canonical = self.canonical_ticker(ticker)
            candles = await self._provider.fetch(
                canonical,
                timeframe,
                start=start,
                end=end,
                limit=limit,
            )
            return FetchReceipt(
                candles=candles,
                timeframe_transform=self.timeframe_transform,
                source_identity=dict(self.source_identity or {}),
                raw_response=dict(self.last_raw_response or {}) if self.last_raw_response else None,
                attempts=tuple(getattr(self._provider, "last_attempts", ()) or ()),
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
