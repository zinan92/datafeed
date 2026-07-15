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

    def test_limit_returns_latest_rows_in_chronological_order(
        self, store: KlineStore, sample_candles: list[Candle]
    ):
        store.save("AAPL", AssetClass.US_STOCK, Timeframe.DAY, sample_candles)
        result = store.query("AAPL", AssetClass.US_STOCK, Timeframe.DAY, limit=2)
        assert [item.timestamp for item in result] == ["2026-03-26", "2026-03-27"]

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


class TestSourceIsolation:
    def test_same_candle_from_two_sources_does_not_collide(self, store: KlineStore):
        source_a = [
            Candle(timestamp="2026-03-25", open=100, high=105, low=99, close=103, volume=10)
        ]
        source_b = [
            Candle(timestamp="2026-03-25", open=200, high=205, low=199, close=203, volume=20)
        ]

        store.save(
            "GOLD",
            AssetClass.COMMODITY,
            Timeframe.DAY,
            source_a,
            source_id="source_a",
        )
        store.save(
            "GOLD",
            AssetClass.COMMODITY,
            Timeframe.DAY,
            source_b,
            source_id="source_b",
        )

        result_a = store.query(
            "GOLD", AssetClass.COMMODITY, Timeframe.DAY, source_id="source_a"
        )
        result_b = store.query(
            "GOLD", AssetClass.COMMODITY, Timeframe.DAY, source_id="source_b"
        )
        assert result_a[0].close == 103
        assert result_b[0].close == 203
        assert store.count(
            "GOLD", AssetClass.COMMODITY, Timeframe.DAY, source_id="source_a"
        ) == 1
        assert store.count(
            "GOLD", AssetClass.COMMODITY, Timeframe.DAY, source_id="source_b"
        ) == 1

    def test_old_schema_is_migrated_without_source_guessing(self, tmp_path: Path):
        import sqlite3

        db_path = tmp_path / "legacy.db"
        connection = sqlite3.connect(db_path)
        connection.execute(
            "CREATE TABLE klines ("
            "id INTEGER PRIMARY KEY, ticker VARCHAR NOT NULL, asset_class VARCHAR NOT NULL, "
            "timeframe VARCHAR NOT NULL, timestamp VARCHAR NOT NULL, open FLOAT NOT NULL, "
            "high FLOAT NOT NULL, low FLOAT NOT NULL, close FLOAT NOT NULL, volume FLOAT NOT NULL, "
            "amount FLOAT, created_at DATETIME, updated_at DATETIME)"
        )
        connection.execute(
            "CREATE UNIQUE INDEX ix_kline_lookup "
            "ON klines (ticker, asset_class, timeframe, timestamp)"
        )
        connection.execute(
            "INSERT INTO klines "
            "(ticker, asset_class, timeframe, timestamp, open, high, low, close, volume) "
            "VALUES ('GOLD', 'commodity', '1d', '2026-03-25', 1, 2, 0, 1, 10)"
        )
        connection.commit()
        connection.close()

        migrated = KlineStore(str(db_path))
        assert migrated.count("GOLD", AssetClass.COMMODITY, Timeframe.DAY) == 1
        migrated.save(
            "GOLD",
            AssetClass.COMMODITY,
            Timeframe.DAY,
            [Candle(timestamp="2026-03-25", open=3, high=4, low=2, close=3, volume=20)],
            source_id="new_source",
        )
        assert migrated.count(
            "GOLD", AssetClass.COMMODITY, Timeframe.DAY, source_id="new_source"
        ) == 1


class TestSourceObservations:
    def test_latest_observation_is_kept_per_source_instrument(self, store: KlineStore):
        common = {
            "provider": "broker",
            "ticker": "GOLD",
            "asset_class": AssetClass.COMMODITY,
            "timeframe": Timeframe.MIN_1,
            "candle_count": 1,
            "latest_timestamp": "2026-07-15T10:00:00",
            "latency_ms": 12.5,
            "quality_flags": [],
        }
        store.save_source_observation(source_id="source_a", success=True, **common)
        store.save_source_observation(
            source_id="source_a", success=False, error="timeout", **common
        )
        store.save_source_observation(source_id="source_b", success=True, **common)

        observations = store.latest_source_observations()
        assert len(observations) == 2
        source_a = next(item for item in observations if item["source_id"] == "source_a")
        assert source_a["success"] is False
        assert source_a["error"] == "timeout"
