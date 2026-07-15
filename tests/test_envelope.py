"""Tests for the response envelope assembly (_build_response)."""

from kline.api import _build_response
from kline.models import AssetClass, Candle, Timeframe
from kline.provenance import provider_meta


def _candle(ts: str, close: float = 100.0) -> Candle:
    return Candle(timestamp=ts, open=close, high=close + 1, low=close - 1, close=close, volume=10)


class TestBuildResponse:
    def test_stamps_provider_and_flags_on_every_candle(self):
        meta = provider_meta(AssetClass.CRYPTO)
        resp = _build_response(
            "BTC", AssetClass.CRYPTO, Timeframe.MIN_1,
            [_candle("2026-03-28T11:58:00"), _candle("2026-03-28T11:59:00")],
            meta, served_from="upstream",
        )
        assert resp.count == 2
        for candle in resp.candles:
            assert candle.provider == "binance_spot"
            assert "research_only" in candle.quality_flags

    def test_envelope_carries_full_provenance_header(self):
        meta = provider_meta(AssetClass.CRYPTO)
        resp = _build_response(
            "BTC", AssetClass.CRYPTO, Timeframe.MIN_1,
            [_candle("2026-03-28T11:59:00")], meta, served_from="cache",
        )
        assert resp.schema_version == "kline-candles-v1"
        assert resp.provider == "binance_spot"
        assert resp.source_mode == "binance_spot_public"
        assert resp.served_from == "cache"
        assert resp.latest_timestamp == "2026-03-28T11:59:00+00:00"
        assert resp.age_seconds is not None  # continuous source → age computed

    def test_is_synthetic_is_always_false(self):
        # kline has no synthetic path; downstream can trust real prices.
        meta = provider_meta(AssetClass.US_STOCK)
        resp = _build_response(
            "AAPL", AssetClass.US_STOCK, Timeframe.DAY,
            [_candle("2026-03-28")], meta, served_from="upstream",
        )
        assert resp.is_synthetic is False

    def test_market_hours_source_leaves_fresh_unknown(self):
        meta = provider_meta(AssetClass.US_STOCK)
        resp = _build_response(
            "AAPL", AssetClass.US_STOCK, Timeframe.DAY,
            [_candle("2026-03-28")], meta, served_from="upstream",
        )
        assert resp.fresh is None
        assert resp.max_age_seconds is None

    def test_empty_candles_yields_empty_but_valid_envelope(self):
        meta = provider_meta(AssetClass.CRYPTO)
        resp = _build_response("BTC", AssetClass.CRYPTO, Timeframe.MIN_1, [], meta, served_from="upstream")
        assert resp.count == 0
        assert resp.candles == []
        assert resp.latest_timestamp is None
        assert resp.fresh is None
        assert resp.provider == "binance_spot"  # header still describes the source
