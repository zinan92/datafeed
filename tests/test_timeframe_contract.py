import asyncio
from datetime import date, datetime, timedelta, timezone
import time
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
import httpx

from kline.api import RequestPolicy, _build_response, _normalize_timeframe_transform
from kline.models import AssetClass, CachePolicy, Candle, FallbackPolicy, QualityPolicy, Timeframe
from kline.providers.base import ProviderError
from kline.providers.us import USStockProvider, _aggregate_completed_weeks
from kline.providers.crypto import CryptoProvider
from kline.provenance import provider_meta, source_manifest


def _daily_frame(rows: list[tuple[str, float, float, float, float]]) -> pd.DataFrame:
    index = pd.DatetimeIndex([item[0] for item in rows], tz="UTC")
    return pd.DataFrame(
        {
            "Open": [item[1] for item in rows],
            "High": [item[2] for item in rows],
            "Low": [item[3] for item in rows],
            "Close": [item[4] for item in rows],
            "Volume": [100.0 for _ in rows],
        },
        index=index,
    )


def _repaired_daily_frame() -> pd.DataFrame:
    index = pd.DatetimeIndex(["2026-08-27", "2026-08-28"], tz="UTC")
    return pd.DataFrame(
        {
            "Open": [100.0, 101.0],
            "High": [102.0, 103.0],
            "Low": [99.0, 100.0],
            "Close": [101.0, 102.0],
            "Volume": [100.0, 120.0],
            "Repaired?": [False, True],
        },
        index=index,
    )


def _broken_daily_frame() -> pd.DataFrame:
    frame = _repaired_daily_frame().copy()
    frame.loc[frame.index[-1], ["Open", "High", "Low", "Close"]] = float("nan")
    frame["Repaired?"] = [False, False]
    return frame


@pytest.mark.asyncio
async def test_yahoo_1h_is_aggregated_to_complete_4h_and_metadata_is_typed(monkeypatch):
    hourly = []
    for base in (100.0, 104.0):
        for offset in range(4):
            value = base + offset
            hourly.append(
                (
                    f"2026-08-18T{offset + (13 if base == 100 else 17):02d}:30:00",
                    value,
                    value + 2,
                    value - 1,
                    value + 1,
                )
            )

    class FakeTicker:
        def history(self, **kwargs):
            assert kwargs["interval"] == "1h"
            return _daily_frame(hourly)

    monkeypatch.setattr("kline.providers.us.yf.Ticker", lambda ticker: FakeTicker())
    provider = USStockProvider(four_hour_anchor=(13, 30))

    candles = await provider.fetch("DX-Y.NYB", Timeframe.HOUR_4, limit=2)

    assert len(candles) == 2
    assert candles[0].timestamp == "2026-08-18T13:30:00+00:00"
    assert candles[0].open == 100
    assert candles[0].high == 105
    assert candles[0].low == 99
    assert candles[0].close == 104
    assert provider.timeframe_transform is not None
    transform = provider.timeframe_transform
    assert transform.raw_timeframe == Timeframe.HOUR_1
    assert transform.timeframe_origin == "aggregated"
    assert transform.aggregation["rule"] == "fixed_4h"
    assert transform.aggregation["anchor_hour"] == 13
    assert transform.aggregation["anchor_minute"] == 30


@pytest.mark.asyncio
async def test_yahoo_sync_history_does_not_block_cell_timeout(monkeypatch):
    class HangingTicker:
        def history(self, **_kwargs):
            time.sleep(0.2)
            return _daily_frame([("2026-08-28", 100, 102, 99, 101)])

    monkeypatch.setattr("kline.providers.us.yf.Ticker", lambda _ticker: HangingTicker())
    provider = USStockProvider()
    started = time.perf_counter()
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(provider.fetch("AAPL", Timeframe.DAY), timeout=0.02)
    assert time.perf_counter() - started < 0.1


@pytest.mark.asyncio
async def test_yahoo_sync_repair_does_not_block_cell_timeout(monkeypatch):
    class RepairHangingTicker:
        def history(self, **kwargs):
            if kwargs.get("repair"):
                time.sleep(0.2)
                return _repaired_daily_frame()
            return _broken_daily_frame()

    monkeypatch.setattr("kline.providers.us.yf.Ticker", lambda _ticker: RepairHangingTicker())
    provider = USStockProvider()
    started = time.perf_counter()
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(provider.fetch("AAPL", Timeframe.DAY), timeout=0.02)
    assert time.perf_counter() - started < 0.1


@pytest.mark.asyncio
async def test_yahoo_weekly_excludes_current_partial_week(monkeypatch):
    rows = [
        ("2026-08-10", 100, 105, 99, 104),
        ("2026-08-14", 104, 110, 103, 109),
        ("2026-08-17", 109, 112, 108, 111),
        ("2026-08-20", 111, 115, 110, 114),
    ]

    class FakeTicker:
        def history(self, **kwargs):
            assert kwargs["interval"] == "1d"
            return _daily_frame(rows)

    monkeypatch.setattr("kline.providers.us.yf.Ticker", lambda ticker: FakeTicker())
    provider = USStockProvider()

    candles = await provider.fetch("^GSPC", Timeframe.WEEK, end="2026-08-21", limit=10)

    assert len(candles) == 1
    assert candles[0].timestamp == "2026-08-14"
    assert candles[0].open == 100
    assert candles[0].high == 110
    assert candles[0].low == 99
    assert candles[0].close == 109
    assert provider.timeframe_transform is not None
    assert provider.timeframe_transform.raw_timeframe == Timeframe.DAY
    assert provider.timeframe_transform.timeframe_origin == "aggregated"


@pytest.mark.asyncio
async def test_yahoo_daily_excludes_current_calendar_day(monkeypatch):
    today = datetime.now(ZoneInfo("America/New_York")).date()
    rows = [
        ((today - timedelta(days=1)).isoformat(), 100, 105, 99, 104),
        (today.isoformat(), 104, 110, 103, 109),
    ]

    class FakeTicker:
        def history(self, **kwargs):
            assert kwargs["interval"] == "1d"
            return _daily_frame(rows)

    monkeypatch.setattr("kline.providers.us.yf.Ticker", lambda ticker: FakeTicker())
    provider = USStockProvider()

    candles = await provider.fetch("^GSPC", Timeframe.DAY, limit=10)

    assert [item.timestamp for item in candles] == [(today - timedelta(days=1)).isoformat()]


@pytest.mark.asyncio
async def test_yahoo_default_daily_history_is_bounded_and_recorded(monkeypatch):
    yesterday = datetime.now(ZoneInfo("America/New_York")).date() - timedelta(days=1)
    calls: dict = {}

    class FakeTicker:
        def history(self, **kwargs):
            calls.update(kwargs)
            return _daily_frame(
                [(yesterday.isoformat(), 100, 105, 99, 104)],
            )

    monkeypatch.setattr("kline.providers.us.yf.Ticker", lambda ticker: FakeTicker())
    provider = USStockProvider()

    candles = await provider.fetch("SCHD", Timeframe.DAY, limit=10)

    assert len(candles) == 1
    assert calls["interval"] == "1d"
    assert calls["period"] == "5y"
    assert provider.last_raw_response is not None
    assert provider.last_raw_response["request_params"]["period"] == "5y"


@pytest.mark.asyncio
async def test_yahoo_enables_upstream_repair_and_records_repaired_rows(monkeypatch):
    calls: dict = {}

    class FakeTicker:
        def history(self, **kwargs):
            calls.update(kwargs)
            return _repaired_daily_frame() if kwargs["repair"] else _broken_daily_frame()

    monkeypatch.setattr("kline.providers.us.yf.Ticker", lambda ticker: FakeTicker())
    provider = USStockProvider()

    candles = await provider.fetch(
        "SPY",
        Timeframe.DAY,
        start="2026-08-27",
        end="2026-08-29",
        limit=10,
    )

    assert len(candles) == 2
    assert calls["repair"] is True
    assert provider.last_raw_response is not None
    assert (
        provider.last_raw_response["request_params"]["repair_policy"]
        == "yfinance_repair_on_invalid_ohlc"
    )
    assert provider.last_raw_response["request_params"]["repair_attempted"] is True
    assert provider.source_identity["repair_policy"] == "yfinance_repair_on_invalid_ohlc"
    assert provider.source_identity["repair_attempted"] is True
    assert provider.source_identity["repaired_row_count"] == 1
    assert provider.source_identity["repaired_timestamps"] == ["2026-08-28"]
    assert provider.last_raw_response["response_body"]["repaired_timestamps"] == ["2026-08-28"]


@pytest.mark.asyncio
async def test_yahoo_excludes_one_invalid_row_and_reports_the_quality_loss(monkeypatch):
    index = pd.DatetimeIndex(["2021-08-31", "2021-09-01", "2021-09-02"], tz="UTC")
    frame = pd.DataFrame(
        {
            "Open": [100.0, float("nan"), 102.0],
            "High": [102.0, float("nan"), 104.0],
            "Low": [99.0, float("nan"), 101.0],
            "Close": [101.0, float("nan"), 103.0],
            "Volume": [100.0, 120.0, 140.0],
        },
        index=index,
    )

    class FakeTicker:
        def history(self, **_kwargs):
            return frame.copy()

    monkeypatch.setattr("kline.providers.us.yf.Ticker", lambda _ticker: FakeTicker())
    provider = USStockProvider()

    candles = await provider.fetch("QCOM", Timeframe.DAY, limit=10)

    assert [candle.timestamp for candle in candles] == ["2021-08-31", "2021-09-02"]
    assert all(candle.open in {100.0, 102.0} for candle in candles)
    assert provider.source_identity["quality_flags"] == ["invalid_row_excluded"]
    assert provider.source_identity["excluded_row_count"] == 1
    assert provider.source_identity["excluded_rows"] == [
        {
            "timestamp": "2021-09-01",
            "reason": "non_finite_ohlcv",
        }
    ]


@pytest.mark.asyncio
async def test_yahoo_fails_loudly_when_every_row_is_excluded(monkeypatch):
    index = pd.DatetimeIndex(["2023-02-02"], tz="UTC")
    frame = pd.DataFrame(
        {
            "Open": [100.0],
            "High": [99.0],
            "Low": [98.0],
            "Close": [101.0],
            "Volume": [100.0],
        },
        index=index,
    )

    class FakeTicker:
        def history(self, **_kwargs):
            return frame.copy()

    monkeypatch.setattr("kline.providers.us.yf.Ticker", lambda _ticker: FakeTicker())
    provider = USStockProvider()

    with pytest.raises(ProviderError, match="all rows failed quality validation"):
        await provider.fetch("000660.KS", Timeframe.DAY, limit=10)

    assert provider.source_identity["quality_flags"] == ["invalid_row_excluded"]
    assert provider.source_identity["excluded_rows"] == [
        {
            "timestamp": "2023-02-02",
            "reason": "ohlc_invariant",
        }
    ]
    assert provider.last_raw_response is not None
    assert provider.last_raw_response["error"] == "all_rows_failed_quality_validation"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ticker", "bad_date"),
    [
        ("000660.KS", "2023-02-02"),
        ("DHR", "2026-09-01"),
    ],
)
async def test_yahoo_real_invariant_regressions_keep_good_rows_deterministically(
    monkeypatch, ticker, bad_date
):
    index = pd.DatetimeIndex(["2021-01-04", "2022-01-04", bad_date], tz="UTC")
    frame = pd.DataFrame(
        {
            "Open": [90.0, 110.0, 100.0],
            "High": [92.0, 112.0, 99.0],
            "Low": [89.0, 109.0, 98.0],
            "Close": [91.0, 111.0, 101.0],
            "Volume": [100.0, 120.0, 140.0],
        },
        index=index,
    )

    class FakeTicker:
        def history(self, **_kwargs):
            return frame.copy()

    monkeypatch.setattr("kline.providers.us.yf.Ticker", lambda _ticker: FakeTicker())
    provider = USStockProvider()

    first = await provider.fetch(
        ticker,
        Timeframe.DAY,
        start="2021-01-04",
        end="2026-09-03",
        limit=10,
    )
    first_exclusions = list(provider.source_identity["excluded_rows"])
    second = await provider.fetch(
        ticker,
        Timeframe.DAY,
        start="2021-01-04",
        end="2026-09-03",
        limit=10,
    )

    assert [candle.timestamp for candle in first] == ["2021-01-04", "2022-01-04"]
    assert [candle.model_dump() for candle in second] == [
        candle.model_dump() for candle in first
    ]
    assert provider.source_identity["excluded_rows"] == first_exclusions == [
        {"timestamp": bad_date, "reason": "ohlc_invariant"}
    ]


@pytest.mark.asyncio
async def test_yahoo_excludes_negative_volume_without_fabricating_a_replacement(monkeypatch):
    index = pd.DatetimeIndex(["2026-08-31", "2026-09-01"], tz="UTC")
    frame = pd.DataFrame(
        {
            "Open": [100.0, 101.0],
            "High": [102.0, 103.0],
            "Low": [99.0, 100.0],
            "Close": [101.0, 102.0],
            "Volume": [-1.0, 120.0],
        },
        index=index,
    )

    class FakeTicker:
        def history(self, **_kwargs):
            return frame.copy()

    monkeypatch.setattr("kline.providers.us.yf.Ticker", lambda _ticker: FakeTicker())
    provider = USStockProvider()

    candles = await provider.fetch("AAPL", Timeframe.DAY, limit=10)

    assert [candle.timestamp for candle in candles] == ["2026-09-01"]
    assert candles[0].volume == 120.0
    assert provider.source_identity["repair_attempted"] is False
    assert provider.source_identity["excluded_rows"] == [
        {"timestamp": "2026-08-31", "reason": "negative_volume"}
    ]


@pytest.mark.asyncio
async def test_yahoo_repair_context_is_clipped_to_requested_end(monkeypatch):
    calls: dict = {}
    index = pd.DatetimeIndex(["2026-08-28", "2026-08-31"], tz="UTC")
    frame = pd.DataFrame(
        {
            "Open": [100.0, 101.0],
            "High": [102.0, 103.0],
            "Low": [99.0, 100.0],
            "Close": [101.0, 102.0],
            "Volume": [100.0, 120.0],
            "Repaired?": [True, True],
        },
        index=index,
    )

    class FakeTicker:
        def history(self, **kwargs):
            calls.update(kwargs)
            return frame if kwargs["repair"] else frame.assign(Close=[101.0, float("nan")])

    monkeypatch.setattr("kline.providers.us.yf.Ticker", lambda ticker: FakeTicker())
    provider = USStockProvider()

    candles = await provider.fetch(
        "^KS11",
        Timeframe.DAY,
        start="2026-08-28",
        end="2026-08-29",
        limit=10,
    )

    assert calls["end"] == "2026-09-05"
    assert [candle.timestamp for candle in candles] == ["2026-08-28"]
    assert provider.source_identity["repaired_timestamps"] == ["2026-08-28"]


@pytest.mark.asyncio
async def test_yahoo_explicit_range_does_not_add_default_period(monkeypatch):
    calls: dict = {}

    class FakeTicker:
        def history(self, **kwargs):
            calls.update(kwargs)
            return _daily_frame(
                [("2026-08-18", 100, 105, 99, 104)],
            )

    monkeypatch.setattr("kline.providers.us.yf.Ticker", lambda ticker: FakeTicker())
    provider = USStockProvider()

    await provider.fetch(
        "CL=F",
        Timeframe.DAY,
        start="2026-08-18",
        end="2026-08-19",
        limit=10,
    )

    assert calls["interval"] == "1d"
    assert calls["start"] == "2026-08-18"
    assert calls["end"] == "2026-08-26"
    assert "period" not in calls
    assert provider.last_raw_response is not None
    assert "period" not in provider.last_raw_response["request_params"]
    assert provider.last_raw_response["request_params"]["end"] == "2026-08-19"
    assert provider.last_raw_response["request_params"]["repair_context_end"] == "2026-08-26"


@pytest.mark.asyncio
async def test_yahoo_intraday_accepts_timezone_aware_iso_window(monkeypatch):
    calls: dict = {}

    class FakeTicker:
        def history(self, **kwargs):
            calls.update(kwargs)
            return _daily_frame(
                [("2026-08-31T16:00:00", 100, 105, 99, 104)],
            )

    monkeypatch.setattr("kline.providers.us.yf.Ticker", lambda ticker: FakeTicker())
    provider = USStockProvider()

    candles = await provider.fetch(
        "AAPL",
        Timeframe.HOUR_1,
        start="2026-08-31T16:00:00+00:00",
        end="2026-09-01T00:00:00+00:00",
        limit=10,
    )

    assert len(candles) == 1
    assert calls["start"] == datetime.fromisoformat("2026-08-31T16:00:00+00:00")
    assert calls["start"].tzinfo is not None
    assert calls["end"] == "2026-09-08"
    assert provider.last_raw_response is not None
    assert provider.last_raw_response["request_params"]["start"] == "2026-08-31T16:00:00+00:00"


def test_yahoo_symbol_timeframe_allowlist_is_explicit():
    index = source_manifest("yahoo_finance_index", AssetClass.INDEX)
    etf = source_manifest("yahoo_finance_etf", AssetClass.ETF)
    futures = source_manifest("yahoo_finance_futures", AssetClass.COMMODITY)
    us_stock = source_manifest("yahoo_finance", AssetClass.US_STOCK)
    crypto = source_manifest("binance_spot_public", AssetClass.CRYPTO)
    hyperliquid = source_manifest("hyperliquid_perpetual_public", AssetClass.CRYPTO)
    tushare = source_manifest("tushare_pro", AssetClass.A_SHARE)

    assert index.meta.supported_symbols == ("DX-Y.NYB", "^GSPC", "^IXIC", "^VIX", "^N225", "^KS11")
    assert index.supports_timeframe("DX-Y.NYB", Timeframe.HOUR_4)
    assert not index.supports_timeframe("^GSPC", Timeframe.HOUR_4)
    assert etf.meta.supported_symbols == ("SPY", "QQQ", "SCHD", "UUP")
    assert not etf.supports_timeframe("SCHD", Timeframe.HOUR_4)
    assert etf.supports_timeframe("UUP", Timeframe.HOUR_4)
    assert etf.supports_timeframe("SPY", Timeframe.DAY)
    assert etf.supports_timeframe("QQQ", Timeframe.WEEK)
    assert futures.supports_timeframe("GC=F", Timeframe.HOUR_4)
    assert not us_stock.supports_timeframe("AAPL", Timeframe.HOUR_4)
    assert crypto.supports_timeframe("BTC", Timeframe.HOUR_4)
    assert not crypto.supports_timeframe("ETH", Timeframe.HOUR_4)
    assert hyperliquid.supports_timeframe("BTC", Timeframe.MIN_30)
    assert hyperliquid.supports_timeframe("ETH", Timeframe.MIN_30)
    assert hyperliquid.supports_timeframe("HYPE", Timeframe.MIN_30)
    assert hyperliquid.supports_timeframe("BTC", Timeframe.MIN_15)
    assert hyperliquid.supports_timeframe("ETH", Timeframe.MIN_15)
    assert hyperliquid.supports_timeframe("HYPE", Timeframe.MIN_15)
    assert tushare.supports_timeframe("300308.SZ", Timeframe.MIN_15)
    assert tushare.supports_timeframe("300308.SZ", Timeframe.HOUR_4)
    assert tushare.supports_timeframe("300308.SZ", Timeframe.DAY)


def test_health_exposes_effective_symbol_matrix(tmp_path):
    from kline.config import Settings
    from kline.registry import init, provider_status

    init(Settings(db_path=str(tmp_path / "health.db")))
    sources = provider_status()["sources"]

    assert sources["yahoo_finance_index"]["supported_timeframes"] == ["1d", "1w"]
    assert "4h" in sources["yahoo_finance_index"]["supported_timeframes_by_symbol"]["DX-Y.NYB"]
    assert "4h" not in sources["yahoo_finance_index"]["supported_timeframes_by_symbol"]["^GSPC"]


def test_weekly_does_not_promote_monday_to_thursday_as_a_closed_friday_week():
    next_monday = date.today() + timedelta(days=7 - date.today().weekday())
    candles = [
        Candle(
            timestamp=(next_monday + timedelta(days=offset)).isoformat(),
            open=100 + offset,
            high=101 + offset,
            low=99 + offset,
            close=100 + offset,
            volume=10,
        )
        for offset in range(4)
    ]

    assert _aggregate_completed_weeks(candles, cutoff=next_monday + timedelta(days=4)) == []


def test_weekly_accepts_a_completed_holiday_week_using_last_trading_session():
    previous_monday = date.today() - timedelta(days=date.today().weekday() + 7)
    candles = [
        Candle(
            timestamp=(previous_monday + timedelta(days=offset)).isoformat(),
            open=100 + offset,
            high=101 + offset,
            low=99 + offset,
            close=100 + offset,
            volume=10,
        )
        for offset in range(4)
    ]

    output = _aggregate_completed_weeks(candles, cutoff=previous_monday + timedelta(days=4))

    assert len(output) == 1
    assert output[0].timestamp == (previous_monday + timedelta(days=3)).isoformat()


@pytest.mark.asyncio
async def test_binance_native_4h_requests_native_interval_and_keeps_receipt():
    open_time = int(datetime(2026, 8, 18, tzinfo=timezone.utc).timestamp() * 1000)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["symbol"] == "BTCUSDT"
        assert request.url.params["interval"] == "4h"
        return httpx.Response(
            200,
            json=[
                [open_time, "100", "105", "99", "104", "10", 0, "1040"],
            ],
            request=request,
        )

    provider = CryptoProvider(transport=httpx.MockTransport(handler))
    candles = await provider.fetch("BTC", Timeframe.HOUR_4, limit=1)

    assert len(candles) == 1
    assert candles[0].timestamp == "2026-08-18T00:00:00+00:00"
    assert provider.timeframe_transform is not None
    assert provider.timeframe_transform.raw_timeframe == Timeframe.HOUR_4
    assert provider.timeframe_transform.timeframe_origin == "native"
    assert provider.last_raw_response["request_params"]["interval"] == "4h"
    assert provider.source_identity["provider_symbol"] == "BTCUSDT"


@pytest.mark.asyncio
async def test_binance_daily_excludes_current_active_bar_and_validates_ohlc():
    today = datetime.combine(date.today(), datetime.min.time(), tzinfo=timezone.utc)
    yesterday = today - timedelta(days=1)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                [int(yesterday.timestamp() * 1000), "100", "105", "99", "104", "10", 0, "1040"],
                [int(today.timestamp() * 1000), "104", "110", "103", "109", "10", 0, "1090"],
            ],
            request=request,
        )

    provider = CryptoProvider(transport=httpx.MockTransport(handler))
    candles = await provider.fetch("BTC", Timeframe.DAY, limit=5)

    assert [item.timestamp for item in candles] == [yesterday.strftime("%Y-%m-%d")]


def test_native_transform_is_not_relabelled_as_aggregated():
    transform = _normalize_timeframe_transform(
        Timeframe.HOUR_4,
        {
            "raw_timeframe": "4h",
            "timeframe_origin": "native",
            "aggregation": {"kind": "none", "rule": "native_passthrough"},
        },
    )
    assert transform.raw_timeframe == Timeframe.HOUR_4
    assert transform.timeframe_origin == "native"
    assert transform.aggregation["rule"] == "native_passthrough"


def test_response_preserves_timeframe_transform_and_source_identity():
    policy = RequestPolicy(
        requested_source="yahoo_finance_index",
        source="yahoo_finance_index",
        cache_policy=CachePolicy.BYPASS,
        quality_policy=QualityPolicy.STRICT,
        fallback_policy=FallbackPolicy.NONE,
        fallback_sources=(),
        require_execution_venue=False,
    )
    response = _build_response(
        "^GSPC",
        AssetClass.INDEX,
        Timeframe.HOUR_4,
        [
            Candle(
                timestamp="2026-08-18T00:00:00Z",
                open=100,
                high=105,
                low=99,
                close=104,
                volume=10,
            )
        ],
        provider_meta(AssetClass.INDEX),
        served_from="upstream",
        policy=policy,
        selected_source="yahoo_finance_index",
        attempted_sources=["yahoo_finance_index"],
        timeframe_transform={
            "raw_timeframe": "1h",
            "timeframe_origin": "aggregated",
            "aggregation": {"rule": "fixed_4h"},
        },
        source_identity={"provider_symbol": "^GSPC"},
    )

    assert response.raw_timeframe == Timeframe.HOUR_1
    assert response.timeframe_origin == "aggregated"
    assert response.aggregation["rule"] == "fixed_4h"
    assert response.source_identity["provider_symbol"] == "^GSPC"
