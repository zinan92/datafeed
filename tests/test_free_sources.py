from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from kline.free_source_profile import apply_free_source_profile
from kline.models import AssetClass, Candle, Timeframe
from kline.mvp_manifest import load_manifest, manifest_digest
from kline.ports import ProviderBackedMarketDataAdapter
from kline.provenance import source_asset_class, source_manifest
from kline.providers.base import ProviderError
from kline.providers.free_ashare import (
    AShareFreeProvider,
    parse_10jqka_rows,
    parse_tencent_rows,
)
from kline.providers.free_common import requested_cutoff
from kline.providers.free_us import USFreeProvider, _yahoo_ticker
from kline.providers.us import USStockProvider


MANIFEST_PATH = Path(__file__).parents[1] / "configs" / "mvp_manifest.json"


def test_derived_bar_cutoff_uses_request_end() -> None:
    assert requested_cutoff("2026-09-01T08:00:00Z").isoformat() == "2026-09-01T08:00:00+00:00"


def test_yahoo_ticker_alias_preserves_dotted_canonical_symbol() -> None:
    assert _yahoo_ticker("BRK.B") == "BRK-B"
    assert _yahoo_ticker("AAPL") == "AAPL"


def test_free_profile_routes_a_share_and_us_stock_without_changing_identity() -> None:
    manifest = apply_free_source_profile(load_manifest(MANIFEST_PATH))
    by_symbol = {item.display_symbol: item for item in manifest.instruments}
    a_share = by_symbol["600519"]
    us_stock = by_symbol["AAPL"]
    assert a_share.instrument_id == "CN.A.600519"
    assert a_share.source_id == "tencent_stock_free"
    assert a_share.source_status == "configured"
    assert a_share.adjustment_basis == "qfq"
    assert a_share.required_timeframes == ("15m", "1h", "4h", "1d", "1w")
    assert us_stock.instrument_id == "US.EQ.AAPL"
    assert us_stock.source_id == "yahoo_finance_free"
    assert us_stock.source_status == "configured"
    assert us_stock.required_timeframes == ("15m", "1h", "4h", "1d", "1w")
    assert len(manifest_digest(manifest)) == 64


def test_tencent_and_tonghuashun_parsers_normalize_ohlcv() -> None:
    rows = [["202609011500", "1301.23", "1299.56", "1304.51", "1299.01", "6674.00", {}, "5.34"]]
    candles = parse_tencent_rows(rows, timezone_name="Asia/Shanghai")
    assert candles[0].timestamp == "2026-09-01T07:00:00+00:00"
    assert candles[0].open == 1301.23
    assert candles[0].volume == 6674.0
    assert candles[0].amount is None

    tenjqka = parse_10jqka_rows(
        "202609011500,1301.23,1304.51,1299.01,1299.56,6674,8670000.00,0.1,,,0"
    )
    assert tenjqka[0].timestamp == "2026-09-01T07:00:00+00:00"
    assert tenjqka[0].volume == 6674.0
    assert tenjqka[0].amount == 8670000.0


@pytest.mark.asyncio
async def test_free_a_share_provider_falls_back_to_tonghuashun() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "ifzq.gtimg.cn" in str(request.url):
            return httpx.Response(503, request=request)
        return httpx.Response(
            200,
            request=request,
            text='quot({"data":"202609011500,1301.23,1304.51,1299.01,1299.56,6674,8670000.00,0.1,,,0"})',
        )

    provider = AShareFreeProvider(transport=httpx.MockTransport(handler))
    candles = await provider.fetch("600519", Timeframe.HOUR_1, limit=10)
    assert len(candles) == 1
    assert provider.source_identity["selected_source"] == "tonghuashun"
    assert provider.source_identity["fallback_from"] == "tencent"
    assert provider.source_identity["adjustment_basis"] == "unverified"
    assert provider.source_identity["adjustment_basis_evidence"] == "tonghuashun_unverified"
    assert [item["source"] for item in provider.last_attempts] == ["tencent", "tonghuashun"]


@pytest.mark.asyncio
async def test_free_a_share_provider_reports_both_sources_failed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, request=request)

    provider = AShareFreeProvider(transport=httpx.MockTransport(handler))
    with pytest.raises(ProviderError) as error:
        await provider.fetch("600519", Timeframe.HOUR_1, limit=10)
    assert "Tencent=" in str(error.value)
    assert "Tonghuashun=" in str(error.value)
    assert [item["http_status"] for item in provider.last_attempts] == [503, 503]


@pytest.mark.asyncio
async def test_free_a_share_empty_response_is_terminal_and_resets_identity() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        code = "sh600519" if "sh600519" in str(request.url) else "sh601989"
        rows = {
            "sh600519": [["202609011500", "100", "101", "102", "99", "10"]],
            "sh601989": [],
        }[code]
        return httpx.Response(
            200,
            request=request,
            json={"data": {code: {"m15": rows}}},
        )

    provider = AShareFreeProvider(transport=httpx.MockTransport(handler))
    assert await provider.fetch("600519", Timeframe.MIN_15, limit=10)
    assert provider.source_identity["provider_symbol"] == "sh600519"
    with pytest.raises(ProviderError) as error:
        await provider.fetch("601989", Timeframe.MIN_15, limit=10)
    assert error.value.code == "empty_response"
    assert len(provider.last_attempts) == 1
    assert provider.source_identity == {}


@pytest.mark.asyncio
async def test_free_a_share_empty_plus_fallback_404_is_terminal() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "ifzq.gtimg.cn" in str(request.url):
            return httpx.Response(200, request=request, json={"data": {"sh601989": {"m60": []}}})
        return httpx.Response(404, request=request)

    provider = AShareFreeProvider(transport=httpx.MockTransport(handler))
    with pytest.raises(ProviderError) as error:
        await provider.fetch("601989", Timeframe.HOUR_1, limit=10)
    assert error.value.code == "empty_response"
    assert len(provider.last_attempts) == 2


@pytest.mark.asyncio
async def test_adapter_exposes_failed_provider_attempts() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, request=request)

    provider = AShareFreeProvider(transport=httpx.MockTransport(handler))
    adapter = ProviderBackedMarketDataAdapter(
        source_manifest("tencent_stock_free", AssetClass.A_SHARE), provider
    )
    with pytest.raises(ProviderError):
        await adapter.fetch_candles_with_receipt("600519", Timeframe.HOUR_1, limit=10)
    assert [item["source"] for item in adapter.last_attempts] == ["tencent", "tonghuashun"]
    assert [item["http_status"] for item in adapter.last_attempts] == [503, 503]


@pytest.mark.asyncio
async def test_free_us_provider_uses_yahoo_for_intraday_and_declares_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_tickers: list[str] = []

    async def yahoo_success(
        _self,
        _ticker: str,
        _timeframe: Timeframe,
        *,
        start: str | None,
        end: str | None,
        limit: int,
    ) -> list[Candle]:
        seen_tickers.append(_ticker)
        del start, end, limit
        return [Candle(timestamp="2026-09-01", open=100, high=102, low=99, close=101, volume=10)]

    monkeypatch.setattr(USStockProvider, "fetch", yahoo_success)
    provider = USFreeProvider()
    candles = await provider.fetch("BRK.B", Timeframe.HOUR_1, limit=10)
    assert len(candles) == 1
    assert provider.source_identity["source_id"] == "yahoo_finance_free"
    assert provider.source_identity["selected_source"] == "yahoo"
    assert provider.source_identity["requested_symbol"] == "BRK.B"
    assert provider.source_identity["provider_symbol"] == "BRK-B"
    assert seen_tickers == ["BRK-B"]
    assert provider.last_attempts[0]["source"] == "yahoo"
    assert provider.last_attempts[0]["status"] == "success"
    assert provider.supported_timeframes() == [
        Timeframe.MIN_15,
        Timeframe.HOUR_1,
        Timeframe.HOUR_4,
        Timeframe.DAY,
        Timeframe.WEEK,
    ]


@pytest.mark.asyncio
async def test_free_us_provider_prefers_sina_for_daily() -> None:
    payload = '[{"d":"2026-09-01","o":"100","h":"102","l":"99","c":"101","v":"10","a":"1000"}]'
    provider = USFreeProvider(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, request=request, text=f"cb({payload})")
        )
    )
    candles = await provider.fetch("AAPL", Timeframe.DAY, limit=10)
    assert len(candles) == 1
    assert candles[0].close == 101
    assert provider.source_identity["selected_source"] == "sina"
    assert provider.source_identity["fallback_from"] is None
    assert [item["source"] for item in provider.last_attempts] == ["sina"]


@pytest.mark.asyncio
async def test_free_us_provider_falls_back_to_yahoo_when_sina_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def sina_unavailable(*_args, **_kwargs) -> list[Candle]:
        raise ProviderError("Sina unavailable")

    async def yahoo_success(
        _self,
        _ticker: str,
        _timeframe: Timeframe,
        *,
        start: str | None,
        end: str | None,
        limit: int,
    ) -> list[Candle]:
        del start, end, limit
        return [Candle(timestamp="2026-09-01", open=100, high=102, low=99, close=101, volume=10)]

    monkeypatch.setattr(USFreeProvider, "_fetch_sina_daily", sina_unavailable)
    monkeypatch.setattr(USStockProvider, "fetch", yahoo_success)
    provider = USFreeProvider()
    candles = await provider.fetch("AAPL", Timeframe.DAY, limit=10)
    assert len(candles) == 1
    assert provider.source_identity["selected_source"] == "yahoo"
    assert provider.source_identity["fallback_from"] == "sina"
    assert [item["source"] for item in provider.last_attempts] == ["sina", "yahoo"]


@pytest.mark.asyncio
async def test_free_us_provider_weekly_provenance_uses_sina_daily(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def sina_daily(*_args, **_kwargs) -> list[Candle]:
        return [Candle(timestamp="2026-08-28", open=100, high=102, low=99, close=101, volume=10)]

    monkeypatch.setattr(USFreeProvider, "_fetch_sina_daily", sina_daily)
    provider = USFreeProvider()
    candles = await provider.fetch("AAPL", Timeframe.WEEK, limit=10)
    assert candles
    assert provider.source_identity["selected_source"] == "sina"
    assert provider.timeframe_transform is not None
    assert provider.timeframe_transform.timeframe_origin == "aggregated"


def test_free_source_manifests_are_registered_for_the_right_asset_classes() -> None:
    assert source_asset_class("tencent_stock_free") == AssetClass.A_SHARE
    assert source_asset_class("yahoo_finance_free") == AssetClass.US_STOCK
    assert (
        source_manifest("tencent_stock_free", AssetClass.A_SHARE).asset_class == AssetClass.A_SHARE
    )
    assert (
        source_manifest("yahoo_finance_free", AssetClass.US_STOCK).asset_class
        == AssetClass.US_STOCK
    )


def test_registry_exposes_free_adapters_without_tokens(tmp_path: Path) -> None:
    from kline.config import Settings
    from kline.registry import get_adapter_for_source, init

    init(
        Settings(
            db_path=str(tmp_path / "free-adapters.db"),
            load_entrypoint_adapters=False,
            request_timeout=60,
        )
    )
    ashare_adapter = get_adapter_for_source("tencent_stock_free", AssetClass.A_SHARE)
    us_adapter = get_adapter_for_source("yahoo_finance_free", AssetClass.US_STOCK)
    assert ashare_adapter.supported_timeframes()
    assert us_adapter.supported_timeframes() == [
        Timeframe.MIN_15,
        Timeframe.HOUR_1,
        Timeframe.HOUR_4,
        Timeframe.DAY,
        Timeframe.WEEK,
    ]
    assert ashare_adapter._provider._timeout == us_adapter._provider._timeout == 15.0
