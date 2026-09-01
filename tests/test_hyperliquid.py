from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from kline.models import Timeframe
from kline.providers.base import ProviderError
from kline.providers.hyperliquid import HyperliquidPerpetualProvider


def _row(open_ms: int, close: str = "60.0") -> dict[str, object]:
    return {
        "t": open_ms,
        "T": open_ms + 4 * 60 * 60 * 1000 - 1,
        "s": "HYPE",
        "i": "4h",
        "o": "59.0",
        "c": close,
        "h": "61.0",
        "l": "58.5",
        "v": "100.0",
        "n": 10,
    }


def _row_for_interval(open_ms: int, interval: str, close: str = "60.0") -> dict[str, object]:
    minutes = {"15m": 15, "30m": 30, "1h": 60, "4h": 240}[interval]
    return {
        **_row(open_ms, close),
        "T": open_ms + minutes * 60 * 1000 - 1,
        "i": interval,
    }


@pytest.mark.asyncio
async def test_hyperliquid_native_4h_preserves_perpetual_identity():
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["type"] == "candleSnapshot"
        assert payload["req"]["coin"] == "HYPE"
        assert payload["req"]["interval"] == "4h"
        return httpx.Response(200, json=[_row(1786838400000)])

    provider = HyperliquidPerpetualProvider(transport=httpx.MockTransport(handler))
    candles = await provider.fetch("HYPE", Timeframe.HOUR_4, limit=1)

    assert len(candles) == 1
    assert provider.timeframe_transform is not None
    assert provider.timeframe_transform.timeframe_origin == "native"
    assert provider.source_identity == {
        "provider_symbol": "HYPE",
        "venue": "hyperliquid",
        "contract_type": "perpetual",
        "settlement": "USDC",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("symbol", ["BTC", "ETH", "HYPE"])
async def test_hyperliquid_native_15m_is_available_for_mvp_crypto_source(symbol: str):
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["req"]["coin"] == symbol
        assert payload["req"]["interval"] == "15m"
        return httpx.Response(200, json=[_row_for_interval(1786838400000, "15m")])

    provider = HyperliquidPerpetualProvider(transport=httpx.MockTransport(handler))
    candles = await provider.fetch(symbol, Timeframe.MIN_15, limit=1)

    assert len(candles) == 1
    assert provider.timeframe_transform is not None
    assert provider.timeframe_transform.raw_timeframe == Timeframe.MIN_15
    assert provider.timeframe_transform.timeframe_origin == "native"


@pytest.mark.asyncio
@pytest.mark.parametrize("symbol", ["BTC", "ETH", "HYPE"])
async def test_hyperliquid_native_1h_is_available_for_mvp_crypto_source(symbol: str):
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["req"]["coin"] == symbol
        assert payload["req"]["interval"] == "1h"
        return httpx.Response(200, json=[_row_for_interval(1786838400000, "1h")])

    provider = HyperliquidPerpetualProvider(transport=httpx.MockTransport(handler))
    candles = await provider.fetch(symbol, Timeframe.HOUR_1, limit=1)

    assert len(candles) == 1
    assert provider.timeframe_transform is not None
    assert provider.timeframe_transform.raw_timeframe == Timeframe.HOUR_1
    assert provider.timeframe_transform.timeframe_origin == "native"


@pytest.mark.asyncio
@pytest.mark.parametrize("symbol", ["BTC", "ETH", "HYPE"])
async def test_hyperliquid_native_30m_is_available_for_unified_crypto_source(symbol: str):
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["req"]["coin"] == symbol
        assert payload["req"]["interval"] == "30m"
        return httpx.Response(200, json=[_row_for_interval(1786838400000, "30m")])

    provider = HyperliquidPerpetualProvider(transport=httpx.MockTransport(handler))
    candles = await provider.fetch(symbol, Timeframe.MIN_30, limit=1)

    assert len(candles) == 1
    assert provider.timeframe_transform is not None
    assert provider.timeframe_transform.raw_timeframe == Timeframe.MIN_30
    assert provider.timeframe_transform.timeframe_origin == "native"


@pytest.mark.asyncio
async def test_hyperliquid_rejects_unlisted_symbol():
    provider = HyperliquidPerpetualProvider(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=[]))
    )
    with pytest.raises(ProviderError, match="does not enable SOL"):
        await provider.fetch("SOL", Timeframe.HOUR_4)


@pytest.mark.asyncio
@pytest.mark.parametrize("symbol", ["BTC", "ETH", "HYPE"])
async def test_hyperliquid_unified_source_accepts_all_weekly_crypto_symbols(symbol: str):
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["req"]["coin"] == symbol
        return httpx.Response(200, json=[_row(1786838400000)])

    provider = HyperliquidPerpetualProvider(transport=httpx.MockTransport(handler))
    candles = await provider.fetch(symbol, Timeframe.HOUR_4, limit=1)

    assert len(candles) == 1
    assert provider.source_identity["provider_symbol"] == symbol


@pytest.mark.asyncio
async def test_hyperliquid_weekly_bar_is_dated_on_completed_friday():
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["req"]["interval"] == "1d"
        start = datetime(2026, 8, 10, tzinfo=timezone.utc)
        rows = []
        for offset in range(7):
            stamp = start + timedelta(days=offset)
            rows.append(
                {
                    **_row(int(stamp.timestamp() * 1000)),
                    "T": int((stamp + timedelta(days=1)).timestamp() * 1000) - 1,
                    "i": "1d",
                }
            )
        return httpx.Response(200, json=rows)

    provider = HyperliquidPerpetualProvider(transport=httpx.MockTransport(handler))
    candles = await provider.fetch("HYPE", Timeframe.WEEK, limit=1)

    assert candles[0].timestamp == "2026-08-14"
