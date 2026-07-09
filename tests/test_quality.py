"""Tests for candle quality checks."""

from __future__ import annotations

from datetime import datetime, timezone

from kline.models import Candle, Timeframe
from kline.provenance import live_provider_meta
from kline.quality import analyze_candles


def _candle(ts: str, close: float = 3300.0) -> Candle:
    return Candle(timestamp=ts, open=close, high=close + 1, low=close - 1, close=close, volume=10)


def test_gap_is_visible_and_blocks_strict_live():
    meta = live_provider_meta()
    now = datetime(2026, 7, 9, 10, 3, tzinfo=timezone.utc)
    report = analyze_candles(
        [_candle("2026-07-09T10:00:00"), _candle("2026-07-09T10:02:00")],
        Timeframe.MIN_1,
        meta,
        strict=True,
        now=now,
    )

    assert "gap" in report.quality_flags
    assert report.reject_reason == "gap"


def test_stale_is_visible_and_blocks_strict_live():
    meta = live_provider_meta()
    now = datetime(2026, 7, 9, 10, 10, tzinfo=timezone.utc)
    report = analyze_candles(
        [_candle("2026-07-09T10:00:00")],
        Timeframe.MIN_1,
        meta,
        strict=True,
        now=now,
    )

    assert "stale" in report.quality_flags
    assert report.fresh is False
    assert report.reject_reason == "stale"


def test_out_of_order_is_visible_and_blocks_strict_live():
    meta = live_provider_meta()
    now = datetime(2026, 7, 9, 10, 3, tzinfo=timezone.utc)
    report = analyze_candles(
        [_candle("2026-07-09T10:02:00"), _candle("2026-07-09T10:01:00")],
        Timeframe.MIN_1,
        meta,
        strict=True,
        now=now,
    )

    assert "out_of_order" in report.quality_flags
    assert report.reject_reason == "out_of_order"
