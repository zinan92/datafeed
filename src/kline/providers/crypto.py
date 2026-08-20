"""Crypto provider — Binance public K-line API (no auth needed)."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
import math
from typing import Any

import httpx

from kline.models import Candle, Timeframe, TimeframeTransform
from kline.providers.base import ProviderError

logger = logging.getLogger(__name__)

BINANCE_KLINE_URL = "https://api.binance.com/api/v3/klines"

_TF_MAP = {
    Timeframe.MIN_1: "1m",
    Timeframe.MIN_5: "5m",
    Timeframe.MIN_15: "15m",
    Timeframe.MIN_30: "30m",
    Timeframe.HOUR_1: "1h",
    Timeframe.HOUR_4: "4h",
    Timeframe.DAY: "1d",
    Timeframe.WEEK: "1w",
}


def _normalize_symbol(ticker: str) -> str:
    """Normalize to Binance format: BTC → BTCUSDT, btcusdt → BTCUSDT."""

    symbol = ticker.upper().strip()
    if not symbol.endswith("USDT"):
        symbol = f"{symbol}USDT"
    return symbol


class CryptoProvider:
    """Fetch crypto K-line data via Binance public API."""

    def __init__(
        self,
        timeout: int = 30,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._timeout = timeout
        self._transport = transport
        self.last_raw_response: dict[str, Any] | None = None
        self.timeframe_transform: TimeframeTransform | None = None
        self.source_identity: dict[str, Any] = {}

    def supported_timeframes(self) -> list[Timeframe]:
        return list(_TF_MAP.keys())

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
        symbol = _normalize_symbol(ticker)
        requested_transform = _expected_transform(timeframe)
        self.timeframe_transform = requested_transform
        self.source_identity = {"provider_symbol": symbol}

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
                self.source_identity = {"provider_symbol": symbol}
                raise
            raw_audit = self.last_raw_response
            self.timeframe_transform = requested_transform
            self.source_identity = {"provider_symbol": symbol}
            candles = _aggregate_completed_weeks(daily, cutoff=_exclusive_end_cutoff(end))
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

        interval = _TF_MAP.get(timeframe)
        if not interval:
            raise ProviderError(
                f"Timeframe {timeframe.value} not supported for crypto",
                suggestions=[f"Supported: {[item.value for item in self.supported_timeframes()]}"]
            )

        request_limit = min(limit + 1, 1000) if timeframe == Timeframe.DAY else min(limit, 1000)
        params: dict[str, Any] = {
            "symbol": symbol,
            "interval": interval,
            "limit": request_limit,
        }
        self.last_raw_response = {
            "request_params": params,
            "response_body": None,
            "status_code": None,
            "error": None,
        }

        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            try:
                response = await client.get(BINANCE_KLINE_URL, params=params)
                response.raise_for_status()
                data = response.json()
            except httpx.HTTPStatusError as exc:
                self.last_raw_response["status_code"] = exc.response.status_code
                self.last_raw_response["error"] = str(exc)
                if exc.response.status_code == 400:
                    raise ProviderError(
                        f"Invalid symbol: {symbol}",
                        suggestions=[
                            "Use base symbol (BTC, ETH, SOL) or full pair (BTCUSDT)",
                            "Common symbols: BTC, ETH, SOL, BNB, DOGE, ADA",
                        ],
                    ) from exc
                raise ProviderError(f"Binance API error: {exc}") from exc
            except httpx.RequestError as exc:
                self.last_raw_response["error"] = str(exc)
                raise ProviderError(
                    f"Binance request failed: {exc}",
                    suggestions=[
                        "Check internet connection",
                        "Binance may be blocked in your region",
                    ],
                ) from exc

        self.last_raw_response["status_code"] = 200
        self.last_raw_response["response_body"] = data
        candles: list[Candle] = []
        for item in data:
            # Binance kline format: [open_time, open, high, low, close, volume, ...]
            open_time_ms = item[0]
            stamp = datetime.fromtimestamp(open_time_ms / 1000, tz=timezone.utc)
            timestamp = (
                stamp.strftime("%Y-%m-%d")
                if timeframe == Timeframe.DAY
                else stamp.strftime("%Y-%m-%dT%H:%M:%S")
            )
            open_value = float(item[1])
            high_value = float(item[2])
            low_value = float(item[3])
            close_value = float(item[4])
            volume = float(item[5])
            if not all(
                math.isfinite(value)
                for value in (open_value, high_value, low_value, close_value, volume)
            ):
                self.last_raw_response["error"] = "non_finite_ohlc"
                raise ProviderError(f"Binance OHLC contains a non-finite value for {symbol}")
            if high_value < max(open_value, close_value) or low_value > min(
                open_value, close_value
            ):
                self.last_raw_response["error"] = "ohlc_invariant"
                raise ProviderError(f"Binance OHLC invariant failed for {symbol}")
            if volume < 0:
                self.last_raw_response["error"] = "negative_volume"
                raise ProviderError(f"Binance volume is negative for {symbol}")
            candles.append(
                Candle(
                    timestamp=timestamp,
                    open=open_value,
                    high=high_value,
                    low=low_value,
                    close=close_value,
                    volume=volume,
                    amount=float(item[7]),
                )
            )

        if start:
            candles = [item for item in candles if item.timestamp[:10] >= start[:10]]
        if end:
            candles = [item for item in candles if item.timestamp[:10] < end[:10]]
        if timeframe == Timeframe.DAY:
            cutoff = _closed_daily_cutoff(end)
            candles = [
                item
                for item in candles
                if date.fromisoformat(item.timestamp[:10]) <= cutoff
            ]
        if not candles:
            self.last_raw_response["error"] = "empty_closed_data"
            raise ProviderError(f"No closed data returned for {symbol} ({timeframe.value})")
        if limit and len(candles) > limit:
            candles = candles[-limit:]

        logger.info(f"Fetched {len(candles)} candles for {symbol} ({timeframe.value})")
        self.timeframe_transform = TimeframeTransform(
            raw_timeframe=timeframe,
            timeframe_origin="native",
            aggregation={"kind": "none", "rule": "native_passthrough"},
        )
        self.source_identity = {"provider_symbol": symbol}
        return candles


def _exclusive_end_cutoff(end: str | None) -> date:
    if end:
        return date.fromisoformat(end[:10]) - timedelta(days=1)
    return datetime.now(timezone.utc).date()


def _expected_transform(timeframe: Timeframe) -> TimeframeTransform:
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
    return TimeframeTransform(
        raw_timeframe=timeframe,
        timeframe_origin="native",
        aggregation={"kind": "none", "rule": "native_passthrough"},
    )


def _closed_daily_cutoff(end: str | None) -> date:
    utc_today = datetime.now(timezone.utc).date()
    return min(_exclusive_end_cutoff(end), utc_today - timedelta(days=1))


def _aggregate_completed_weeks(candles: list[Candle], *, cutoff: date) -> list[Candle]:
    groups: dict[tuple[int, int], list[Candle]] = {}
    for candle in candles:
        trading_date = date.fromisoformat(candle.timestamp[:10])
        iso = trading_date.isocalendar()
        week_end = date.fromisocalendar(iso.year, iso.week, 7)
        if week_end > cutoff:
            continue
        groups.setdefault((int(iso.year), int(iso.week)), []).append(candle)

    output: list[Candle] = []
    for (iso_year, iso_week), rows in groups.items():
        rows.sort(key=lambda item: item.timestamp)
        week_end = date.fromisocalendar(iso_year, iso_week, 7)
        if week_end == cutoff and rows[-1].timestamp[:10] != week_end.isoformat():
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
        "response_body": {"raw": base.get("response_body"), "public_row_count": public_row_count},
        "status_code": base.get("status_code"),
        "error": error,
        "provider_symbol": base.get("provider_symbol") or request_params.get("symbol"),
    }
