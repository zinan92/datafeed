"""Tests for Binance USD-M Futures provider normalization."""

from __future__ import annotations

from datetime import datetime, timezone
import asyncio

import httpx
import pytest

from kline.models import Timeframe
from kline.providers.binance_usdm import BinanceUsdmFuturesProvider
from kline.providers.base import ProviderError


def _open_ms(hour: int, minute: int) -> int:
    dt = datetime(2026, 7, 9, hour, minute, tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _kline(open_time_ms: int, open_price: str = "3300.10") -> list:
    return [
        open_time_ms,
        open_price,
        "3301.20",
        "3299.90",
        "3300.70",
        "12.5",
        open_time_ms + 59_999,
        "41258.75",
        42,
        "6.2",
        "20460.11",
        "0",
    ]


@pytest.mark.parametrize(
    ("timeframe", "interval"),
    [(Timeframe.MIN_1, "1m"), (Timeframe.MIN_5, "5m")],
)
async def test_xauusdt_rest_normalizes_to_standard_candles(timeframe: Timeframe, interval: str):
    seen_params: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_params.update(dict(request.url.params))
        return httpx.Response(200, json=[_kline(_open_ms(10, 0))])

    provider = BinanceUsdmFuturesProvider(transport=httpx.MockTransport(handler))
    candles = await provider.fetch("XAUUSDT", timeframe, limit=10)

    assert seen_params["symbol"] == "XAUUSDT"
    assert seen_params["interval"] == interval
    assert candles[0].timestamp == "2026-07-09T10:00:00"
    assert candles[0].open == 3300.10
    assert candles[0].high == 3301.20
    assert candles[0].low == 3299.90
    assert candles[0].close == 3300.70
    assert candles[0].volume == 12.5
    assert candles[0].amount == 41258.75
    assert provider.last_raw_response is not None
    assert provider.last_raw_response["response_body"][0][0] == _open_ms(10, 0)


async def test_binance_usdm_rejects_non_xau_symbols():
    provider = BinanceUsdmFuturesProvider(transport=httpx.MockTransport(lambda _: httpx.Response(200)))

    with pytest.raises(ProviderError) as exc:
        await provider.fetch("BTCUSDT", Timeframe.MIN_1)

    assert "not enabled" in str(exc.value)


class _FakeWebSocket:
    def __init__(self, messages: list[str] | None = None) -> None:
        self.messages = list(messages or [])

    async def recv(self) -> str:
        if self.messages:
            return self.messages.pop(0)
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class _FakeWebSocketContext:
    def __init__(self, socket: _FakeWebSocket) -> None:
        self.socket = socket

    async def __aenter__(self) -> _FakeWebSocket:
        return self.socket

    async def __aexit__(self, *_args) -> None:
        return None


async def test_binance_usdm_stream_fails_visibly_when_upstream_is_silent():
    provider = BinanceUsdmFuturesProvider(
        timeout=0.01,
        websocket_connect=lambda *_args, **_kwargs: _FakeWebSocketContext(_FakeWebSocket()),
    )

    with pytest.raises(ProviderError, match="no kline update"):
        await anext(provider.stream("XAUUSDT", Timeframe.MIN_1))
