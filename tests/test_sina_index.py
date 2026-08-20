"""Focused contract tests for the Sina A-share index fallback source."""

from __future__ import annotations

from datetime import date

import httpx
import pytest

from kline.models import Timeframe
from kline.providers.base import ProviderError
from kline.providers.sina import SinaIndexProvider
from kline.provenance import source_manifest
from kline.models import AssetClass


def _row(day: str, value: float = 100.0) -> dict[str, str]:
    return {
        "day": day,
        "open": str(value),
        "high": str(value + 2),
        "low": str(value - 1),
        "close": str(value + 1),
        "volume": "1000",
    }


def _transport(rows: list[dict[str, str]]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["symbol"] == "sh000001"
        assert request.url.params["scale"] == "240"
        assert request.url.params["ma"] == "no"
        return httpx.Response(200, json=rows, request=request)

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
@pytest.mark.parametrize("symbol", ["sh000001", "sh000688", "sh000015"])
async def test_sina_index_daily_is_native_and_excludes_today(symbol: str):
    rows = [_row("2026-08-18"), _row("2026-08-19"), _row("2026-08-20")]
    provider = SinaIndexProvider(
        transport=_transport(rows),
        today=lambda: date(2026, 8, 20),
    )

    # The transport fixture is intentionally bound to sh000001; the symbol
    # allowlist itself is exercised for all three public index symbols below.
    if symbol != "sh000001":
        provider = SinaIndexProvider(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=rows, request=request)
            ),
            today=lambda: date(2026, 8, 20),
        )
    candles = await provider.fetch(symbol, Timeframe.DAY, limit=10)

    assert [item.timestamp for item in candles] == ["2026-08-18", "2026-08-19"]
    assert provider.source_identity["source_id"] == "sina_index"
    assert provider.source_identity["provider_symbol"] == symbol
    assert provider.timeframe_transform is not None
    assert provider.timeframe_transform.raw_timeframe == Timeframe.DAY
    assert provider.timeframe_transform.timeframe_origin == "native"
    assert provider.last_raw_response["request_params"]["scale"] == 240


@pytest.mark.asyncio
async def test_sina_index_weekly_aggregates_only_completed_weeks():
    rows = [
        _row("2026-08-10", 100),
        _row("2026-08-11", 101),
        _row("2026-08-12", 102),
        _row("2026-08-13", 103),
        _row("2026-08-14", 104),
        _row("2026-08-17", 105),
        _row("2026-08-18", 106),
        _row("2026-08-19", 107),
        _row("2026-08-20", 108),
    ]
    provider = SinaIndexProvider(
        transport=_transport(rows),
        today=lambda: date(2026, 8, 20),
    )

    candles = await provider.fetch(
        "sh000001",
        Timeframe.WEEK,
        start="2026-08-10",
        end="2026-08-21",
        limit=10,
    )

    assert [item.timestamp for item in candles] == ["2026-08-14"]
    assert candles[0].open == 100
    assert candles[0].close == 105
    assert provider.timeframe_transform is not None
    assert provider.timeframe_transform.raw_timeframe == Timeframe.DAY
    assert provider.timeframe_transform.timeframe_origin == "aggregated"
    assert provider.timeframe_transform.aggregation["input_source"] == {
        "source_id": "sina_index",
        "provider_symbol": "sh000001",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rows",
    [
        [{"day": "2026-08-19", "open": "bad"}],
        [_row("2026-08-19"), _row("2026-08-18")],
    ],
)
async def test_sina_index_fails_closed_on_bad_rows(rows):
    provider = SinaIndexProvider(
        transport=_transport(rows),
        today=lambda: date(2026, 8, 20),
    )

    with pytest.raises(ProviderError):
        await provider.fetch("sh000001", Timeframe.DAY, limit=10)

    assert provider.last_raw_response["error"]


def test_sina_index_manifest_is_explicit_and_timeframe_bound():
    manifest = source_manifest("sina_index", AssetClass.INDEX)

    assert manifest.meta.source_mode == "sina_index"
    assert manifest.meta.name == "sina_finance"
    assert manifest.supports_timeframe("sh000001", Timeframe.DAY)
    assert manifest.supports_timeframe("sh000688", Timeframe.WEEK)
    assert not manifest.supports_timeframe("sh000001", Timeframe.HOUR_4)
