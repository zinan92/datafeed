
from __future__ import annotations

import httpx
import pytest

from kline.models import AssetClass, Timeframe
from kline.provenance import normalize_source, source_manifest
from kline.providers.ashare import _to_tushare_code
from kline.providers.fred import FredCsvProvider
from kline.ports import ProviderMeta
from kline.quality import analyze_candles
from kline.models import Candle


def test_yahoo_index_and_etf_manifests_are_explicit():
    assert normalize_source("yahoo_finance", AssetClass.INDEX) == "yahoo_finance_index"
    assert normalize_source("yahoo_finance", AssetClass.ETF) == "yahoo_finance_etf"
    assert source_manifest("yahoo_finance_index", AssetClass.INDEX).canonical_ticker("^GSPC") == "^GSPC"
    assert source_manifest("yahoo_finance_etf", AssetClass.ETF).canonical_ticker("SCHD") == "SCHD"


def test_tushare_index_symbols_preserve_exchange_suffix():
    assert _to_tushare_code("000001.SH") == "000001.SH"
    assert _to_tushare_code("000688.SH") == "000688.SH"
    assert _to_tushare_code("000001") == "000001.SZ"


@pytest.mark.asyncio
async def test_fred_treasury_curve_aliases_and_basis_point_scale():
    def handler(request: httpx.Request) -> httpx.Response:
        series_id = request.url.params["id"]
        assert series_id == "T10Y2Y"
        return httpx.Response(200, text="observation_date,T10Y2Y\n2026-08-14,0.35\n", request=request)

    provider = FredCsvProvider(transport=httpx.MockTransport(handler))
    bars = await provider.fetch("T10Y2Y", Timeframe.DAY, limit=10)
    assert bars[0].close == 35.0
    assert bars[0].open == bars[0].high == bars[0].low == bars[0].close


def test_market_hours_weekend_gap_is_not_blocked_as_intraday_gap():
    meta = ProviderMeta(
        name="test", source_mode="market_hours", quality_flags=(), continuous=False,
        market_type="index",
    )
    candles = [
        Candle(timestamp="2026-08-13", open=1, high=2, low=1, close=2, volume=0),
        Candle(timestamp="2026-08-17", open=2, high=3, low=2, close=3, volume=0),
    ]
    report = analyze_candles(candles, Timeframe.DAY, meta, strict=True)
    assert "gap" not in report.quality_flags
    assert report.reject_reason is None
