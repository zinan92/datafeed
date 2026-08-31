from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from kline.market_calendar import (
    CalendarError,
    aggregate_15m_to_4h,
    aggregate_daily_to_weekly,
    assess_quality,
    calendar_spec,
    resolve_calendar,
)
from kline.mvp_manifest import load_manifest
from kline.storage import CandleSeriesKey, MvpCandle


MANIFEST_PATH = Path(__file__).parents[1] / "configs" / "mvp_manifest.json"


def _key(
    *,
    source_id: str = "source-a",
    instrument_id: str = "US.AAPL",
    display_symbol: str = "AAPL",
    timeframe: str = "15m",
) -> CandleSeriesKey:
    return CandleSeriesKey(
        instrument_id=instrument_id,
        display_symbol=display_symbol,
        provider_symbol=display_symbol,
        source_id=source_id,
        asset_class="us_stock",
        timeframe=timeframe,
        adjustment_basis="raw_unadjusted",
        manifest_version="mvp_universe_v1",
    )


def _bar(key: CandleSeriesKey, local_stamp: datetime, *, close: float = 101.0) -> MvpCandle:
    return MvpCandle(
        key=key,
        timestamp=local_stamp.isoformat(),
        open=100.0,
        high=max(102.0, close),
        low=99.0,
        close=close,
        volume=10.0,
    )


def test_manifest_cells_resolve_to_matching_calendar_specs() -> None:
    manifest = load_manifest(MANIFEST_PATH)
    for instrument in manifest.instruments:
        spec = resolve_calendar(instrument)
        assert spec.calendar_id == instrument.calendar_id
        assert spec.timezone == instrument.timezone


def test_cn_a_4h_joins_morning_and_afternoon_without_lunch_bars() -> None:
    key = _key(instrument_id="CN.A.300308", display_symbol="300308")
    session_date = date(2026, 8, 31)
    stamps = [
        datetime.combine(session_date, datetime.min.time(), tzinfo=timezone.utc).replace(
            hour=9, minute=30
        )
        + timedelta(minutes=15 * offset)
        for offset in range(8)
    ] + [
        datetime.combine(session_date, datetime.min.time(), tzinfo=timezone.utc).replace(hour=13)
        + timedelta(minutes=15 * offset)
        for offset in range(8)
    ]
    # The inputs are local Asia/Shanghai timestamps, so use an explicit +08 offset.
    local_stamps = [stamp.replace(tzinfo=timezone(timedelta(hours=8))) for stamp in stamps]
    result = aggregate_15m_to_4h(
        [_bar(key, stamp, close=100 + index) for index, stamp in enumerate(local_stamps)],
        calendar_id="cn_a",
        cutoff="2026-09-01T00:00:00Z",
        run_id="run-cn",
    )

    assert len(result.candles) == 1
    assert result.candles[0].key.timeframe == "4h"
    assert result.candles[0].volume == 160
    assert result.partial_buckets == ()
    assert result.transform_receipt is not None
    assert result.transform_receipt.aggregation_rule_version == "cn_a_session_4h_v1"
    assert result.transform_receipt.bucket_anchor == "09:30"
    assert result.transform_receipt.partial_bucket_policy == "drop_and_record"
    assert result.transform_receipt.partial_bucket_count == 0


def test_us_regular_session_records_closing_stub_without_publishing_it() -> None:
    key = _key()
    zone = timezone(timedelta(hours=-4))
    start = datetime(2026, 8, 31, 9, 30, tzinfo=zone)
    candles = [
        _bar(key, start + timedelta(minutes=15 * offset), close=100 + offset)
        for offset in range(26)
    ]

    result = aggregate_15m_to_4h(
        candles,
        calendar_id="us_equities",
        cutoff="2026-09-01T00:00:00Z",
        run_id="run-us",
    )

    assert len(result.candles) == 1
    assert len(result.partial_buckets) == 1
    assert any(issue.status == "partial" for issue in result.issues)
    assert result.transform_receipt is not None
    assert result.transform_receipt.partial_bucket_count == 1


def test_crypto_24x7_4h_requires_sixteen_closed_15m_bars() -> None:
    key = _key(instrument_id="CRYPTO.BTC", display_symbol="BTC")
    start = datetime(2026, 8, 31, tzinfo=timezone.utc)
    candles = [_bar(key, start + timedelta(minutes=15 * offset)) for offset in range(16)]

    result = aggregate_15m_to_4h(
        candles,
        calendar_id="crypto_24x7",
        cutoff="2026-08-31T05:00:00Z",
        run_id="run-crypto",
    )

    assert len(result.candles) == 1
    assert result.candles[0].timestamp == "2026-08-31T00:00:00+00:00"


def test_4h_rejects_mixed_sources_and_drops_forming_rows() -> None:
    first = _key(source_id="source-a")
    second = _key(source_id="source-b")
    with pytest.raises(CalendarError, match="mixed source"):
        aggregate_15m_to_4h(
            [
                _bar(first, datetime(2026, 8, 31, 9, 30, tzinfo=timezone.utc)),
                _bar(second, datetime(2026, 8, 31, 9, 45, tzinfo=timezone.utc)),
            ],
            calendar_id="crypto_24x7",
            cutoff="2026-09-01T00:00:00Z",
        )

    candles = [
        _bar(
            first, datetime(2026, 8, 31, 0, 0, tzinfo=timezone.utc) + timedelta(minutes=15 * offset)
        )
        for offset in range(16)
    ]
    result = aggregate_15m_to_4h(
        candles,
        calendar_id="crypto_24x7",
        cutoff="2026-08-31T03:45:00Z",
    )
    assert len(result.candles) == 0
    assert len(result.excluded_forming) == 1


def test_weekly_aggregation_excludes_current_week_and_accepts_holiday_week() -> None:
    key = _key(timeframe="1d")
    monday = date(2026, 8, 24)
    complete = [
        _bar(
            key,
            datetime.combine(
                monday + timedelta(days=offset),
                datetime.min.time(),
                tzinfo=timezone(timedelta(hours=-4)),
            ),
        )
        for offset in range(5)
    ]
    result = aggregate_daily_to_weekly(
        complete,
        calendar_id="us_equities",
        cutoff="2026-08-29T12:00:00Z",
        run_id="run-week",
    )
    assert len(result.candles) == 1
    assert result.candles[0].timestamp.startswith("2026-08-28T00:00:00")

    partial = aggregate_daily_to_weekly(
        complete[:3],
        calendar_id="us_equities",
        cutoff="2026-08-26T20:00:00Z",
    )
    assert len(partial.candles) == 0
    assert len(partial.partial_buckets) == 1


def test_quality_distinguishes_duplicate_order_forming_gap_missing_holiday_suspension_and_stale() -> (
    None
):
    key = _key()
    first = _bar(key, datetime(2026, 8, 31, 13, 30, tzinfo=timezone.utc))
    duplicate = _bar(key, datetime(2026, 8, 31, 13, 30, tzinfo=timezone.utc))
    out_of_order = _bar(key, datetime(2026, 8, 31, 13, 0, tzinfo=timezone.utc))
    report = assess_quality(
        [first, duplicate, out_of_order],
        timeframe="15m",
        calendar_id="crypto_24x7",
        cutoff="2026-08-31T14:00:00Z",
        expected_sessions=[date(2026, 8, 31), date(2026, 9, 1), date(2026, 9, 2)],
        suspension_dates=[date(2026, 9, 1)],
        stale_after=timedelta(minutes=10),
    )
    statuses = {issue.status for issue in report.issues}
    assert {"duplicate", "out_of_order", "missing", "suspension", "stale"}.issubset(statuses)
    assert report.status == "fail"

    holiday = assess_quality(
        [],
        timeframe="1d",
        calendar_id="us_equities",
        cutoff="2026-08-31T20:00:00Z",
        expected_sessions=[date(2026, 8, 30)],
        holidays=[date(2026, 8, 30)],
    )
    assert holiday.status == "fail"
    assert any(issue.status == "holiday" for issue in holiday.issues)


def test_calendar_spec_rejects_unknown_calendar() -> None:
    with pytest.raises(CalendarError, match="unknown market calendar"):
        calendar_spec("not-a-calendar")
