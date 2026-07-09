"""Candle quality checks shared by REST and live streams."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from kline.models import Candle, Timeframe
from kline.provenance import ProviderMeta, freshness, timeframe_seconds


@dataclass(frozen=True)
class QualityReport:
    quality_flags: list[str]
    access_issues: list[str]
    latest_timestamp: Optional[str]
    age_seconds: Optional[float]
    max_age_seconds: Optional[float]
    fresh: Optional[bool]
    reject_reason: Optional[str]


def _parse_ts(ts: str) -> Optional[datetime]:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def analyze_candles(
    candles: list[Candle],
    timeframe: Timeframe,
    meta: ProviderMeta,
    *,
    strict: bool = False,
    now: Optional[datetime] = None,
) -> QualityReport:
    """Detect stale, gap, duplicate, and out-of-order candle batches."""
    if not candles:
        return QualityReport(
            quality_flags=["empty"],
            access_issues=["upstream returned no candles"],
            latest_timestamp=None,
            age_seconds=None,
            max_age_seconds=None,
            fresh=False if strict else None,
            reject_reason="empty_data" if strict else None,
        )

    flags: list[str] = []
    issues: list[str] = []
    latest = candles[-1].timestamp
    age_seconds, max_age_seconds, fresh = freshness(latest, meta, timeframe, now=now)
    parsed = [_parse_ts(c.timestamp) for c in candles]

    if any(ts is None for ts in parsed):
        flags.append("invalid_timestamp")
        issues.append("one or more candle timestamps could not be parsed")
    else:
        assert all(ts is not None for ts in parsed)
        expected = timeframe_seconds(timeframe)
        for prev, cur in zip(parsed, parsed[1:]):
            assert prev is not None and cur is not None
            delta = (cur - prev).total_seconds()
            if delta == 0:
                if "duplicate_timestamp" not in flags:
                    flags.append("duplicate_timestamp")
                    issues.append("one or more candle timestamps are duplicated")
            elif delta < 0:
                if "out_of_order" not in flags:
                    flags.append("out_of_order")
                    issues.append("candles are not strictly sorted oldest to newest")
            elif (meta.continuous or strict) and delta > expected * 1.5:
                if "gap" not in flags:
                    flags.append("gap")
                    issues.append("one or more candle intervals are missing")

    if fresh is False:
        flags.append("stale")
        issues.append("latest candle is older than max_age_seconds")

    reject_reason = None
    if strict:
        critical = [
            "empty",
            "invalid_timestamp",
            "duplicate_timestamp",
            "out_of_order",
            "gap",
            "stale",
        ]
        reject_reason = next((flag for flag in critical if flag in flags), None)

    return QualityReport(
        quality_flags=flags,
        access_issues=issues,
        latest_timestamp=latest,
        age_seconds=age_seconds,
        max_age_seconds=max_age_seconds,
        fresh=fresh,
        reject_reason=reject_reason,
    )
