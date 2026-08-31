"""US stock provider — Yahoo Finance via yfinance."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
import math
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

from kline.models import Candle, Timeframe, TimeframeTransform
from kline.providers.base import ProviderError

logger = logging.getLogger(__name__)

# yfinance interval mapping
_TF_MAP = {
    Timeframe.MIN_1: "1m",
    Timeframe.MIN_5: "5m",
    Timeframe.MIN_15: "15m",
    Timeframe.MIN_30: "30m",
    Timeframe.HOUR_1: "1h",
    Timeframe.DAY: "1d",
    Timeframe.WEEK: "1wk",
}

# yfinance period limits per interval.  Yahoo's ``max`` history can contain
# malformed legacy OHLC rows for otherwise healthy instruments.  The default
# window is intentionally bounded; callers that need an exact historical
# range must provide both ``start`` and ``end``.
_DEFAULT_PERIOD = {
    Timeframe.MIN_1: "7d",
    Timeframe.MIN_5: "60d",
    Timeframe.MIN_15: "60d",
    Timeframe.MIN_30: "60d",
    Timeframe.HOUR_1: "730d",
    Timeframe.DAY: "5y",
    Timeframe.WEEK: "5y",
}


def _index_date(index: Any) -> date:
    """Return a calendar date from a pandas timestamp-like index value."""

    value = getattr(index, "date", None)
    if callable(value):
        return value()
    return date.fromisoformat(str(index)[:10])


def _index_text(index: Any, timeframe: Timeframe) -> str:
    if timeframe == Timeframe.DAY:
        return _index_date(index).isoformat()
    if hasattr(index, "isoformat"):
        return index.isoformat()
    return str(index)


def _invalid_indices(frame: Any) -> list[Any]:
    """Return rows that cannot be represented as a finite OHLCV candle."""

    if frame is None or getattr(frame, "empty", True):
        return []
    invalid: list[Any] = []
    for idx, row in frame.iterrows():
        try:
            values = [
                float(row[column])
                for column in ("Open", "High", "Low", "Close")
            ]
            volume = float(row.get("Volume", 0))
            if not all(math.isfinite(value) for value in (*values, volume)):
                invalid.append(idx)
                continue
            if values[1] < max(values[0], values[3]) or values[2] > min(values[0], values[3]):
                invalid.append(idx)
        except (TypeError, ValueError, KeyError):
            invalid.append(idx)
    return invalid


class USStockProvider:
    """Fetch US stock K-line data via Yahoo Finance."""

    def __init__(self, *, four_hour_anchor: tuple[int, int] = (0, 0)) -> None:
        if not 0 <= four_hour_anchor[0] < 24 or not 0 <= four_hour_anchor[1] < 60:
            raise ValueError("four_hour_anchor must be (hour 0-23, minute 0-59)")
        self.last_raw_response: dict[str, Any] | None = None
        self.timeframe_transform: TimeframeTransform | None = None
        self.source_identity: dict[str, Any] = {}
        self._four_hour_anchor = four_hour_anchor

    def supported_timeframes(self) -> list[Timeframe]:
        return [*list(_TF_MAP.keys()), Timeframe.HOUR_4]

    async def fetch(
        self,
        ticker: str,
        timeframe: Timeframe,
        *,
        start: str | None = None,
        end: str | None = None,
        limit: int = 500,
    ) -> list[Candle]:
        self.last_raw_response = None
        requested_transform = _expected_transform(timeframe, self._four_hour_anchor)
        self.timeframe_transform = requested_transform
        self.source_identity = {"provider_symbol": ticker.upper()}
        if timeframe == Timeframe.WEEK:
            try:
                daily = await self.fetch(
                    ticker,
                    Timeframe.DAY,
                    start=start,
                    end=end,
                    limit=max(limit * 7, 500),
                )
            except ProviderError:
                self.timeframe_transform = requested_transform
                self.source_identity = {"provider_symbol": ticker.upper()}
                raise
            raw_audit = self.last_raw_response
            raw_identity = dict(self.source_identity)
            self.timeframe_transform = requested_transform
            self.source_identity = raw_identity
            try:
                candles = _aggregate_completed_weeks(
                    daily,
                    cutoff=_exclusive_end_cutoff(end, timezone_name=_source_timezone(ticker)),
                )
            except ProviderError as error:
                self.last_raw_response = _derived_audit(
                    raw_audit,
                    requested_timeframe=timeframe,
                    public_row_count=0,
                    error=str(error),
                )
                raise
            if not candles:
                self.last_raw_response = _derived_audit(
                    raw_audit,
                    requested_timeframe=timeframe,
                    public_row_count=0,
                    error=f"No completed weekly data returned for {ticker}",
                )
                raise ProviderError(f"No completed weekly data returned for {ticker}")
            if limit and len(candles) > limit:
                candles = candles[-limit:]
            self.last_raw_response = _derived_audit(
                raw_audit,
                requested_timeframe=timeframe,
                public_row_count=len(candles),
            )
            return candles

        if timeframe == Timeframe.HOUR_4:
            try:
                hourly = await self.fetch(
                    ticker,
                    Timeframe.HOUR_1,
                    start=start,
                    end=end,
                    limit=max(limit * 4, 200),
                )
            except ProviderError:
                self.timeframe_transform = requested_transform
                self.source_identity = {"provider_symbol": ticker.upper()}
                raise
            raw_audit = self.last_raw_response
            raw_identity = dict(self.source_identity)
            self.timeframe_transform = requested_transform
            self.source_identity = raw_identity
            candles, dropped = _aggregate_fixed_4h(
                hourly,
                anchor_hour=self._four_hour_anchor[0],
                anchor_minute=self._four_hour_anchor[1],
            )
            if not candles:
                self.last_raw_response = _derived_audit(
                    raw_audit,
                    requested_timeframe=timeframe,
                    public_row_count=0,
                    error=f"No complete 4H buckets returned for {ticker}",
                )
                raise ProviderError(f"No complete 4H buckets returned for {ticker}")
            if limit and len(candles) > limit:
                candles = candles[-limit:]
            self.timeframe_transform.aggregation["dropped_incomplete_buckets"] = dropped
            self.last_raw_response = _derived_audit(
                raw_audit,
                requested_timeframe=timeframe,
                public_row_count=len(candles),
            )
            return candles

        interval = _TF_MAP.get(timeframe)
        if not interval:
            raise ProviderError(
                f"Timeframe {timeframe.value} not supported for US stocks",
                suggestions=[f"Supported: {[t.value for t in self.supported_timeframes()]}"],
            )

        request_params: dict[str, Any] = {
            "ticker": ticker.upper(),
            "interval": interval,
            "start": start,
            "end": end,
            "limit": limit,
            # Yahoo occasionally emits a live row with valid volume but NaN
            # OHLC. We first ask for the raw response, then invoke yfinance's
            # repair path only when the raw response cannot pass validation.
            "repair_policy": "yfinance_repair_on_invalid_ohlc",
        }
        self.last_raw_response = {
            "request_params": request_params,
            "response_body": None,
            "status_code": None,
            "error": None,
        }
        try:
            stock = yf.Ticker(ticker.upper())
            kwargs: dict = {"interval": interval, "repair": False, "keepna": False}
            if start and end:
                kwargs["start"] = start
                # Give Yahoo a little context beyond the requested cutoff.
                # Its repair path needs the next session to reconstruct a
                # live row; the public response is still clipped to `end`
                # below, so no future candle can leak into the contract.
                kwargs["end"] = _repair_context_end(end)
                request_params["repair_context_end"] = kwargs["end"]
            else:
                period = _DEFAULT_PERIOD.get(timeframe, "2y")
                kwargs["period"] = period
                request_params["period"] = period
            df = stock.history(**kwargs)
        except Exception as e:
            self.last_raw_response["error"] = str(e)
            raise ProviderError(
                f"Yahoo Finance request failed for {ticker}: {e}",
                suggestions=[f"Verify {ticker} is a valid US stock ticker (e.g., AAPL, MSFT)"],
            ) from e

        if df is None or df.empty:
            self.last_raw_response["error"] = "empty_data"
            raise ProviderError(
                f"No data returned for {ticker}",
                suggestions=[
                    f"Verify {ticker} is a valid ticker symbol",
                    "Try common symbols: AAPL, MSFT, GOOGL, AMZN",
                ],
            )

        cutoff = (
            _closed_daily_cutoff(end, timezone_name=_source_timezone(ticker))
            if timeframe == Timeframe.DAY
            else None
        )
        raw_bad_indices = _invalid_indices(df)
        if timeframe == Timeframe.DAY and cutoff is not None:
            raw_dates = [
                _index_date(idx)
                for idx in df.index
                if _index_date(idx) <= cutoff
            ]
            # A missing latest session can be represented by a dropped row
            # rather than a NaN row (notably for VIX). Only retry when the
            # requested cutoff is a weekday and the raw data stops earlier.
            if raw_dates and max(raw_dates) < cutoff and cutoff.weekday() < 5:
                raw_bad_indices.append(None)

        repaired_timestamps: list[str] = []
        repair_attempted = False
        if raw_bad_indices:
            repair_attempted = True
            repair_kwargs = dict(kwargs)
            repair_kwargs["repair"] = True
            try:
                repaired_df = yf.Ticker(ticker.upper()).history(**repair_kwargs)
            except Exception as e:
                self.last_raw_response["error"] = str(e)
                raise ProviderError(
                    f"Yahoo Finance repair failed for {ticker}: {e}",
                    suggestions=["Retry the same source after the upstream response stabilizes"],
                ) from e
            if repaired_df is None or repaired_df.empty:
                self.last_raw_response["error"] = "repair_empty_data"
                raise ProviderError(f"Yahoo Finance repair returned no data for {ticker}")

            allowed_indices = set(df.index)
            target_index = None
            if timeframe == Timeframe.DAY and cutoff is not None:
                candidates = [
                    idx for idx in repaired_df.index if _index_date(idx) <= cutoff
                ]
                if candidates:
                    target_index = max(candidates, key=_index_date)
                    allowed_indices.add(target_index)

            replace_indices = {idx for idx in raw_bad_indices if idx is not None}
            repaired_indices = [
                idx
                for idx in repaired_df.index
                if idx in allowed_indices
                and (idx in replace_indices or idx == target_index)
            ]
            for idx in repaired_indices:
                if idx in df.index:
                    for column in ("Open", "High", "Low", "Close", "Volume"):
                        if column in repaired_df.columns:
                            df.loc[idx, column] = repaired_df.loc[idx, column]
                else:
                    df = pd.concat([df, repaired_df.loc[[idx]]], sort=False)
                repaired_timestamps.append(_index_text(idx, timeframe))
            df = df.sort_index()
            request_params["repair_attempted"] = True
            request_params["repair_selected_rows"] = repaired_timestamps
        else:
            request_params["repair_attempted"] = False

        candles = []
        for idx, row in df.iterrows():
            if timeframe == Timeframe.DAY:
                ts_str = idx.strftime("%Y-%m-%d")
            elif getattr(idx, "tzinfo", None) is not None:
                ts_str = idx.isoformat()
            else:
                ts_str = idx.strftime("%Y-%m-%dT%H:%M:%S+00:00")
            open_value = float(row["Open"])
            high_value = float(row["High"])
            low_value = float(row["Low"])
            close_value = float(row["Close"])
            volume = float(row.get("Volume", 0))
            if not all(
                math.isfinite(value)
                for value in (open_value, high_value, low_value, close_value, volume)
            ):
                raise ProviderError(
                    f"Yahoo OHLC contains a non-finite value for {ticker} at {ts_str}"
                )
            if high_value < max(open_value, close_value) or low_value > min(
                open_value, close_value
            ):
                raise ProviderError(f"Yahoo OHLC invariant failed for {ticker} at {ts_str}")
            if volume < 0:
                raise ProviderError(f"Yahoo volume is negative for {ticker} at {ts_str}")
            candles.append(
                Candle(
                    timestamp=ts_str,
                    open=open_value,
                    high=high_value,
                    low=low_value,
                    close=close_value,
                    volume=volume,
                )
            )

        if timeframe == Timeframe.DAY:
            assert cutoff is not None
            candles = [
                candle
                for candle in candles
                if date.fromisoformat(candle.timestamp[:10]) <= cutoff
            ]
            returned_timestamps = {candle.timestamp[:10] for candle in candles}
            repaired_timestamps = [
                timestamp
                for timestamp in repaired_timestamps
                if timestamp[:10] in returned_timestamps
            ]
            if not candles:
                raise ProviderError(f"No closed daily data returned for {ticker}")

        if limit and len(candles) > limit:
            candles = candles[-limit:]

        self.timeframe_transform = TimeframeTransform(
            raw_timeframe=timeframe,
            timeframe_origin="native",
            aggregation={"kind": "none", "rule": "native_passthrough"},
        )
        self.source_identity = {
            "provider_symbol": ticker.upper(),
            "repair_policy": "yfinance_repair_on_invalid_ohlc",
            "repair_attempted": repair_attempted,
            "repaired_row_count": len(repaired_timestamps),
            "repaired_timestamps": repaired_timestamps,
        }
        self.last_raw_response["response_body"] = {
            "row_count": len(candles),
            "rows": [item.model_dump() for item in candles],
            "repaired_timestamps": repaired_timestamps,
        }
        logger.info(f"Fetched {len(candles)} candles for {ticker} ({timeframe.value})")
        return candles


def _source_timezone(ticker: str) -> str:
    return {
        "^VIX": "America/Chicago",
        "^N225": "Asia/Tokyo",
        "^KS11": "Asia/Seoul",
    }.get(ticker.upper(), "America/New_York")


def _exclusive_end_cutoff(end: str | None, *, timezone_name: str) -> date:
    if end:
        return date.fromisoformat(end[:10]) - timedelta(days=1)
    return datetime.now(ZoneInfo(timezone_name)).date()


def _expected_transform(timeframe: Timeframe, anchor: tuple[int, int]) -> TimeframeTransform:
    if timeframe == Timeframe.WEEK:
        return TimeframeTransform(
            raw_timeframe=Timeframe.DAY,
            timeframe_origin="aggregated",
            aggregation={
                "kind": "ohlc_resample",
                "rule": "completed_iso_week",
                "input_timeframe": Timeframe.DAY.value,
                "bucket_timezone": "UTC",
            },
        )
    if timeframe == Timeframe.HOUR_4:
        return TimeframeTransform(
            raw_timeframe=Timeframe.HOUR_1,
            timeframe_origin="aggregated",
            aggregation={
                "kind": "ohlc_resample",
                "rule": "fixed_4h",
                "input_timeframe": Timeframe.HOUR_1.value,
                "bucket_timezone": "UTC",
                "anchor_hour": anchor[0],
                "anchor_minute": anchor[1],
            },
        )
    return TimeframeTransform(
        raw_timeframe=timeframe,
        timeframe_origin="native",
        aggregation={"kind": "none", "rule": "native_passthrough"},
    )


def _closed_daily_cutoff(end: str | None, *, timezone_name: str) -> date:
    """Use only bars strictly before the current calendar day."""

    yesterday = datetime.now(ZoneInfo(timezone_name)).date() - timedelta(days=1)
    requested = _exclusive_end_cutoff(end, timezone_name=timezone_name)
    return min(requested, yesterday)


def _repair_context_end(end: str) -> str:
    """Extend an upstream Yahoo request without extending the public cutoff."""

    return (date.fromisoformat(end[:10]) + timedelta(days=7)).isoformat()


def _derived_audit(
    raw_audit: dict[str, Any] | None,
    *,
    requested_timeframe: Timeframe,
    public_row_count: int,
    error: str | None = None,
) -> dict[str, Any]:
    base = dict(raw_audit) if isinstance(raw_audit, dict) else {}
    request_params = dict(base.get("request_params") or {})
    request_params["requested_timeframe"] = requested_timeframe.value
    return {
        "request_params": request_params,
        "response_body": {
            "raw": base.get("response_body"),
            "public_row_count": public_row_count,
        },
        "status_code": base.get("status_code"),
        "error": error,
        "provider_symbol": base.get("provider_symbol") or request_params.get("ticker"),
    }


def _aggregate_completed_weeks(candles: list[Candle], *, cutoff: date) -> list[Candle]:
    groups: dict[tuple[int, int], list[Candle]] = {}
    for candle in candles:
        trading_date = date.fromisoformat(candle.timestamp[:10])
        iso = trading_date.isocalendar()
        week_end = date.fromisocalendar(iso.year, iso.week, 5)
        if week_end > cutoff:
            continue
        groups.setdefault((int(iso.year), int(iso.week)), []).append(candle)
    output: list[Candle] = []
    for (iso_year, iso_week), rows in groups.items():
        rows.sort(key=lambda item: item.timestamp)
        week_end = date.fromisocalendar(iso_year, iso_week, 5)
        if (
            week_end == cutoff
            and cutoff >= date.today()
            and rows[-1].timestamp[:10] != week_end.isoformat()
        ):
            continue
        output.append(
            Candle(
                timestamp=rows[-1].timestamp,
                open=rows[0].open,
                high=max(row.high for row in rows),
                low=min(row.low for row in rows),
                close=rows[-1].close,
                volume=sum(row.volume for row in rows),
            )
        )
    return sorted(output, key=lambda item: item.timestamp)


def _aggregate_fixed_4h(
    candles: list[Candle], *, anchor_hour: int, anchor_minute: int
) -> tuple[list[Candle], int]:
    buckets: dict[datetime, list[Candle]] = {}

    for candle in candles:
        stamp = datetime.fromisoformat(candle.timestamp.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        stamp = stamp.astimezone(timezone.utc)
        anchor = stamp.replace(
            hour=anchor_hour,
            minute=anchor_minute,
            second=0,
            microsecond=0,
        )
        if stamp < anchor:
            anchor -= timedelta(days=1)
        elapsed_hours = int((stamp - anchor).total_seconds() // 3600)
        bucket = anchor + timedelta(hours=(elapsed_hours // 4) * 4)
        buckets.setdefault(bucket, []).append(candle)

    output: list[Candle] = []
    dropped = 0
    now = datetime.now(timezone.utc)
    for bucket, rows in sorted(buckets.items()):
        rows.sort(key=lambda item: item.timestamp)
        stamps = [
            datetime.fromisoformat(item.timestamp.replace("Z", "+00:00")).astimezone(timezone.utc)
            for item in rows
        ]
        expected_stamps = [bucket + timedelta(hours=offset) for offset in range(4)]
        complete = (
            bucket + timedelta(hours=4) <= now
            and len(rows) == 4
            and stamps == expected_stamps
        )
        if not complete:
            dropped += 1
            continue
        output.append(
            Candle(
                timestamp=bucket.isoformat().replace("+00:00", "Z"),
                open=rows[0].open,
                high=max(row.high for row in rows),
                low=min(row.low for row in rows),
                close=rows[-1].close,
                volume=sum(row.volume for row in rows),
            )
        )
    return output, dropped
