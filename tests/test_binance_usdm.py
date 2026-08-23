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


def _exchange_info() -> dict:
    return {
        "serverTime": 1783667819466,
        "symbols": [
            {
                "symbol": "XAUUSDT",
                "contractType": "TRADIFI_PERPETUAL",
                "status": "TRADING",
                "maintMarginPercent": "2.5000",
                "requiredMarginPercent": "5.0000",
                "baseAsset": "XAU",
                "quoteAsset": "USDT",
                "marginAsset": "USDT",
                "pricePrecision": 2,
                "quantityPrecision": 3,
                "filters": [
                    {
                        "filterType": "PRICE_FILTER",
                        "tickSize": "0.01",
                        "minPrice": "0.01",
                        "maxPrice": "200000",
                    },
                    {
                        "filterType": "LOT_SIZE",
                        "stepSize": "0.001",
                        "minQty": "0.001",
                        "maxQty": "10000",
                    },
                    {
                        "filterType": "MARKET_LOT_SIZE",
                        "stepSize": "0.001",
                        "minQty": "0.001",
                        "maxQty": "1000",
                    },
                    {"filterType": "MIN_NOTIONAL", "notional": "5"},
                ],
                "orderTypes": ["LIMIT", "MARKET", "STOP_MARKET", "TAKE_PROFIT_MARKET"],
                "timeInForce": ["GTC", "IOC", "FOK"],
            }
        ],
    }


@pytest.mark.parametrize(
    ("timeframe", "interval"),
    [(Timeframe.MIN_1, "1m"), (Timeframe.MIN_5, "5m"), (Timeframe.DAY, "1d")],
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
    assert candles[0].timestamp == "2026-07-09T10:00:00+00:00"
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


@pytest.mark.parametrize("ticker", ["BTC", "BTCUSDT", "ETH", "ETHUSDT"])
async def test_research_provider_accepts_explicit_crypto_futures_symbols(ticker: str):
    seen_params: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_params.update(dict(request.url.params))
        return httpx.Response(200, json=[_kline(_open_ms(10, 0), "100.0")])

    provider = BinanceUsdmFuturesProvider(
        transport=httpx.MockTransport(handler),
        allowed_symbols={"BTCUSDT", "ETHUSDT"},
    )
    candles = await provider.fetch(ticker, Timeframe.HOUR_4, limit=1)

    assert seen_params["symbol"] == ("BTCUSDT" if ticker.startswith("BTC") else "ETHUSDT")
    assert seen_params["interval"] == "4h"
    assert candles[0].close == 3300.70


async def test_xauusdt_instrument_definition_preserves_exchange_constraints():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/fapi/v1/exchangeInfo"
        assert request.url.params["symbol"] == "XAUUSDT"
        return httpx.Response(200, json=_exchange_info())

    provider = BinanceUsdmFuturesProvider(transport=httpx.MockTransport(handler))
    definition = await provider.fetch_instrument_definition("GOLD")

    assert definition.schema_version == "instrument-definition-v1"
    assert definition.instrument_id == "XAUUSDT.BINANCE"
    assert definition.contract_type == "TRADIFI_PERPETUAL"
    assert definition.base_currency == "XAU"
    assert definition.quote_currency == "USDT"
    assert definition.settlement_currency == "USDT"
    assert definition.is_inverse is False
    assert definition.price_precision == 2
    assert definition.price_increment == "0.01"
    assert definition.size_precision == 3
    assert definition.size_increment == "0.001"
    assert definition.min_quantity == "0.001"
    assert definition.min_notional == "5"
    assert definition.margin_init_rate == "0.0500"
    assert definition.margin_maint_rate == "0.0250"
    assert definition.is_synthetic is False
    assert definition.contract_multiplier == "1"
    assert definition.missing_fields == ["maker_fee_rate", "taker_fee_rate"]
    assert definition.derived_fields["contract_multiplier"] == (
        "usd_m_notional_equals_price_times_quantity"
    )
    assert provider.last_raw_response is not None
    assert provider.last_raw_response["response_body"]["serverTime"] == 1783667819466


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
