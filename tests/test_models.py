"""Tests for data models."""

from kline.models import AssetClass, Candle, CandleResponse, Timeframe


class TestCandle:
    def test_create_candle(self):
        c = Candle(timestamp="2026-03-28", open=100, high=105, low=99, close=103, volume=1000)
        assert c.timestamp == "2026-03-28"
        assert c.amount is None

    def test_candle_with_amount(self):
        c = Candle(timestamp="2026-03-28", open=100, high=105, low=99, close=103, volume=1000, amount=50000)
        assert c.amount == 50000


class TestCandleResponse:
    def test_response_envelope(self):
        resp = CandleResponse(
            ticker="AAPL",
            asset_class=AssetClass.US_STOCK,
            timeframe=Timeframe.DAY,
            count=1,
            candles=[
                Candle(timestamp="2026-03-28", open=100, high=105, low=99, close=103, volume=1000)
            ],
        )
        assert resp.count == 1
        assert resp.ticker == "AAPL"


class TestEnums:
    def test_asset_class_values(self):
        assert AssetClass.A_SHARE.value == "a_share"
        assert AssetClass.US_STOCK.value == "us_stock"
        assert AssetClass.CRYPTO.value == "crypto"
        assert AssetClass.COMMODITY.value == "commodity"

    def test_timeframe_values(self):
        assert Timeframe.DAY.value == "1d"
        assert Timeframe.MIN_5.value == "5m"
        assert Timeframe.MIN_15.value == "15m"
        assert Timeframe.HOUR_4.value == "4h"
