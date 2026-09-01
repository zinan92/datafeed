"""Read-only provider preflight for a bounded real-data pilot.

This module is deliberately an operations seam, not a production provider.
It probes public/explicitly configured endpoints, normalizes the response into
small immutable bars, derives only the contract-approved 4h/weekly previews,
and writes a receipt.  It never opens the resident database and never stores
credentials or upstream payload bodies.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import math
from pathlib import Path
import sqlite3
import time
from typing import Any, Mapping, Protocol, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from kline.market_calendar import aggregate_15m_to_4h, aggregate_daily_to_weekly
from kline.storage import CandleSeriesKey, MvpCandle


TIMEFRAMES = ("15m", "1h", "4h", "1d", "1w")
BASE_TIMEFRAMES = ("15m", "1h", "1d")
PRE_FLIGHT_SCHEMA = "provider-preflight-v1"


@dataclass(frozen=True)
class PolicyReceipt:
    """Operator-supplied source rights, never inferred from HTTP access."""

    status: str = "unverified"
    persistence_allowed: bool | None = None
    derived_allowed: bool | None = None
    non_display_allowed: bool | None = None
    valid_from: str | None = None
    valid_to: str | None = None
    evidence_ref: str = "operator_review_required"

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "persistence_allowed": self.persistence_allowed,
            "derived_allowed": self.derived_allowed,
            "non_display_allowed": self.non_display_allowed,
            "valid_from": self.valid_from,
            "valid_to": self.valid_to,
            "evidence_ref": self.evidence_ref,
        }


@dataclass(frozen=True)
class PreflightTarget:
    asset_class: str
    display_symbol: str
    provider_symbol: str
    source_id: str
    source_kind: str
    calendar_id: str
    timezone: str
    volume_semantics: str
    requested_timeframes: tuple[str, ...] = TIMEFRAMES
    policy: PolicyReceipt = field(default_factory=PolicyReceipt)
    display_name: str | None = None
    volume_scope: str = "unknown"
    volume_completeness: str = "unknown"

    def __post_init__(self) -> None:
        if not self.display_symbol.strip() or not self.provider_symbol.strip():
            raise ValueError("preflight target symbols must be non-empty")
        if not set(self.requested_timeframes).issubset(TIMEFRAMES):
            raise ValueError("preflight target contains an unsupported timeframe")
        if "30m" in self.requested_timeframes:
            raise ValueError("30m is excluded from the preflight contract")

    @property
    def instrument_id(self) -> str:
        return f"{self.asset_class}:{self.display_symbol}"


@dataclass(frozen=True)
class Bar:
    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: float | None
    amount: float | None
    is_derived: bool = False


@dataclass(frozen=True)
class QualityReport:
    status: str
    invalid_rows: int = 0
    duplicates: int = 0
    out_of_order: int = 0
    gaps: int = 0
    forming_rows: int = 0
    missing_volume: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "invalid_rows": self.invalid_rows,
            "duplicates": self.duplicates,
            "out_of_order": self.out_of_order,
            "gaps": self.gaps,
            "forming_rows": self.forming_rows,
            "missing_volume": self.missing_volume,
        }


@dataclass(frozen=True)
class ParsedSeries:
    bars: tuple[Bar, ...]
    quality: QualityReport
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DerivedSeries:
    bars: tuple[Bar, ...]
    transform: Mapping[str, Any] | None
    quality: QualityReport


@dataclass(frozen=True)
class ClassifiedCell:
    target: PreflightTarget
    timeframe: str
    status: str
    status_reason: str
    bars: tuple[Bar, ...] = ()
    quality: QualityReport = field(default_factory=lambda: QualityReport("unavailable"))
    is_derived: bool | None = None
    transform: Mapping[str, Any] | None = None
    error: Mapping[str, Any] | None = None

    def as_dict(
        self,
        *,
        request: Mapping[str, Any] | None = None,
        response: Mapping[str, Any] | None = None,
        observed_at: str | None = None,
    ) -> dict[str, Any]:
        latest = self.bars[-1].timestamp if self.bars else None
        policy = self.target.policy.as_dict()
        return {
            "instrument_id": self.target.instrument_id,
            "display_symbol": self.target.display_symbol,
            "display_name": self.target.display_name,
            "provider_symbol": self.target.provider_symbol,
            "asset_class": self.target.asset_class,
            "source_id": self.target.source_id,
            "source_kind": self.target.source_kind,
            "calendar_id": self.target.calendar_id,
            "timezone": self.target.timezone,
            "timeframe": self.timeframe,
            "status": self.status,
            "status_reason": self.status_reason,
            "row_count": len(self.bars),
            "latest_closed_timestamp": latest,
            "volume_semantics": self.target.volume_semantics,
            "volume_scope": self.target.volume_scope,
            "volume_completeness": self.target.volume_completeness,
            "is_derived": self.is_derived,
            "transform": dict(self.transform) if self.transform else None,
            "quality": self.quality.as_dict(),
            "entitlement": policy,
            "request": dict(request or {}),
            "response": dict(response or {}),
            "observed_at": observed_at,
            "error": dict(self.error) if self.error else None,
        }


@dataclass(frozen=True)
class HttpObservation:
    status_code: int
    body: bytes
    payload: Any | None
    elapsed_ms: float
    error: str | None = None

    @property
    def response_hash(self) -> str | None:
        return sha256(self.body).hexdigest() if self.body else None


class JsonFetcher(Protocol):
    def __call__(
        self, url: str, *, params: Mapping[str, str], timeout: float
    ) -> HttpObservation: ...


class PreflightParseError(ValueError):
    """An upstream response cannot be normalized into the preflight shape."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _parse_zone(timezone_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone_name)
    except Exception as exc:  # pragma: no cover - ZoneInfo error text varies
        raise PreflightParseError("invalid_timezone", f"invalid timezone: {timezone_name}") from exc


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _bar_end(timestamp: datetime, timeframe: str, *, calendar_id: str, zone: ZoneInfo) -> datetime:
    local = timestamp.astimezone(zone)
    if timeframe == "15m":
        return timestamp + timedelta(minutes=15)
    if timeframe == "1h":
        return timestamp + timedelta(hours=1)
    if timeframe == "1d":
        if calendar_id == "cn_a":
            return datetime.combine(
                local.date(), datetime.min.time().replace(hour=15), tzinfo=zone
            ).astimezone(timezone.utc)
        if calendar_id in {"us_equities", "us_futures"}:
            return datetime.combine(
                local.date(), datetime.min.time().replace(hour=16), tzinfo=zone
            ).astimezone(timezone.utc)
        return timestamp + timedelta(days=1)
    if timeframe == "1w":
        return timestamp + timedelta(days=7)
    raise PreflightParseError("unsupported_timeframe", f"unsupported parser timeframe: {timeframe}")


def _is_closed(
    timestamp: datetime,
    timeframe: str,
    *,
    calendar_id: str,
    zone: ZoneInfo,
    now: datetime,
) -> bool:
    return _bar_end(timestamp, timeframe, calendar_id=calendar_id, zone=zone) <= now.astimezone(
        timezone.utc
    )


def _quality(
    bars: Sequence[Bar], *, invalid_rows: int, forming_rows: int, missing_volume: int = 0
) -> QualityReport:
    duplicates = 0
    out_of_order = 0
    seen: set[str] = set()
    previous: str | None = None
    for bar in bars:
        if bar.timestamp in seen:
            duplicates += 1
        if previous is not None and bar.timestamp < previous:
            out_of_order += 1
        seen.add(bar.timestamp)
        previous = bar.timestamp
    status = "unavailable" if not bars else "ready"
    if bars and (invalid_rows or duplicates or out_of_order):
        status = "partial"
    return QualityReport(
        status=status,
        invalid_rows=invalid_rows,
        duplicates=duplicates,
        out_of_order=out_of_order,
        forming_rows=forming_rows,
        missing_volume=missing_volume,
    )


def _finite_ohlc(values: Sequence[Any]) -> tuple[float, float, float, float] | None:
    try:
        numbers = tuple(float(value) for value in values)
    except (TypeError, ValueError):
        return None
    if len(numbers) != 4 or not all(math.isfinite(value) for value in numbers):
        return None
    open_value, high_value, low_value, close_value = numbers
    if high_value < max(open_value, close_value) or low_value > min(open_value, close_value):
        return None
    return open_value, high_value, low_value, close_value


def parse_yahoo_chart_payload(
    payload: Mapping[str, Any],
    *,
    provider_symbol: str,
    timeframe: str,
    calendar_id: str,
    timezone_name: str,
    now: datetime,
) -> ParsedSeries:
    """Parse Yahoo chart JSON and drop forming/invalid rows with counts."""

    chart = payload.get("chart") if isinstance(payload, Mapping) else None
    result = chart.get("result") if isinstance(chart, Mapping) else None
    if not isinstance(result, list) or not result or not isinstance(result[0], Mapping):
        error = chart.get("error") if isinstance(chart, Mapping) else None
        raise PreflightParseError(
            "empty_response", f"Yahoo chart result unavailable: {error or 'empty'}"
        )
    root = result[0]
    meta = root.get("meta") if isinstance(root.get("meta"), Mapping) else {}
    timestamps = root.get("timestamp")
    indicators = root.get("indicators") if isinstance(root.get("indicators"), Mapping) else {}
    quotes = indicators.get("quote") if isinstance(indicators.get("quote"), list) else []
    quote = quotes[0] if quotes and isinstance(quotes[0], Mapping) else {}
    if not isinstance(timestamps, list):
        raise PreflightParseError("missing_timestamps", "Yahoo chart timestamps are missing")

    zone = _parse_zone(timezone_name or str(meta.get("exchangeTimezoneName") or "UTC"))
    bars: list[Bar] = []
    invalid = 0
    forming = 0
    missing_volume = 0
    for index, raw_timestamp in enumerate(timestamps):
        try:
            timestamp = datetime.fromtimestamp(float(raw_timestamp), tz=timezone.utc)
        except (TypeError, ValueError, OverflowError):
            invalid += 1
            continue
        if not _is_closed(timestamp, timeframe, calendar_id=calendar_id, zone=zone, now=now):
            forming += 1
            continue
        ohlc = _finite_ohlc(
            [
                (quote.get("open") or [None])[index]
                if index < len(quote.get("open") or [])
                else None,
                (quote.get("high") or [None])[index]
                if index < len(quote.get("high") or [])
                else None,
                (quote.get("low") or [None])[index]
                if index < len(quote.get("low") or [])
                else None,
                (quote.get("close") or [None])[index]
                if index < len(quote.get("close") or [])
                else None,
            ]
        )
        if ohlc is None:
            invalid += 1
            continue
        raw_volume = (
            (quote.get("volume") or [None])[index]
            if index < len(quote.get("volume") or [])
            else None
        )
        try:
            volume = (
                float(raw_volume)
                if raw_volume is not None and math.isfinite(float(raw_volume))
                else None
            )
        except (TypeError, ValueError):
            volume = None
        if volume is None:
            missing_volume += 1
        bars.append(Bar(_stamp(timestamp), *ohlc, volume, None))
    quality = _quality(
        tuple(bars), invalid_rows=invalid, forming_rows=forming, missing_volume=missing_volume
    )
    ordered = tuple(sorted(bars, key=lambda bar: bar.timestamp))
    return ParsedSeries(
        bars=ordered,
        quality=quality,
        metadata={
            "provider": "yahoo_finance",
            "provider_symbol": provider_symbol,
            "exchange_timezone": str(meta.get("exchangeTimezoneName") or timezone_name),
            "exchange_name": meta.get("fullExchangeName") or meta.get("exchangeName"),
            "currency": meta.get("currency"),
        },
    )


def parse_eastmoney_payload(
    payload: Mapping[str, Any],
    *,
    provider_symbol: str,
    timeframe: str,
    timezone_name: str,
    now: datetime,
) -> ParsedSeries:
    """Parse Eastmoney ``qt/stock/kline/get`` rows into canonical bars."""

    if not isinstance(payload, Mapping) or payload.get("rc") not in {0, "0"}:
        raise PreflightParseError(
            "upstream_rejected",
            f"Eastmoney response rejected: {payload.get('msg') if isinstance(payload, Mapping) else 'invalid'}",
        )
    data = payload.get("data")
    rows = data.get("klines") if isinstance(data, Mapping) else None
    if rows is None:
        rows = []
    if not isinstance(rows, list):
        raise PreflightParseError("malformed_rows", "Eastmoney klines is not a list")

    zone = _parse_zone(timezone_name)
    bars: list[Bar] = []
    invalid = 0
    forming = 0
    missing_volume = 0
    for raw_row in rows:
        if not isinstance(raw_row, str):
            invalid += 1
            continue
        fields = [field.strip() for field in raw_row.split(",")]
        if len(fields) < 7:
            invalid += 1
            continue
        try:
            local_stamp = datetime.fromisoformat(fields[0])
            local_stamp = (
                local_stamp.replace(tzinfo=zone)
                if local_stamp.tzinfo is None
                else local_stamp.astimezone(zone)
            )
            timestamp = local_stamp.astimezone(timezone.utc)
        except ValueError:
            invalid += 1
            continue
        if not _is_closed(timestamp, timeframe, calendar_id="cn_a", zone=zone, now=now):
            forming += 1
            continue
        # Eastmoney's row order is timestamp, open, close, high, low, volume, amount.
        ohlc = _finite_ohlc([fields[1], fields[3], fields[4], fields[2]])
        if ohlc is None:
            invalid += 1
            continue
        try:
            volume = float(fields[5])
            amount = float(fields[6])
            if not math.isfinite(volume) or not math.isfinite(amount) or volume < 0 or amount < 0:
                raise ValueError
        except (TypeError, ValueError):
            invalid += 1
            continue
        bars.append(Bar(_stamp(timestamp), *ohlc, volume, amount))
    quality = _quality(
        tuple(bars), invalid_rows=invalid, forming_rows=forming, missing_volume=missing_volume
    )
    ordered = tuple(sorted(bars, key=lambda bar: bar.timestamp))
    return ParsedSeries(
        bars=ordered,
        quality=quality,
        metadata={
            "provider": "eastmoney",
            "provider_symbol": provider_symbol,
            "market": data.get("market") if isinstance(data, Mapping) else None,
            "name": data.get("name") if isinstance(data, Mapping) else None,
        },
    )


def _default_quality(bars: Sequence[Bar]) -> QualityReport:
    return _quality(tuple(bars), invalid_rows=0, forming_rows=0)


def classify_status(
    target: PreflightTarget,
    timeframe: str,
    bars: Sequence[Bar],
    *,
    policy: PolicyReceipt | None = None,
    quality: QualityReport | None = None,
    is_derived: bool = False,
    transform: Mapping[str, Any] | None = None,
    error: Mapping[str, Any] | None = None,
) -> ClassifiedCell:
    """Classify technical rows without treating access as persistence rights."""

    active_policy = policy or target.policy
    normalized = tuple(sorted(bars, key=lambda bar: bar.timestamp))
    report = quality or _default_quality(normalized)
    if error:
        status = (
            "blocked"
            if error.get("code") in {"persistence_not_allowed", "entitlement_unverified"}
            else "unavailable"
        )
        return ClassifiedCell(
            target,
            timeframe,
            status,
            str(error.get("code") or "request_failed"),
            normalized,
            report,
            is_derived,
            transform,
            error,
        )
    if not normalized:
        return ClassifiedCell(
            target,
            timeframe,
            "unavailable",
            "no_closed_bars",
            normalized,
            report,
            is_derived,
            transform,
        )
    if report.invalid_rows or report.duplicates or report.out_of_order:
        return ClassifiedCell(
            target,
            timeframe,
            "blocked",
            "quality_invalid",
            normalized,
            report,
            is_derived,
            transform,
        )
    if target.volume_semantics != "not_applicable" and report.missing_volume:
        return ClassifiedCell(
            target,
            timeframe,
            "blocked",
            "volume_missing",
            normalized,
            report,
            is_derived,
            transform,
        )
    if active_policy.status in {"blocked", "expired"}:
        return ClassifiedCell(
            target,
            timeframe,
            "blocked",
            f"entitlement_{active_policy.status}",
            normalized,
            report,
            is_derived,
            transform,
        )
    if is_derived and active_policy.derived_allowed is False:
        return ClassifiedCell(
            target,
            timeframe,
            "blocked",
            "derived_not_allowed",
            normalized,
            report,
            is_derived,
            transform,
        )
    if active_policy.persistence_allowed is False:
        return ClassifiedCell(
            target,
            timeframe,
            "blocked",
            "persistence_not_allowed",
            normalized,
            report,
            is_derived,
            transform,
        )
    if active_policy.non_display_allowed is False:
        return ClassifiedCell(
            target,
            timeframe,
            "blocked",
            "non_display_not_allowed",
            normalized,
            report,
            is_derived,
            transform,
        )
    if (
        active_policy.persistence_allowed is not True
        or active_policy.derived_allowed is not True
        and is_derived
        or active_policy.non_display_allowed is not True
    ):
        return ClassifiedCell(
            target,
            timeframe,
            "partial",
            "entitlement_unverified",
            normalized,
            report,
            is_derived,
            transform,
        )
    return ClassifiedCell(
        target,
        timeframe,
        "ready",
        "real_rows_quality_passed",
        normalized,
        report,
        is_derived,
        transform,
    )


def _mvp_key(target: PreflightTarget, timeframe: str) -> CandleSeriesKey:
    return CandleSeriesKey(
        instrument_id=target.instrument_id,
        display_symbol=target.display_symbol,
        provider_symbol=target.provider_symbol,
        source_id=target.source_id,
        asset_class=target.asset_class,
        timeframe=timeframe,
        adjustment_basis="raw",
        manifest_version="preflight-v1",
    )


def _to_mvp(target: PreflightTarget, timeframe: str, bar: Bar, *, derived: bool) -> MvpCandle:
    volume = None if target.volume_semantics == "not_applicable" else bar.volume
    if target.volume_semantics != "not_applicable" and volume is None:
        raise PreflightParseError("volume_missing", f"volume missing for {target.instrument_id}")
    return MvpCandle(
        key=_mvp_key(target, timeframe),
        timestamp=bar.timestamp,
        open=bar.open,
        high=bar.high,
        low=bar.low,
        close=bar.close,
        volume=volume,
        amount=bar.amount,
        volume_semantics=target.volume_semantics,
        is_derived=derived,
    )


def _from_mvp(row: MvpCandle) -> Bar:
    return Bar(
        row.timestamp,
        row.open,
        row.high,
        row.low,
        row.close,
        row.volume,
        row.amount,
        row.is_derived,
    )


def derive_series(
    target: PreflightTarget,
    *,
    input_timeframe: str,
    output_timeframe: str,
    bars: Sequence[Bar],
    now: datetime,
) -> DerivedSeries:
    """Run the canonical calendar-aware 15m→4h or 1d→1w transform."""

    if output_timeframe == "4h" and input_timeframe != "15m":
        raise ValueError("4h preflight derivation requires 15m input")
    if output_timeframe == "1w" and input_timeframe != "1d":
        raise ValueError("weekly preflight derivation requires 1d input")
    mvp_rows = tuple(_to_mvp(target, input_timeframe, bar, derived=False) for bar in bars)
    if output_timeframe == "4h":
        result = aggregate_15m_to_4h(
            mvp_rows,
            calendar_id=target.calendar_id,
            cutoff=now,
            run_id=f"preflight:{target.instrument_id}:4h",
        )
    elif output_timeframe == "1w":
        result = aggregate_daily_to_weekly(
            mvp_rows,
            calendar_id=target.calendar_id,
            cutoff=now,
            run_id=f"preflight:{target.instrument_id}:1w",
        )
    else:
        raise ValueError(f"unsupported derived timeframe: {output_timeframe}")
    transform = None
    if result.transform_receipt:
        receipt = result.transform_receipt
        transform = {
            "input_timeframe": receipt.input_timeframe,
            "output_timeframe": receipt.output_timeframe,
            "aggregation_rule_version": receipt.aggregation_rule_version,
            "input_start": receipt.input_start,
            "input_end": receipt.input_end,
            "input_hash": receipt.input_hash,
            "output_hash": receipt.output_hash,
            "bucket_anchor": receipt.bucket_anchor,
            "partial_bucket_policy": receipt.partial_bucket_policy,
            "partial_bucket_count": receipt.partial_bucket_count,
        }
    output = tuple(_from_mvp(row) for row in result.candles)
    quality = QualityReport("ready" if output else "unavailable", gaps=len(result.partial_buckets))
    return DerivedSeries(output, transform, quality)


def idempotency_check(
    *,
    source_id: str,
    instrument_id: str,
    timeframe: str,
    bars: Sequence[Bar],
) -> dict[str, Any]:
    """Upsert bars twice in memory and prove the canonical key is stable."""

    connection = sqlite3.connect(":memory:")
    connection.execute(
        "CREATE TABLE bars (source_id TEXT NOT NULL, instrument_id TEXT NOT NULL, timeframe TEXT NOT NULL, timestamp TEXT NOT NULL, open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL, volume REAL, amount REAL, PRIMARY KEY (source_id, instrument_id, timeframe, timestamp))"
    )

    def insert_rows() -> int:
        before = int(connection.execute("SELECT COUNT(*) FROM bars").fetchone()[0])
        for bar in bars:
            connection.execute(
                "INSERT INTO bars VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(source_id, instrument_id, timeframe, timestamp) DO UPDATE SET open=excluded.open, high=excluded.high, low=excluded.low, close=excluded.close, volume=excluded.volume, amount=excluded.amount",
                (
                    source_id,
                    instrument_id,
                    timeframe,
                    bar.timestamp,
                    bar.open,
                    bar.high,
                    bar.low,
                    bar.close,
                    bar.volume,
                    bar.amount,
                ),
            )
        connection.commit()
        after = int(connection.execute("SELECT COUNT(*) FROM bars").fetchone()[0])
        return after - before

    first_inserted = insert_rows()
    second_inserted = insert_rows()
    row_count = int(connection.execute("SELECT COUNT(*) FROM bars").fetchone()[0])
    duplicate_keys = int(
        connection.execute(
            "SELECT COUNT(*) - COUNT(DISTINCT source_id || '|' || instrument_id || '|' || timeframe || '|' || timestamp) FROM bars"
        ).fetchone()[0]
    )
    connection.close()
    return {
        "first_inserted": first_inserted,
        "second_inserted": second_inserted,
        "row_count_after_rerun": row_count,
        "duplicate_keys": duplicate_keys,
    }


def fetch_json(url: str, *, params: Mapping[str, str], timeout: float) -> HttpObservation:
    """GET JSON without logging URLs that could contain future credentials."""

    query = urlencode(params)
    request = Request(
        f"{url}?{query}",
        headers={"Accept": "application/json", "User-Agent": "datafeed-provider-preflight/1"},
        method="GET",
    )
    started = time.monotonic()
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read()
            status = int(getattr(response, "status", 200))
    except HTTPError as error:
        body = error.read()
        return HttpObservation(
            int(error.code), body, _safe_json(body), _elapsed_ms(started), type(error).__name__
        )
    except (URLError, TimeoutError, OSError) as error:
        return HttpObservation(
            0, b"", None, _elapsed_ms(started), f"{type(error).__name__}: {error}"
        )
    payload = _safe_json(body)
    return HttpObservation(
        status, body, payload, _elapsed_ms(started), None if payload is not None else "invalid_json"
    )


def _safe_json(body: bytes) -> Any | None:
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _elapsed_ms(started: float) -> float:
    return round((time.monotonic() - started) * 1000, 2)


def _yahoo_request(target: PreflightTarget, timeframe: str) -> tuple[str, dict[str, str]]:
    interval = {"15m": "15m", "1h": "1h", "1d": "1d"}[timeframe]
    range_value = "5d" if timeframe in {"15m", "1h"} else "1mo"
    return (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{target.provider_symbol}",
        {
            "interval": interval,
            "range": range_value,
            "includePrePost": "false",
            "events": "div,splits",
        },
    )


def _eastmoney_secid(symbol: str) -> str:
    normalized = symbol.upper().split(".")[0]
    market = "1" if normalized.startswith(("6", "68", "9")) else "0"
    return f"{market}.{normalized}"


def _eastmoney_request(
    target: PreflightTarget, timeframe: str, *, now: datetime
) -> tuple[str, dict[str, str]]:
    klt = {"15m": "15", "1h": "60", "1d": "101"}[timeframe]
    days = 45 if timeframe in {"15m", "1h"} else 730
    return (
        "https://push2his.eastmoney.com/api/qt/stock/kline/get",
        {
            "secid": _eastmoney_secid(target.provider_symbol),
            "klt": klt,
            "fqt": "1",
            "beg": (now.date() - timedelta(days=days)).strftime("%Y%m%d"),
            "end": (now.date() + timedelta(days=1)).strftime("%Y%m%d"),
            "lmt": "4000",
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        },
    )


DEFAULT_TARGETS: tuple[PreflightTarget, ...] = (
    PreflightTarget(
        "a_share",
        "600519",
        "600519",
        "eastmoney_kline",
        "eastmoney",
        "cn_a",
        "Asia/Shanghai",
        "traded",
        display_name="贵州茅台",
        volume_scope="venue_reported",
    ),
    PreflightTarget(
        "a_share",
        "300750",
        "300750",
        "eastmoney_kline",
        "eastmoney",
        "cn_a",
        "Asia/Shanghai",
        "traded",
        display_name="宁德时代",
        volume_scope="venue_reported",
    ),
    PreflightTarget(
        "a_share",
        "688981",
        "688981",
        "eastmoney_kline",
        "eastmoney",
        "cn_a",
        "Asia/Shanghai",
        "traded",
        display_name="中芯国际",
        volume_scope="venue_reported",
    ),
    PreflightTarget(
        "us_stock",
        "AAPL",
        "AAPL",
        "yahoo_chart",
        "yahoo_chart",
        "us_equities",
        "America/New_York",
        "traded",
        display_name="Apple",
        volume_scope="provider_reported",
    ),
    PreflightTarget(
        "us_stock",
        "NVDA",
        "NVDA",
        "yahoo_chart",
        "yahoo_chart",
        "us_equities",
        "America/New_York",
        "traded",
        display_name="NVIDIA",
        volume_scope="provider_reported",
    ),
    PreflightTarget(
        "us_stock",
        "TSLA",
        "TSLA",
        "yahoo_chart",
        "yahoo_chart",
        "us_equities",
        "America/New_York",
        "traded",
        display_name="Tesla",
        volume_scope="provider_reported",
    ),
    PreflightTarget(
        "index",
        "SPX",
        "^GSPC",
        "yahoo_chart",
        "yahoo_chart",
        "us_equities",
        "America/New_York",
        "not_applicable",
        display_name="标普 500",
        volume_scope="index_not_applicable",
        volume_completeness="not_applicable",
    ),
    PreflightTarget(
        "crypto",
        "BTC",
        "BTC-USD",
        "yahoo_chart",
        "yahoo_chart",
        "crypto_24x7",
        "UTC",
        "traded",
        display_name="Bitcoin",
        volume_scope="provider_reported",
    ),
    PreflightTarget(
        "commodity",
        "GOLD",
        "GC=F",
        "yahoo_chart",
        "yahoo_chart",
        "us_futures",
        "America/Chicago",
        "traded",
        display_name="黄金期货",
        volume_scope="provider_reported",
    ),
)


def _request_for(
    target: PreflightTarget, timeframe: str, *, now: datetime
) -> tuple[str, dict[str, str]]:
    if target.source_kind == "yahoo_chart":
        return _yahoo_request(target, timeframe)
    if target.source_kind == "eastmoney":
        return _eastmoney_request(target, timeframe, now=now)
    raise ValueError(f"unknown preflight source kind: {target.source_kind}")


def _parse_observation(
    target: PreflightTarget,
    timeframe: str,
    observation: HttpObservation,
    *,
    now: datetime,
) -> ParsedSeries:
    if observation.error == "invalid_json" or observation.payload is None:
        raise PreflightParseError("invalid_json", "provider returned non-JSON data")
    if target.source_kind == "yahoo_chart":
        return parse_yahoo_chart_payload(
            observation.payload,
            provider_symbol=target.provider_symbol,
            timeframe=timeframe,
            calendar_id=target.calendar_id,
            timezone_name=target.timezone,
            now=now,
        )
    return parse_eastmoney_payload(
        observation.payload,
        provider_symbol=target.provider_symbol,
        timeframe=timeframe,
        timezone_name=target.timezone,
        now=now,
    )


def _failure_cell(
    target: PreflightTarget,
    timeframe: str,
    *,
    code: str,
    message: str,
    status: str = "unavailable",
    quality: QualityReport | None = None,
) -> ClassifiedCell:
    return ClassifiedCell(
        target=target,
        timeframe=timeframe,
        status=status,
        status_reason=code,
        quality=quality or QualityReport("unavailable"),
        error={
            "code": code,
            "message": _redact(message),
            "redacted_raw": _redact(message),
            "next_step": _next_step(code),
        },
    )


def _redact(value: str) -> str:
    text = str(value)
    for marker in ("token", "api_key", "apikey", "secret", "password"):
        lower = text.lower()
        index = lower.find(marker)
        if index >= 0:
            end = text.find(" ", index)
            if end < 0:
                end = len(text)
            text = f"{text[:index]}{marker}=<redacted>{text[end:]}"
    return text[:500]


def _next_step(code: str) -> str:
    return {
        "entitlement_unverified": "provide a current persistence/derived-use policy receipt",
        "persistence_not_allowed": "choose a source whose terms allow private persistence",
        "quality_invalid": "inspect upstream rows before any canonical promotion",
        "no_closed_bars": "retry after the next closed session or widen the request window",
        "rate_limited": "respect provider rate limits and retry with bounded backoff",
    }.get(code, "inspect the provider receipt and keep the cell blocked")


def _now_iso(value: datetime) -> str:
    return _stamp(value)


def _coverage_summary(cells: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, int]]:
    summary: dict[str, dict[str, int]] = {}
    for cell in cells:
        timeframe = str(cell["timeframe"])
        status = str(cell["status"])
        bucket = summary.setdefault(timeframe, {})
        bucket[status] = bucket.get(status, 0) + 1
    return {
        timeframe: dict(sorted(states.items())) for timeframe, states in sorted(summary.items())
    }


def run_preflight(
    targets: Sequence[PreflightTarget] = DEFAULT_TARGETS,
    *,
    now: datetime | None = None,
    timeout: float = 20.0,
    fetcher: JsonFetcher = fetch_json,
) -> dict[str, Any]:
    """Probe all target base timeframes and return a redacted receipt."""

    observed_at = now or datetime.now(timezone.utc)
    cells: list[dict[str, Any]] = []
    idempotency: list[dict[str, Any]] = []
    for target in targets:
        base_series: dict[str, ParsedSeries] = {}
        for timeframe in BASE_TIMEFRAMES:
            if timeframe not in target.requested_timeframes:
                continue
            endpoint, params = _request_for(target, timeframe, now=observed_at)
            observation = fetcher(endpoint, params=params, timeout=timeout)
            request = {"endpoint": endpoint, "params": dict(params), "timeframe": timeframe}
            response = {
                "http_status": observation.status_code,
                "latency_ms": observation.elapsed_ms,
                "response_sha256": observation.response_hash,
                "rate_limit_observed": observation.status_code == 429,
                "attempts": 1,
            }
            if observation.status_code in {401, 403}:
                cell = _failure_cell(
                    target,
                    timeframe,
                    code="entitlement_unverified",
                    message=f"provider returned HTTP {observation.status_code}",
                    status="blocked",
                )
            elif observation.status_code == 429:
                cell = _failure_cell(
                    target,
                    timeframe,
                    code="rate_limited",
                    message="provider rate limited the request",
                )
            elif observation.status_code != 200 or observation.error:
                cell = _failure_cell(
                    target,
                    timeframe,
                    code="request_failed",
                    message=observation.error
                    or f"provider returned HTTP {observation.status_code}",
                )
            else:
                try:
                    parsed = _parse_observation(target, timeframe, observation, now=observed_at)
                except PreflightParseError as error:
                    cell = _failure_cell(
                        target, timeframe, code=error.code, message=str(error), status="blocked"
                    )
                else:
                    base_series[timeframe] = parsed
                    cell = classify_status(
                        target,
                        timeframe,
                        parsed.bars,
                        quality=parsed.quality,
                        policy=target.policy,
                    )
                    response["provider_metadata"] = dict(parsed.metadata)
                    if parsed.bars:
                        idempotency.append(
                            {
                                "source_id": target.source_id,
                                "instrument_id": target.instrument_id,
                                "timeframe": timeframe,
                                **idempotency_check(
                                    source_id=target.source_id,
                                    instrument_id=target.instrument_id,
                                    timeframe=timeframe,
                                    bars=parsed.bars,
                                ),
                            }
                        )
            cells.append(
                cell.as_dict(request=request, response=response, observed_at=_now_iso(observed_at))
            )

        for output_timeframe, input_timeframe in (("4h", "15m"), ("1w", "1d")):
            if output_timeframe not in target.requested_timeframes:
                continue
            source = base_series.get(input_timeframe)
            if source is None or not source.bars:
                cell = _failure_cell(
                    target,
                    output_timeframe,
                    code="missing_input",
                    message=f"{output_timeframe} requires closed {input_timeframe} input",
                )
                cells.append(cell.as_dict(observed_at=_now_iso(observed_at)))
                continue
            try:
                derived = derive_series(
                    target,
                    input_timeframe=input_timeframe,
                    output_timeframe=output_timeframe,
                    bars=source.bars,
                    now=observed_at,
                )
            except (PreflightParseError, ValueError) as error:
                cell = _failure_cell(
                    target,
                    output_timeframe,
                    code="transform_failed",
                    message=str(error),
                    status="blocked",
                )
            else:
                cell = classify_status(
                    target,
                    output_timeframe,
                    derived.bars,
                    policy=target.policy,
                    quality=derived.quality,
                    is_derived=True,
                    transform=derived.transform,
                )
                if derived.bars:
                    idempotency.append(
                        {
                            "source_id": target.source_id,
                            "instrument_id": target.instrument_id,
                            "timeframe": output_timeframe,
                            **idempotency_check(
                                source_id=target.source_id,
                                instrument_id=target.instrument_id,
                                timeframe=output_timeframe,
                                bars=derived.bars,
                            ),
                        }
                    )
            cells.append(cell.as_dict(observed_at=_now_iso(observed_at)))

    counts: dict[str, int] = {}
    for cell in cells:
        counts[cell["status"]] = counts.get(cell["status"], 0) + 1
    return {
        "schema_version": PRE_FLIGHT_SCHEMA,
        "observed_at": _now_iso(observed_at),
        "target_count": len(targets),
        "targets": [
            {
                "instrument_id": target.instrument_id,
                "display_symbol": target.display_symbol,
                "display_name": target.display_name,
                "asset_class": target.asset_class,
                "source_id": target.source_id,
                "provider_symbol": target.provider_symbol,
                "source_kind": target.source_kind,
                "calendar_id": target.calendar_id,
                "timezone": target.timezone,
                "volume_semantics": target.volume_semantics,
                "volume_scope": target.volume_scope,
                "volume_completeness": target.volume_completeness,
                "policy": target.policy.as_dict(),
            }
            for target in targets
        ],
        "summary": {
            "cells": len(cells),
            "by_status": dict(sorted(counts.items())),
            "by_timeframe": _coverage_summary(cells),
        },
        "cells": cells,
        "idempotency": idempotency,
        "read_only": True,
        "database": {"mode": "sqlite_memory", "production_database_touched": False},
        "decision": _decision(cells),
        "decision_by_asset_class": _decision_by_asset_class(cells),
        "first_gate": {
            "name": "3+3 real end-to-end for 7 days",
            "status": "partial"
            if any(cell["status"] in {"ready", "partial"} for cell in cells)
            else "blocked",
            "requirement": "technical rows, explicit source rights, closed-bar quality, and idempotent writes",
        },
    }


def _decision(cells: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    statuses = {str(cell["status"]) for cell in cells}
    if "ready" in statuses and statuses.issubset({"ready"}):
        status = "ready"
    elif "ready" in statuses or "partial" in statuses:
        status = "partial"
    elif "blocked" in statuses:
        status = "blocked"
    else:
        status = "unavailable"
    return {
        "status": status,
        "canonical_promotion_allowed": status == "ready",
        "next_step": "promote only after policy evidence"
        if status != "ready"
        else "run bounded 3+3 pilot",
    }


def _decision_by_asset_class(cells: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[str]] = {}
    for cell in cells:
        grouped.setdefault(str(cell["asset_class"]), []).append(str(cell["status"]))
    decisions: dict[str, dict[str, Any]] = {}
    for asset_class, statuses in sorted(grouped.items()):
        if "ready" in statuses or "partial" in statuses:
            status = "partial"
            next_step = (
                "keep as pilot candidate; obtain persistence/derived-use evidence before promotion"
            )
        elif "blocked" in statuses:
            status = "blocked"
            next_step = "resolve entitlement or parser blocker before adapter implementation"
        else:
            status = "unavailable"
            next_step = "repeat the source smoke when the endpoint is reachable"
        decisions[asset_class] = {
            "status": status,
            "next_step": next_step,
            "canonical_promotion_allowed": status == "ready",
        }
    return decisions


def render_markdown(receipt: Mapping[str, Any]) -> str:
    """Render a compact human-readable decision matrix from a JSON receipt."""

    summary = receipt.get("summary", {})
    lines = [
        "# Provider Preflight Receipt",
        "",
        f"- Observed at: `{receipt.get('observed_at')}`",
        f"- Targets: `{receipt.get('target_count')}`",
        f"- Cells: `{summary.get('cells', 0)}`",
        f"- Decision: **{receipt.get('decision', {}).get('status', 'unknown')}**",
        f"- Read-only: `{receipt.get('read_only')}`",
        "",
        "## Decision matrix",
        "",
        "| Asset | Source | Timeframe | Status | Reason | Rows | Latest closed | Policy |",
        "|---|---|---:|---|---|---:|---|---|",
    ]
    for cell in receipt.get("cells", []):
        policy = cell.get("entitlement", {}).get("status", "unknown")
        lines.append(
            f"| {cell.get('display_symbol')} | {cell.get('source_id')} | {cell.get('timeframe')} | "
            f"{cell.get('status')} | {cell.get('status_reason')} | {cell.get('row_count', 0)} | "
            f"{cell.get('latest_closed_timestamp') or '—'} | {policy} |"
        )
    lines.extend(
        [
            "",
            "## Next path by asset class",
            "",
        ]
    )
    for asset_class, decision in sorted(receipt.get("decision_by_asset_class", {}).items()):
        lines.append(
            f"- `{asset_class}`: **{decision.get('status')}** — {decision.get('next_step')}"
        )
    lines.extend(
        [
            "",
            "## Safety",
            "",
            "- No resident database was opened or changed.",
            "- Response bodies are represented by hashes only; errors are redacted.",
            "- `partial`, `blocked`, and `unavailable` cells are not promoted to canonical data.",
            "",
        ]
    )
    return "\n".join(lines)


def _default_output_paths() -> tuple[Path, Path]:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    root = Path("docs/research")
    return root / f"provider-preflight-{stamp}.json", root / f"provider-preflight-{stamp}.md"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    default_json, default_md = _default_output_paths()
    parser.add_argument("--json-out", type=Path, default=default_json)
    parser.add_argument("--md-out", type=Path, default=default_md)
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args(argv)
    receipt = run_preflight(timeout=args.timeout)
    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.md_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    args.md_out.write_text(render_markdown(receipt), encoding="utf-8")
    print(
        json.dumps(
            {
                "json": str(args.json_out),
                "markdown": str(args.md_out),
                "decision": receipt["decision"],
            },
            ensure_ascii=False,
        )
    )
    return 0 if receipt["decision"]["status"] in {"ready", "partial"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
