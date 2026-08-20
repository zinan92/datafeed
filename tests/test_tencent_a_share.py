"""Focused contract tests for the Tencent A-share index source."""

from __future__ import annotations

from datetime import date
import httpx
import pytest
from fastapi import HTTPException

from kline.api import get_candles
from kline.models import (
    AssetClass,
    CachePolicy,
    Candle,
    FallbackPolicy,
    QualityPolicy,
    Timeframe,
    TimeframeTransform,
)
from kline.ports import ProviderBackedMarketDataAdapter
from kline.providers.ashare import TencentIndexProvider
from kline.providers.base import ProviderError
from kline.provenance import canonical_ticker_for_source, source_manifest


def _rows(*dates: str) -> list[list[str]]:
    return [[stamp, "100", "101", "102", "99", "1000"] for stamp in dates]


def _handler(rows: list[list[str]], *, response_symbol: str | None = None):
    def handle(request: httpx.Request) -> httpx.Response:
        symbol = request.url.params["param"].split(",", 1)[0]
        returned_symbol = response_symbol or symbol
        return httpx.Response(
            200,
            json={
                "code": 0,
                "msg": "",
                "data": {returned_symbol: {"day": rows}},
            },
            request=request,
        )

    return handle


@pytest.mark.asyncio
@pytest.mark.parametrize("symbol", ["sh000001", "sh000688", "sh000015"])
async def test_tencent_index_daily_keeps_explicit_symbol_and_excludes_today(symbol: str):
    transport = httpx.MockTransport(
        _handler(_rows("2026-08-18", "2026-08-19", "2026-08-20"))
    )
    provider = TencentIndexProvider(
        transport=transport,
        today=lambda: date(2026, 8, 20),
    )

    candles = await provider.fetch(symbol, Timeframe.DAY, limit=10)

    assert [item.timestamp for item in candles] == ["2026-08-18", "2026-08-19"]
    assert provider.source_identity["provider_symbol"] == symbol
    assert provider.timeframe_transform is not None
    assert provider.timeframe_transform.raw_timeframe == Timeframe.DAY
    assert provider.timeframe_transform.timeframe_origin == "native"
    assert provider.timeframe_transform.aggregation["rule"] == "native_passthrough"
    assert provider.last_raw_response["request_params"]["requests"][0]["param"].startswith(
        f"{symbol},day"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("symbol", ["sh000001", "sh000688", "sh000015"])
async def test_tencent_index_weekly_aggregates_only_completed_weeks_with_input_identity(symbol: str):
    transport = httpx.MockTransport(
        _handler(
            _rows(
                "2026-08-10",
                "2026-08-11",
                "2026-08-12",
                "2026-08-13",
                "2026-08-14",
                "2026-08-17",
                "2026-08-18",
                "2026-08-19",
                "2026-08-20",
            )
        )
    )
    provider = TencentIndexProvider(
        transport=transport,
        today=lambda: date(2026, 8, 20),
    )

    candles = await provider.fetch(
        symbol,
        Timeframe.WEEK,
        start="2026-08-10",
        end="2026-08-21",
        limit=10,
    )

    assert [item.timestamp for item in candles] == ["2026-08-14"]
    assert candles[0].open == 100
    assert candles[0].close == 101
    assert provider.timeframe_transform is not None
    assert provider.timeframe_transform.raw_timeframe == Timeframe.DAY
    assert provider.timeframe_transform.timeframe_origin == "aggregated"
    aggregation = provider.timeframe_transform.aggregation
    assert aggregation["rule"] == "completed_iso_week"
    assert aggregation["input_source"] == {
        "source_id": "tencent_kline",
        "provider_symbol": symbol,
    }


@pytest.mark.asyncio
async def test_tencent_index_historical_end_without_start_still_fetches_rows():
    provider = TencentIndexProvider(
        transport=httpx.MockTransport(
            _handler(_rows("2026-08-13", "2026-08-14", "2026-08-15"))
        ),
        today=lambda: date(2026, 8, 20),
    )

    candles = await provider.fetch(
        "sh000001",
        Timeframe.DAY,
        end="2026-08-16",
        limit=10,
    )

    assert [item.timestamp for item in candles] == ["2026-08-13", "2026-08-14", "2026-08-15"]


@pytest.mark.asyncio
async def test_tencent_index_rejects_current_partial_week():
    transport = httpx.MockTransport(
        _handler(_rows("2026-08-17", "2026-08-18", "2026-08-19"))
    )
    provider = TencentIndexProvider(
        transport=transport,
        today=lambda: date(2026, 8, 20),
    )

    with pytest.raises(ProviderError, match="no closed candles"):
        await provider.fetch(
            "sh000001",
            Timeframe.WEEK,
            start="2026-08-17",
            end="2026-08-21",
            limit=10,
        )

    assert provider.last_raw_response["error"]


@pytest.mark.asyncio
async def test_tencent_index_rejects_bare_ambiguous_code_without_http_call():
    called = False

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(500, request=request)

    provider = TencentIndexProvider(transport=httpx.MockTransport(handle))

    with pytest.raises(ProviderError, match="explicit market-prefixed"):
        await provider.fetch("000001", Timeframe.DAY)

    assert called is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("rows", "response_symbol", "message"),
    [
        ([], None, "no daily K-line rows"),
        ([["2026-08-19", "bad", "101", "102", "99", "1000"]], None, "invalid values"),
        (_rows("2026-08-19"), "sz000001", "wrong symbol"),
    ],
)
async def test_tencent_index_fails_closed_on_empty_malformed_or_wrong_symbol(
    rows: list[list[str]], response_symbol: str | None, message: str
):
    provider = TencentIndexProvider(
        transport=httpx.MockTransport(_handler(rows, response_symbol=response_symbol)),
        today=lambda: date(2026, 8, 20),
    )

    with pytest.raises(ProviderError, match=message):
        await provider.fetch("sh000001", Timeframe.DAY, limit=10)

    assert provider.last_raw_response["error"]


@pytest.mark.asyncio
async def test_tencent_index_rejects_out_of_order_rows_instead_of_sorting_them():
    provider = TencentIndexProvider(
        transport=httpx.MockTransport(
            _handler(_rows("2026-08-19", "2026-08-18"))
        ),
        today=lambda: date(2026, 8, 20),
    )

    with pytest.raises(ProviderError, match="out-of-order"):
        await provider.fetch("sh000001", Timeframe.DAY, limit=10)


class _FailingTencentProvider:
    def __init__(self) -> None:
        self.called = False

    def supported_timeframes(self) -> list[Timeframe]:
        return [Timeframe.DAY, Timeframe.WEEK]

    async def fetch(self, *_args, **_kwargs):
        self.called = True
        raise ProviderError("Tencent returned wrong symbol")


class _SinaFallbackProvider:
    def __init__(self) -> None:
        self.last_raw_response = {
            "request_params": {"symbol": "sh000001", "scale": 240},
            "response_body": {"row_count": 1},
            "status_code": 200,
            "error": None,
        }
        self.timeframe_transform = TimeframeTransform(
            raw_timeframe=Timeframe.DAY,
            timeframe_origin="native",
            aggregation={"kind": "none", "rule": "native_passthrough"},
        )
        self.source_identity = {
            "source_id": "sina_index",
            "provider_symbol": "sh000001",
            "endpoint": "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData",
        }

    def supported_timeframes(self) -> list[Timeframe]:
        return [Timeframe.DAY, Timeframe.WEEK]

    async def fetch(self, *_args, **_kwargs):
        return [
            Candle(
                timestamp="2026-08-19",
                open=100,
                high=102,
                low=99,
                close=101,
                volume=1000,
            )
        ]


@pytest.mark.asyncio
async def test_tencent_failure_can_use_explicit_sina_fallback(monkeypatch, tmp_path):
    from kline.store import KlineStore

    store = KlineStore(str(tmp_path / "kline.db"))
    primary = ProviderBackedMarketDataAdapter(
        source_manifest("tencent_kline", AssetClass.INDEX),
        _FailingTencentProvider(),
    )
    fallback = ProviderBackedMarketDataAdapter(
        source_manifest("sina_index", AssetClass.INDEX),
        _SinaFallbackProvider(),
    )

    def adapter_for_source(source, _asset_class):
        return primary if source == "tencent_kline" else fallback

    monkeypatch.setattr("kline.api.get_store", lambda: store)
    monkeypatch.setattr("kline.api.get_adapter_for_source", adapter_for_source)

    response = await get_candles(
        asset_class=AssetClass.INDEX,
        ticker="sh000001",
        timeframe=Timeframe.DAY,
        start=None,
        end=None,
        limit=1,
        refresh=False,
        source="tencent_kline",
        cache_policy=CachePolicy.BYPASS,
        quality=QualityPolicy.STRICT,
        fallback_policy=FallbackPolicy.EXPLICIT,
        fallback_sources=["sina_index"],
        require_execution_venue=False,
        profile=None,
        strict=False,
        mode="research",
    )

    assert response.requested_source == "tencent_kline"
    assert response.selected_source == "sina_index"
    assert response.selection_reason == "explicit_fallback"
    assert response.attempted_sources == ["tencent_kline", "sina_index"]
    assert response.provider == "sina_finance"
    assert response.provider_symbol == "sh000001"
    assert response.source_identity["source_id"] == "sina_index"
    assert any("Tencent returned wrong symbol" in issue for issue in response.access_issues)


@pytest.mark.asyncio
async def test_tencent_api_strict_bypass_none_does_not_read_cache_or_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    from kline.provenance import source_manifest
    from kline.store import KlineStore

    store = KlineStore(str(tmp_path / "kline.db"))
    store.save(
        "sh000001",
        AssetClass.INDEX,
        Timeframe.DAY,
        [Candle(timestamp="2026-08-19", open=1, high=2, low=0, close=1.5, volume=10)],
        source_id="tencent_kline",
    )
    failing_provider = _FailingTencentProvider()
    adapter = ProviderBackedMarketDataAdapter(
        source_manifest("tencent_kline", AssetClass.INDEX),
        failing_provider,
    )
    monkeypatch.setattr("kline.api.get_store", lambda: store)
    monkeypatch.setattr("kline.api.get_adapter_for_source", lambda *_args: adapter)

    with pytest.raises(HTTPException) as exc:
        await get_candles(
            asset_class=AssetClass.INDEX,
            ticker="sh000001",
            timeframe=Timeframe.DAY,
            start=None,
            end=None,
            limit=1,
            refresh=False,
            source="tencent_kline",
            cache_policy=CachePolicy.BYPASS,
            quality=QualityPolicy.STRICT,
            fallback_policy=FallbackPolicy.NONE,
            require_execution_venue=False,
            profile=None,
            strict=False,
            mode="research",
        )

    detail = exc.value.detail
    assert failing_provider.called is True
    assert detail["cache_policy"] == "bypass"
    assert detail["quality_policy"] == "strict"
    assert detail["fallback_policy"] == "none"
    assert detail["attempted_sources"] == ["tencent_kline"]
    assert detail["selected_source"] == "tencent_kline"
    assert detail["provider_symbol"] == "sh000001"
    assert detail["timeframe"] == "1d"
    assert store.query(
        "sh000001", AssetClass.INDEX, Timeframe.DAY, source_id="tencent_kline"
    )[0].close == 1.5


@pytest.mark.asyncio
async def test_tencent_adapter_receipt_preserves_source_and_transform():
    provider = TencentIndexProvider(
        transport=httpx.MockTransport(_handler(_rows("2026-08-18", "2026-08-19"))),
        today=lambda: date(2026, 8, 20),
    )
    manifest = source_manifest("tencent_kline", AssetClass.INDEX)
    adapter = ProviderBackedMarketDataAdapter(manifest, provider)

    receipt = await adapter.fetch_candles_with_receipt("sh000001", Timeframe.DAY, limit=10)

    assert receipt.source_identity["source_id"] == "tencent_kline"
    assert receipt.source_identity["provider_symbol"] == "sh000001"
    assert receipt.timeframe_transform is not None
    assert receipt.raw_response is not None


def test_tencent_manifest_preserves_market_prefix_and_symbol_matrix():
    manifest = source_manifest("tencent_kline", AssetClass.INDEX)

    assert manifest.canonical_ticker("000001.SH") == "sh000001"
    assert manifest.canonical_ticker("sh000688") == "sh000688"
    assert canonical_ticker_for_source("tencent_kline", AssetClass.INDEX, "000015.SH") == "sh000015"
    assert manifest.supports_timeframe("sh000001", Timeframe.DAY)
    assert manifest.supports_timeframe("sh000001", Timeframe.WEEK)
    assert not manifest.supports_timeframe("sh000001", Timeframe.HOUR_4)
    assert not manifest.supports_timeframe("000001", Timeframe.DAY)
