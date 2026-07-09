"""Tests for KlineStore — the core storage layer."""

from pathlib import Path

import pytest

from kline.models import AssetClass, Candle, Timeframe
from kline.store import KlineStore


@pytest.fixture
def store(tmp_path: Path) -> KlineStore:
    db_path = str(tmp_path / "test.db")
    return KlineStore(db_path)


@pytest.fixture
def sample_candles() -> list[Candle]:
    return [
        Candle(timestamp="2026-03-25", open=100.0, high=105.0, low=99.0, close=103.0, volume=1000),
        Candle(timestamp="2026-03-26", open=103.0, high=108.0, low=102.0, close=107.0, volume=1200),
        Candle(timestamp="2026-03-27", open=107.0, high=110.0, low=105.0, close=109.0, volume=1100),
    ]


class TestSaveAndQuery:
    def test_save_and_query_roundtrip(self, store: KlineStore, sample_candles: list[Candle]):
        count = store.save("AAPL", AssetClass.US_STOCK, Timeframe.DAY, sample_candles)
        assert count == 3

        result = store.query("AAPL", AssetClass.US_STOCK, Timeframe.DAY)
        assert len(result) == 3
        assert result[0].timestamp == "2026-03-25"
        assert result[0].open == 100.0
        assert result[-1].timestamp == "2026-03-27"

    def test_upsert_overwrites(self, store: KlineStore):
        candle_v1 = [Candle(timestamp="2026-03-25", open=100, high=105, low=99, close=103, volume=1000)]
        candle_v2 = [Candle(timestamp="2026-03-25", open=101, high=106, low=100, close=104, volume=1100)]

        store.save("AAPL", AssetClass.US_STOCK, Timeframe.DAY, candle_v1)
        store.save("AAPL", AssetClass.US_STOCK, Timeframe.DAY, candle_v2)

        result = store.query("AAPL", AssetClass.US_STOCK, Timeframe.DAY)
        assert len(result) == 1
        assert result[0].open == 101.0  # Updated
        assert store.count("AAPL", AssetClass.US_STOCK, Timeframe.DAY) == 1

    def test_empty_save_returns_zero(self, store: KlineStore):
        assert store.save("AAPL", AssetClass.US_STOCK, Timeframe.DAY, []) == 0

    def test_raw_upstream_response_is_saved_separately(self, store: KlineStore):
        assert store.count_raw_responses() == 0

        count = store.save_raw_response(
            provider="binance_usdm_futures",
            source_mode="binance_usdm_futures",
            ticker="XAUUSDT",
            asset_class=AssetClass.COMMODITY,
            timeframe=Timeframe.MIN_1,
            served_from="upstream",
            execution_venue=True,
            request_params={"symbol": "XAUUSDT", "interval": "1m"},
            response_body=[["raw"]],
            status_code=200,
        )

        assert count == 1
        assert store.count_raw_responses() == 1

    def test_query_with_date_range(self, store: KlineStore, sample_candles: list[Candle]):
        store.save("AAPL", AssetClass.US_STOCK, Timeframe.DAY, sample_candles)

        result = store.query(
            "AAPL", AssetClass.US_STOCK, Timeframe.DAY,
            start="2026-03-26", end="2026-03-27",
        )
        assert len(result) == 2
        assert result[0].timestamp == "2026-03-26"

    def test_query_empty_returns_empty(self, store: KlineStore):
        result = store.query("NONEXIST", AssetClass.US_STOCK, Timeframe.DAY)
        assert result == []


class TestListAndCount:
    def test_list_tickers(self, store: KlineStore, sample_candles: list[Candle]):
        store.save("AAPL", AssetClass.US_STOCK, Timeframe.DAY, sample_candles)
        store.save("MSFT", AssetClass.US_STOCK, Timeframe.DAY, sample_candles)

        tickers = store.list_tickers()
        assert set(tickers) == {"AAPL", "MSFT"}

    def test_list_tickers_filtered(self, store: KlineStore, sample_candles: list[Candle]):
        store.save("AAPL", AssetClass.US_STOCK, Timeframe.DAY, sample_candles)
        store.save("BTC", AssetClass.CRYPTO, Timeframe.DAY, sample_candles)

        assert store.list_tickers(AssetClass.US_STOCK) == ["AAPL"]
        assert store.list_tickers(AssetClass.CRYPTO) == ["BTC"]

    def test_count(self, store: KlineStore, sample_candles: list[Candle]):
        store.save("AAPL", AssetClass.US_STOCK, Timeframe.DAY, sample_candles)
        assert store.count("AAPL", AssetClass.US_STOCK, Timeframe.DAY) == 3


class TestAssetClassIsolation:
    def test_same_ticker_different_asset_class(self, store: KlineStore, sample_candles: list[Candle]):
        """Same ticker string in different asset classes should not collide."""
        store.save("000001", AssetClass.A_SHARE, Timeframe.DAY, sample_candles)
        store.save("000001", AssetClass.US_STOCK, Timeframe.DAY, sample_candles[:1])

        assert store.count("000001", AssetClass.A_SHARE, Timeframe.DAY) == 3
        assert store.count("000001", AssetClass.US_STOCK, Timeframe.DAY) == 1
