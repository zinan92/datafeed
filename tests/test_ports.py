"""Tests for the market-data port/adapter extension point."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator

from kline.api import get_candles
from kline.models import AssetClass, CachePolicy, Candle, FallbackPolicy, QualityPolicy, Timeframe
from kline.ports import ProviderMeta, SourceManifest
from kline.providers.base import ProviderError
from kline.registry import register_adapter
from kline.store import KlineStore


def _fresh_candle(close: float = 500.0) -> Candle:
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    ts = now.strftime("%Y-%m-%dT%H:%M:%S")
    return Candle(timestamp=ts, open=close, high=close + 1, low=close - 1, close=close, volume=10)


class FakeBrokerAdapter:
    def __init__(self) -> None:
        self.manifest = SourceManifest(
            source_id="fake_broker_feed",
            asset_class=AssetClass.CRYPTO,
            meta=ProviderMeta(
                name="fake_broker",
                source_mode="fake_broker_feed",
                quality_flags=("broker_adapter", "execution_venue"),
                continuous=True,
                execution_venue=True,
                realtime_supported=True,
                market_type="broker_stream",
                supported_symbols=("BTCUSD",),
            ),
            ticker_aliases={"BTC": "BTCUSD"},
        )
        self.received_ticker: str | None = None

    @property
    def last_raw_response(self) -> dict | None:
        return {
            "request_params": {"symbol": self.received_ticker},
            "response_body": {"ok": True},
            "status_code": 200,
            "error": None,
        }

    def canonical_ticker(self, ticker: str) -> str:
        return self.manifest.canonical_ticker(ticker)

    def supported_timeframes(self) -> list[Timeframe]:
        return [Timeframe.MIN_1]

    async def fetch_candles(
        self,
        ticker: str,
        timeframe: Timeframe,
        *,
        start: str | None = None,
        end: str | None = None,
        limit: int = 500,
    ) -> list[Candle]:
        self.received_ticker = ticker
        return [_fresh_candle(123.0)]

    async def stream_candles(
        self,
        ticker: str,
        timeframe: Timeframe,
    ) -> AsyncIterator[Candle]:
        raise ProviderError("not used")


async def test_registered_broker_adapter_fits_without_api_changes(monkeypatch, tmp_path: Path):
    store = KlineStore(str(tmp_path / "test.db"))
    adapter = FakeBrokerAdapter()
    register_adapter(adapter)
    monkeypatch.setattr("kline.api.get_store", lambda: store)

    response = await get_candles(
        AssetClass.CRYPTO,
        "BTC",
        timeframe=Timeframe.MIN_1,
        start=None,
        end=None,
        limit=1,
        refresh=False,
        source="fake_broker_feed",
        cache_policy=CachePolicy.BYPASS,
        quality=QualityPolicy.STANDARD,
        fallback_policy=FallbackPolicy.NONE,
        require_execution_venue=True,
        profile=None,
        strict=False,
        mode="research",
    )

    assert adapter.received_ticker == "BTCUSD"
    assert response.provider == "fake_broker"
    assert response.source_mode == "fake_broker_feed"
    assert response.execution_venue is True
    assert response.require_execution_venue is True
    assert response.candles[0].close == 123.0
