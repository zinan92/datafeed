"""Deterministic market calendars, closed-bar checks, and MVP transforms.

This module is intentionally pure: it consumes already-normalized candles and
returns derived candles plus receipts.  Providers and the storage adapter stay
behind their own seams.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
from typing import Iterable, Sequence
from zoneinfo import ZoneInfo

from kline.mvp_manifest import ManifestInstrument
from kline.storage import CandleSeriesKey, MvpCandle, TransformReceiptWrite


_FOUR_HOURS = timedelta(hours=4)
_ONE_HOUR = timedelta(hours=1)
_FIFTEEN_MINUTES = timedelta(minutes=15)
_ALLOWED_TIMEFRAMES = frozenset({"15m", "1h", "4h", "1d", "1w"})


class CalendarError(ValueError):
    """Calendar or aggregation input violates the MVP contract."""


@dataclass(frozen=True)
class SessionWindow:
    open_time: time
    close_time: time


@dataclass(frozen=True)
class CalendarSpec:
    calendar_id: str
    timezone: str
    sessions: tuple[SessionWindow, ...]
    continuous: bool
    four_hour_rule: str
    four_hour_anchor: time

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


@dataclass(frozen=True)
class AggregationIssue:
    status: str
    detail: str
    timestamp: str | None = None


@dataclass(frozen=True)
class AggregationResult:
    candles: tuple[MvpCandle, ...]
    transform_receipt: TransformReceiptWrite | None
    partial_buckets: tuple[str, ...]
    excluded_forming: tuple[str, ...]
    issues: tuple[AggregationIssue, ...]


@dataclass(frozen=True)
class QualityResult:
    status: str
    issues: tuple[AggregationIssue, ...]
    closed_count: int
    forming_count: int


_CALENDARS = {
    "cn_a": CalendarSpec(
        calendar_id="cn_a",
        timezone="Asia/Shanghai",
        sessions=(
            SessionWindow(time(9, 30), time(11, 30)),
            SessionWindow(time(13, 0), time(15, 0)),
        ),
        continuous=False,
        four_hour_rule="cn_a_session_4h_v1",
        four_hour_anchor=time(9, 30),
    ),
    "us_equities": CalendarSpec(
        calendar_id="us_equities",
        timezone="America/New_York",
        sessions=(SessionWindow(time(9, 30), time(16, 0)),),
        continuous=False,
        four_hour_rule="us_regular_fixed_4h_v1",
        four_hour_anchor=time(9, 30),
    ),
    "crypto_24x7": CalendarSpec(
        calendar_id="crypto_24x7",
        timezone="UTC",
        sessions=(),
        continuous=True,
        four_hour_rule="utc_fixed_4h_v1",
        four_hour_anchor=time(0, 0),
    ),
    "us_futures": CalendarSpec(
        calendar_id="us_futures",
        timezone="America/Chicago",
        sessions=(),
        continuous=True,
        four_hour_rule="us_futures_utc_fixed_4h_v1",
        four_hour_anchor=time(18, 0),
    ),
    "jp_equities": CalendarSpec(
        calendar_id="jp_equities",
        timezone="Asia/Tokyo",
        sessions=(
            SessionWindow(time(9, 0), time(11, 30)),
            SessionWindow(time(12, 30), time(15, 30)),
        ),
        continuous=False,
        four_hour_rule="jp_regular_fixed_4h_v1",
        four_hour_anchor=time(9, 0),
    ),
    "kr_equities": CalendarSpec(
        calendar_id="kr_equities",
        timezone="Asia/Seoul",
        sessions=(SessionWindow(time(9, 0), time(15, 30)),),
        continuous=False,
        four_hour_rule="kr_regular_fixed_4h_v1",
        four_hour_anchor=time(9, 0),
    ),
}


def calendar_spec(calendar_id: str) -> CalendarSpec:
    try:
        return _CALENDARS[calendar_id]
    except KeyError as exc:
        raise CalendarError(f"unknown market calendar: {calendar_id}") from exc


def resolve_calendar(instrument: ManifestInstrument) -> CalendarSpec:
    """Resolve and validate the calendar declared by one manifest cell."""

    spec = calendar_spec(instrument.calendar_id)
    if spec.timezone != instrument.timezone:
        try:
            ZoneInfo(instrument.timezone)
        except Exception as exc:
            raise CalendarError(
                f"{instrument.instrument_id} timezone {instrument.timezone} is invalid"
            ) from exc
        # A few instruments (for example VIX) use the same US regular-session
        # hours but publish timestamps in a different local timezone.
        spec = replace(spec, timezone=instrument.timezone)
    if not instrument.session_policy.strip():
        raise CalendarError(f"{instrument.instrument_id} has no session_policy")
    return spec


def _parse_stamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise CalendarError(f"invalid candle timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_cutoff(value: datetime | str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = _parse_stamp(value)
    else:
        raise CalendarError("cutoff must be an ISO timestamp or datetime")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _session_date(local_stamp: datetime) -> date:
    return local_stamp.date()


def is_trading_session(
    calendar_id: str,
    trading_date: date,
    *,
    holidays: Iterable[date] = (),
) -> bool:
    """Return whether a date is eligible for a regular-session calendar."""

    spec = calendar_spec(calendar_id)
    if spec.continuous:
        return True
    return trading_date.weekday() < 5 and trading_date not in set(holidays)


def _inside_session(local_stamp: datetime, spec: CalendarSpec) -> bool:
    if spec.continuous:
        return local_stamp.minute % 15 == 0 and local_stamp.second == 0
    local_time = local_stamp.timetz().replace(tzinfo=None)
    return any(
        window.open_time <= local_time < window.close_time
        and local_time.minute % 15 == 0
        and local_time.second == 0
        for window in spec.sessions
    )


def _bar_end(candle: MvpCandle, *, timeframe: str, spec: CalendarSpec) -> datetime:
    start = _parse_stamp(candle.timestamp)
    local = start.astimezone(spec.zone)
    if timeframe == "15m":
        return start + _FIFTEEN_MINUTES
    if timeframe == "4h":
        return start + _FOUR_HOURS
    if timeframe == "1h":
        return start + _ONE_HOUR
    if timeframe == "1d":
        if spec.continuous:
            return start + timedelta(days=1)
        close = spec.sessions[-1].close_time
        return datetime.combine(local.date(), close, tzinfo=spec.zone).astimezone(timezone.utc)
    if timeframe == "1w":
        days_to_friday = 4 - local.weekday()
        week_end = local.date() + timedelta(days=days_to_friday)
        if spec.continuous:
            week_end = local.date() + timedelta(days=6 - local.weekday())
            return datetime.combine(week_end, time(23, 59, 59), tzinfo=spec.zone).astimezone(
                timezone.utc
            )
        return datetime.combine(
            week_end, spec.sessions[-1].close_time, tzinfo=spec.zone
        ).astimezone(timezone.utc)
    raise CalendarError(f"unsupported timeframe: {timeframe}")


def _sequence_issues(candles: Sequence[MvpCandle]) -> list[AggregationIssue]:
    issues: list[AggregationIssue] = []
    seen: set[str] = set()
    previous: datetime | None = None
    source_ids: set[str] = set()
    for candle in candles:
        if not isinstance(candle, MvpCandle):
            issues.append(AggregationIssue("malformed", "input is not an MvpCandle"))
            continue
        source_ids.add(candle.key.source_id)
        try:
            stamp = _parse_stamp(candle.timestamp)
        except CalendarError:
            issues.append(AggregationIssue("malformed", "timestamp is not ISO", candle.timestamp))
            continue
        if candle.timestamp in seen:
            issues.append(
                AggregationIssue("duplicate", "duplicate candle timestamp", candle.timestamp)
            )
        seen.add(candle.timestamp)
        if previous is not None and stamp < previous:
            issues.append(
                AggregationIssue("out_of_order", "candles are not chronological", candle.timestamp)
            )
        previous = stamp
    if len(source_ids) > 1:
        issues.append(AggregationIssue("mixed_source", "input contains multiple source identities"))
    return issues


def _hash_candles(candles: Sequence[MvpCandle]) -> str:
    payload = [
        {
            "key": {
                "instrument_id": candle.key.instrument_id,
                "source_id": candle.key.source_id,
                "provider_symbol": candle.key.provider_symbol,
                "timeframe": candle.key.timeframe,
                "adjustment_basis": candle.key.adjustment_basis,
                "manifest_version": candle.key.manifest_version,
            },
            "timestamp": candle.timestamp,
            "open": candle.open,
            "high": candle.high,
            "low": candle.low,
            "close": candle.close,
            "volume": candle.volume,
        }
        for candle in candles
    ]
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _output_key(key: CandleSeriesKey, timeframe: str) -> CandleSeriesKey:
    return CandleSeriesKey(
        instrument_id=key.instrument_id,
        display_symbol=key.display_symbol,
        provider_symbol=key.provider_symbol,
        source_id=key.source_id,
        asset_class=key.asset_class,
        timeframe=timeframe,
        adjustment_basis=key.adjustment_basis,
        manifest_version=key.manifest_version,
    )


def _aggregate_rows(
    rows: Sequence[MvpCandle], key: CandleSeriesKey, timestamp: str, *, derived: bool
) -> MvpCandle:
    volume = None
    if rows[0].volume_semantics != "not_applicable":
        volume = sum(row.volume or 0 for row in rows)
    return MvpCandle(
        key=key,
        timestamp=timestamp,
        open=rows[0].open,
        high=max(row.high for row in rows),
        low=min(row.low for row in rows),
        close=rows[-1].close,
        volume=volume,
        amount=sum(row.amount or 0 for row in rows) if rows[0].amount is not None else None,
        volume_semantics=rows[0].volume_semantics,
        is_derived=derived,
    )


def aggregate_15m_to_4h(
    candles: Sequence[MvpCandle],
    *,
    calendar_id: str,
    cutoff: datetime | str,
    run_id: str = "aggregation-preview",
) -> AggregationResult:
    """Aggregate closed 15m rows into complete, calendar-aware 4H buckets."""

    if not candles:
        return AggregationResult((), None, (), (), ())
    if not all(isinstance(candle, MvpCandle) for candle in candles):
        raise CalendarError("malformed candle input")
    key = candles[0].key
    if key.timeframe != "15m":
        raise CalendarError("4H aggregation requires 15m input")
    if any(candle.key != key for candle in candles):
        raise CalendarError("mixed source/instrument identity in 4H input")
    if len({candle.volume_semantics for candle in candles}) > 1:
        raise CalendarError("mixed volume semantics in 4H input")
    spec = calendar_spec(calendar_id)
    cutoff_utc = _parse_cutoff(cutoff)
    issues = _sequence_issues(candles)
    if any(issue.status == "mixed_source" for issue in issues):
        raise CalendarError("mixed source input cannot be aggregated")

    grouped: dict[datetime, list[MvpCandle]] = {}
    excluded_forming: list[str] = []
    for candle in candles:
        stamp = _parse_stamp(candle.timestamp)
        local = stamp.astimezone(spec.zone)
        if _bar_end(candle, timeframe="15m", spec=spec) > cutoff_utc:
            excluded_forming.append(candle.timestamp)
            issues.append(AggregationIssue("forming", "bar ends after cutoff", candle.timestamp))
            continue
        if not spec.continuous and not _inside_session(local, spec):
            issues.append(
                AggregationIssue(
                    "out_of_session", "bar is outside regular session", candle.timestamp
                )
            )
            continue
        if not spec.continuous and not is_trading_session(calendar_id, local.date()):
            issues.append(
                AggregationIssue("holiday", "bar falls on a non-session date", candle.timestamp)
            )
            continue
        if spec.calendar_id == "cn_a":
            bucket = datetime.combine(local.date(), spec.four_hour_anchor, tzinfo=spec.zone)
        else:
            anchor = datetime.combine(local.date(), spec.four_hour_anchor, tzinfo=spec.zone)
            if local < anchor:
                anchor -= timedelta(days=1)
            bucket = anchor + ((local - anchor) // _FOUR_HOURS) * _FOUR_HOURS
        grouped.setdefault(bucket.astimezone(timezone.utc), []).append(candle)

    output_key = _output_key(key, "4h")
    output: list[MvpCandle] = []
    partial: list[str] = []
    for bucket, rows in sorted(grouped.items()):
        rows.sort(key=lambda item: _parse_stamp(item.timestamp))
        if spec.calendar_id == "cn_a":
            local_date = bucket.astimezone(spec.zone).date()
            expected_stamps = [
                datetime.combine(local_date, time(9, 30), tzinfo=spec.zone)
                + timedelta(minutes=15 * offset)
                for offset in range(8)
            ] + [
                datetime.combine(local_date, time(13, 0), tzinfo=spec.zone)
                + timedelta(minutes=15 * offset)
                for offset in range(8)
            ]
        elif spec.continuous:
            expected_stamps = [bucket + timedelta(minutes=15 * offset) for offset in range(16)]
        else:
            local_bucket = bucket.astimezone(spec.zone)
            expected_stamps = [
                local_bucket + timedelta(minutes=15 * offset) for offset in range(16)
            ]
            expected_stamps = [stamp.astimezone(timezone.utc) for stamp in expected_stamps]
        actual_stamps = [_parse_stamp(row.timestamp) for row in rows]
        if len(rows) != len(expected_stamps) or actual_stamps != sorted(expected_stamps):
            partial.append(bucket.isoformat())
            issues.append(AggregationIssue("partial", "incomplete 4H bucket", bucket.isoformat()))
            continue
        output.append(_aggregate_rows(rows, output_key, bucket.isoformat(), derived=True))

    receipt = None
    if output:
        receipt = TransformReceiptWrite(
            run_id=run_id,
            manifest_version=key.manifest_version,
            instrument_id=key.instrument_id,
            source_id=key.source_id,
            output_timeframe="4h",
            input_timeframe="15m",
            aggregation_rule_version=spec.four_hour_rule,
            input_start=min(candle.timestamp for candle in candles),
            input_end=max(candle.timestamp for candle in candles),
            input_hash=_hash_candles(candles),
            output_hash=_hash_candles(output),
            bucket_anchor=spec.four_hour_anchor.strftime("%H:%M"),
            partial_bucket_policy="drop_and_record",
            partial_bucket_count=len(partial),
        )
    return AggregationResult(
        tuple(output), receipt, tuple(partial), tuple(excluded_forming), tuple(issues)
    )


def aggregate_15m_to_1h(
    candles: Sequence[MvpCandle],
    *,
    calendar_id: str,
    cutoff: datetime | str,
    run_id: str = "aggregation-preview",
) -> AggregationResult:
    """Aggregate closed 15m rows into complete, calendar-aware 1H buckets."""

    if not candles:
        return AggregationResult((), None, (), (), ())
    if not all(isinstance(candle, MvpCandle) for candle in candles):
        raise CalendarError("malformed candle input")
    key = candles[0].key
    if key.timeframe != "15m":
        raise CalendarError("1H aggregation requires 15m input")
    if any(candle.key != key for candle in candles):
        raise CalendarError("mixed source/instrument identity in 1H input")
    if len({candle.volume_semantics for candle in candles}) > 1:
        raise CalendarError("mixed volume semantics in 1H input")
    spec = calendar_spec(calendar_id)
    cutoff_utc = _parse_cutoff(cutoff)
    issues = _sequence_issues(candles)
    if any(issue.status == "mixed_source" for issue in issues):
        raise CalendarError("mixed source input cannot be aggregated")

    grouped: dict[datetime, list[MvpCandle]] = {}
    excluded_forming: list[str] = []
    for candle in candles:
        stamp = _parse_stamp(candle.timestamp)
        local = stamp.astimezone(spec.zone)
        if _bar_end(candle, timeframe="15m", spec=spec) > cutoff_utc:
            excluded_forming.append(candle.timestamp)
            issues.append(AggregationIssue("forming", "bar ends after cutoff", candle.timestamp))
            continue
        if not spec.continuous and not _inside_session(local, spec):
            issues.append(
                AggregationIssue(
                    "out_of_session", "bar is outside regular session", candle.timestamp
                )
            )
            continue
        if not spec.continuous and not is_trading_session(calendar_id, local.date()):
            issues.append(
                AggregationIssue("holiday", "bar falls on a non-session date", candle.timestamp)
            )
            continue
        if spec.calendar_id == "cn_a":
            local_time = local.timetz().replace(tzinfo=None)
            anchor_time = time(13, 0) if local_time >= time(13, 0) else time(9, 30)
            anchor = datetime.combine(local.date(), anchor_time, tzinfo=spec.zone)
            bucket = anchor + ((local - anchor) // _ONE_HOUR) * _ONE_HOUR
        else:
            anchor = datetime.combine(local.date(), spec.four_hour_anchor, tzinfo=spec.zone)
            if local < anchor:
                anchor -= timedelta(days=1)
            bucket = anchor + ((local - anchor) // _ONE_HOUR) * _ONE_HOUR
        grouped.setdefault(bucket.astimezone(timezone.utc), []).append(candle)

    output_key = _output_key(key, "1h")
    output: list[MvpCandle] = []
    partial: list[str] = []
    for bucket, rows in sorted(grouped.items()):
        rows.sort(key=lambda item: _parse_stamp(item.timestamp))
        local_bucket = bucket.astimezone(spec.zone)
        expected_stamps = [
            (local_bucket + timedelta(minutes=15 * offset)).astimezone(timezone.utc)
            for offset in range(4)
        ]
        actual_stamps = [_parse_stamp(row.timestamp) for row in rows]
        if len(rows) != len(expected_stamps) or actual_stamps != sorted(expected_stamps):
            partial.append(bucket.isoformat())
            issues.append(AggregationIssue("partial", "incomplete 1H bucket", bucket.isoformat()))
            continue
        output.append(_aggregate_rows(rows, output_key, bucket.isoformat(), derived=True))

    receipt = None
    if output:
        receipt = TransformReceiptWrite(
            run_id=run_id,
            manifest_version=key.manifest_version,
            instrument_id=key.instrument_id,
            source_id=key.source_id,
            output_timeframe="1h",
            input_timeframe="15m",
            aggregation_rule_version=(
                "cn_a_session_1h_v1"
                if spec.calendar_id == "cn_a"
                else f"{spec.calendar_id}_fixed_1h_v1"
            ),
            input_start=min(candle.timestamp for candle in candles),
            input_end=max(candle.timestamp for candle in candles),
            input_hash=_hash_candles(candles),
            output_hash=_hash_candles(output),
            bucket_anchor=spec.four_hour_anchor.strftime("%H:%M"),
            partial_bucket_policy="drop_and_record",
            partial_bucket_count=len(partial),
        )
    return AggregationResult(
        tuple(output), receipt, tuple(partial), tuple(excluded_forming), tuple(issues)
    )


def aggregate_daily_to_weekly(
    candles: Sequence[MvpCandle],
    *,
    calendar_id: str,
    cutoff: datetime | str,
    run_id: str = "aggregation-preview",
) -> AggregationResult:
    """Aggregate daily rows into completed local-calendar weeks only."""

    if not candles:
        return AggregationResult((), None, (), (), ())
    if not all(isinstance(candle, MvpCandle) for candle in candles):
        raise CalendarError("malformed candle input")
    key = candles[0].key
    if key.timeframe != "1d":
        raise CalendarError("weekly aggregation requires 1d input")
    if any(candle.key != key for candle in candles):
        raise CalendarError("mixed source/instrument identity in weekly input")
    if len({candle.volume_semantics for candle in candles}) > 1:
        raise CalendarError("mixed volume semantics in weekly input")
    spec = calendar_spec(calendar_id)
    cutoff_utc = _parse_cutoff(cutoff)
    issues = _sequence_issues(candles)
    if any(issue.status == "mixed_source" for issue in issues):
        raise CalendarError("mixed source input cannot be aggregated")
    grouped: dict[tuple[int, int], list[MvpCandle]] = {}
    excluded_forming: list[str] = []
    for candle in candles:
        end = _bar_end(candle, timeframe="1d", spec=spec)
        if end > cutoff_utc:
            excluded_forming.append(candle.timestamp)
            issues.append(AggregationIssue("forming", "daily bar is not closed", candle.timestamp))
            continue
        local = _parse_stamp(candle.timestamp).astimezone(spec.zone)
        if not spec.continuous and not is_trading_session(calendar_id, local.date()):
            issues.append(
                AggregationIssue(
                    "holiday", "daily bar falls on a non-session date", candle.timestamp
                )
            )
            continue
        iso = local.date().isocalendar()
        grouped.setdefault((iso.year, iso.week), []).append(candle)

    output_key = _output_key(key, "1w")
    output: list[MvpCandle] = []
    partial: list[str] = []
    for (iso_year, iso_week), rows in sorted(grouped.items()):
        rows.sort(key=lambda item: _parse_stamp(item.timestamp))
        if spec.continuous:
            week_end = date.fromisocalendar(iso_year, iso_week, 7)
            week_close = datetime.combine(week_end, time(23, 59, 59), tzinfo=spec.zone)
        else:
            week_end = date.fromisocalendar(iso_year, iso_week, 5)
            week_close = datetime.combine(week_end, spec.sessions[-1].close_time, tzinfo=spec.zone)
        if week_close.astimezone(timezone.utc) > cutoff_utc:
            partial.append(week_end.isoformat())
            issues.append(
                AggregationIssue("partial", "current week is not closed", week_end.isoformat())
            )
            continue
        output.append(_aggregate_rows(rows, output_key, week_end.isoformat(), derived=True))

    receipt = None
    if output:
        receipt = TransformReceiptWrite(
            run_id=run_id,
            manifest_version=key.manifest_version,
            instrument_id=key.instrument_id,
            source_id=key.source_id,
            output_timeframe="1w",
            input_timeframe="1d",
            aggregation_rule_version="completed_local_calendar_week_v1",
            input_start=min(candle.timestamp for candle in candles),
            input_end=max(candle.timestamp for candle in candles),
            input_hash=_hash_candles(candles),
            output_hash=_hash_candles(output),
            bucket_anchor="local_week",
            partial_bucket_policy="defer_until_closed",
            partial_bucket_count=len(partial),
        )
    return AggregationResult(
        tuple(output), receipt, tuple(partial), tuple(excluded_forming), tuple(issues)
    )


def assess_quality(
    candles: Sequence[MvpCandle],
    *,
    timeframe: str,
    calendar_id: str,
    cutoff: datetime | str,
    expected_sessions: Iterable[date] = (),
    holidays: Iterable[date] = (),
    suspension_dates: Iterable[date] = (),
    stale_after: timedelta | None = None,
    market_open_buffer_minutes: int = 0,
) -> QualityResult:
    """Classify closed/forming/holiday/suspension/gap/duplicate/stale input."""

    if timeframe not in _ALLOWED_TIMEFRAMES:
        raise CalendarError(f"unsupported quality timeframe: {timeframe}")
    if market_open_buffer_minutes < 0:
        raise CalendarError("market_open_buffer_minutes must be non-negative")
    spec = calendar_spec(calendar_id)
    cutoff_utc = _parse_cutoff(cutoff)
    issues = _sequence_issues(candles)
    if not candles:
        issues.append(AggregationIssue("missing", "no candle rows were supplied"))
    closed_count = 0
    forming_count = 0
    observed_dates: set[date] = set()
    latest_end: datetime | None = None
    closed_stamps: list[tuple[datetime, datetime]] = []
    for candle in candles:
        if not isinstance(candle, MvpCandle):
            continue
        local = _parse_stamp(candle.timestamp).astimezone(spec.zone)
        observed_dates.add(local.date())
        if (
            timeframe in {"15m", "1h", "4h"}
            and not spec.continuous
            and not _inside_session(local, spec)
        ):
            issues.append(
                AggregationIssue("malformed", "bar is outside regular session", candle.timestamp)
            )
        end = _bar_end(candle, timeframe=timeframe, spec=spec)
        if end > cutoff_utc:
            forming_count += 1
            issues.append(AggregationIssue("forming", "bar ends after cutoff", candle.timestamp))
        else:
            closed_count += 1
            latest_end = max(latest_end, end) if latest_end is not None else end
            closed_stamps.append((_parse_stamp(candle.timestamp), end))
    interval = {
        "15m": _FIFTEEN_MINUTES,
        "1h": _ONE_HOUR,
        "4h": _FOUR_HOURS,
        "1d": timedelta(days=1),
        "1w": timedelta(days=7),
    }[timeframe]
    for (previous_start, _), (current_start, _) in zip(closed_stamps, closed_stamps[1:]):
        delta = current_start - previous_start
        if delta <= interval * 1.5:
            continue
        previous_local = previous_start.astimezone(spec.zone)
        current_local = current_start.astimezone(spec.zone)
        same_session = spec.continuous or any(
            window.open_time <= previous_local.timetz().replace(tzinfo=None) < window.close_time
            and window.open_time <= current_local.timetz().replace(tzinfo=None) < window.close_time
            for window in spec.sessions
        )
        if same_session and previous_local.date() == current_local.date():
            issues.append(
                AggregationIssue(
                    "gap", "closed candle interval is missing", current_start.isoformat()
                )
            )
    holiday_set = set(holidays)
    suspension_set = set(suspension_dates)
    for expected in expected_sessions:
        if expected in observed_dates:
            continue
        if expected in suspension_set:
            issues.append(
                AggregationIssue(
                    "suspension", "expected session is suspended", expected.isoformat()
                )
            )
        elif expected in holiday_set or not is_trading_session(
            calendar_id, expected, holidays=holiday_set
        ):
            issues.append(
                AggregationIssue(
                    "holiday", "expected date is a market holiday", expected.isoformat()
                )
            )
        else:
            issues.append(
                AggregationIssue("missing", "expected session has no candle", expected.isoformat())
            )
    if stale_after is not None and latest_end is not None and latest_end + stale_after < cutoff_utc:
        issues.append(
            AggregationIssue(
                "stale", "latest closed candle exceeded freshness window", latest_end.isoformat()
            )
        )
    blocking = {"malformed", "duplicate", "out_of_order", "mixed_source", "missing"}
    local_cutoff = cutoff_utc.astimezone(spec.zone)
    market_open_buffer_active = False
    if (
        market_open_buffer_minutes > 0
        and spec.calendar_id == "cn_a"
        and timeframe in {"15m", "1h"}
        and is_trading_session(spec.calendar_id, local_cutoff.date())
    ):
        first_open = datetime.combine(
            local_cutoff.date(), spec.sessions[0].open_time, tzinfo=spec.zone
        )
        market_open_buffer_active = (
            first_open
            <= local_cutoff
            < first_open + timedelta(minutes=market_open_buffer_minutes)
        )
    if not market_open_buffer_active:
        blocking.add("forming")
    status = (
        "fail"
        if any(issue.status in blocking for issue in issues)
        else "partial"
        if issues
        else "pass"
    )
    return QualityResult(status, tuple(issues), closed_count, forming_count)
