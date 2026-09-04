"""Interpret declared daily timestamps as market-session dates."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from kline.market_calendar import calendar_spec, is_trading_session
from kline.mvp_manifest import ManifestInstrument
from kline.time_utils import parse_utc_timestamp


SESSION_DATE_AT_LOCAL_MIDNIGHT = "session_date_at_local_midnight"
SESSION_DATE_AT_UTC_MIDNIGHT = "session_date_at_utc_midnight"
DAILY_TIMESTAMP_CONVENTIONS = frozenset(
    {SESSION_DATE_AT_LOCAL_MIDNIGHT, SESSION_DATE_AT_UTC_MIDNIGHT}
)
_MAX_SESSION_LOOKBACK_DAYS = 15


@dataclass(frozen=True)
class DailyFreshness:
    convention: str | None
    observed_session: date | None
    expected_session: date | None
    stale: bool | None


def assess_daily_freshness(
    instrument: ManifestInstrument,
    latest_timestamp: str | None,
    *,
    now: datetime,
) -> DailyFreshness:
    """Compare a declared daily timestamp with the latest closed market session."""

    convention = instrument.metadata.get("daily_timestamp_convention")
    if convention not in DAILY_TIMESTAMP_CONVENTIONS:
        return DailyFreshness(
            convention=str(convention) if convention is not None else None,
            observed_session=None,
            expected_session=None,
            stale=None,
        )
    latest = parse_utc_timestamp(latest_timestamp)
    if latest is None:
        return DailyFreshness(convention, None, None, None)
    try:
        spec = calendar_spec(instrument.calendar_id)
    except (KeyError, ValueError):
        return DailyFreshness(convention, None, None, None)
    if convention == SESSION_DATE_AT_LOCAL_MIDNIGHT:
        observed_session = latest.astimezone(spec.zone).date()
    else:
        observed_session = latest.astimezone(timezone.utc).date()

    if spec.continuous:
        expected_session = now.astimezone(timezone.utc).date() - timedelta(days=1)
        return DailyFreshness(
            convention=convention,
            observed_session=observed_session,
            expected_session=expected_session,
            stale=observed_session < expected_session,
        )
    if not spec.sessions:
        return DailyFreshness(convention, observed_session, None, None)

    local_now = now.astimezone(spec.zone)
    expected_session: date | None = None
    for offset in range(_MAX_SESSION_LOOKBACK_DAYS):
        candidate = local_now.date() - timedelta(days=offset)
        if not is_trading_session(instrument.calendar_id, candidate):
            continue
        close_at = datetime.combine(candidate, spec.sessions[-1].close_time, tzinfo=spec.zone)
        if close_at <= local_now:
            expected_session = candidate
            break
    return DailyFreshness(
        convention=convention,
        observed_session=observed_session,
        expected_session=expected_session,
        stale=(observed_session < expected_session) if expected_session is not None else None,
    )
