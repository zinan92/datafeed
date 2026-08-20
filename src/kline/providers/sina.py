"""Sina Finance public daily K-line provider for the Phase 1 A-share indices."""

from __future__ import annotations

from datetime import date
from hashlib import sha256
import math
from typing import Any, Callable, Mapping

import httpx

from kline.models import Candle, Timeframe, TimeframeTransform
from kline.providers.base import ProviderError


_SINA_KLINE_URL = (
    "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "CN_MarketData.getKLineData"
)
_SINA_INDEX_SYMBOLS = frozenset({"sh000001", "sh000688", "sh000015"})
_SINA_INDEX_ALIASES = {
    "000001.sh": "sh000001",
    "000688.sh": "sh000688",
    "000015.sh": "sh000015",
}


class SinaIndexProvider:
    """Fetch the same three index symbols through Sina as an explicit backup."""

    def __init__(
        self,
        *,
        timeout: float = 30,
        transport: httpx.AsyncBaseTransport | None = None,
        today: Callable[[], date] | None = None,
    ) -> None:
        self._timeout = timeout
        self._transport = transport
        self._today = today or date.today
        self.last_raw_response: dict[str, Any] | None = None
        self.timeframe_transform: TimeframeTransform | None = None
        self.source_identity: dict[str, Any] = {}

    def supported_timeframes(self) -> list[Timeframe]:
        return [Timeframe.DAY, Timeframe.WEEK]

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
        symbol = _normalize_sina_index_symbol(ticker)
        self.source_identity = {
            "source_id": "sina_index",
            "provider_symbol": symbol,
            "endpoint": _SINA_KLINE_URL,
            "scale": 240,
            "price_adjustment": "none",
        }
        if symbol not in _SINA_INDEX_SYMBOLS:
            raise ProviderError(
                f"Unsupported Sina index symbol: {ticker}; explicit market-prefixed symbol required",
                suggestions=[
                    "Use explicit market-prefixed symbols: sh000001, sh000688, sh000015",
                ],
            )
        if timeframe not in self.supported_timeframes():
            raise ProviderError(
                f"Sina index timeframe {timeframe.value} is not supported",
                suggestions=[f"Supported: {[item.value for item in self.supported_timeframes()] }"],
            )

        today = self._today()
        requested_start = _parse_optional_date(start, "start")
        requested_end = _parse_optional_date(end, "end")
        if requested_start and requested_end and requested_start >= requested_end:
            raise ProviderError("Sina index date range is empty")
        completion_boundary = min(requested_end or today, today)
        if requested_start and requested_start >= completion_boundary:
            raise ProviderError("Sina index date range contains no closed sessions")

        datalen = _requested_day_count(
            timeframe,
            start=requested_start,
            end=requested_end or today,
            limit=limit,
        )
        self.timeframe_transform = _sina_transform(timeframe, symbol)
        self.last_raw_response = {
            "request_params": {
                "endpoint": _SINA_KLINE_URL,
                "provider_symbol": symbol,
                "scale": 240,
                "ma": "no",
                "datalen": datalen,
                "requested_start": start,
                "requested_end": end,
                "requested_timeframe": timeframe.value,
            },
            "response_body": None,
            "status_code": None,
            "error": None,
        }
        params = {
            "symbol": symbol,
            "scale": "240",
            "ma": "no",
            "datalen": str(datalen),
        }
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                transport=self._transport,
                follow_redirects=True,
            ) as client:
                response = await client.get(_SINA_KLINE_URL, params=params)
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            self.last_raw_response["status_code"] = exc.response.status_code
            self.last_raw_response["error"] = str(exc)
            raise ProviderError(f"Sina K-line request failed for {symbol}: {exc}") from exc
        except httpx.RequestError as exc:
            self.last_raw_response["error"] = str(exc)
            raise ProviderError(f"Sina K-line request failed for {symbol}: {exc}") from exc

        self.last_raw_response["status_code"] = response.status_code
        try:
            payload = response.json()
        except ValueError as exc:
            body = response.text
            self.last_raw_response["response_body"] = {
                "body_sha256": sha256(body.encode()).hexdigest(),
            }
            self.last_raw_response["error"] = "non_json_response"
            raise ProviderError(f"Sina returned non-JSON K-line data for {symbol}") from exc
        self.last_raw_response["response_body"] = {
            "row_count": len(payload) if isinstance(payload, list) else 0,
            "payload": payload,
        }
        if not isinstance(payload, list) or not payload:
            self.last_raw_response["error"] = "empty_data"
            raise ProviderError(f"Sina returned no daily K-line rows for {symbol}")

        try:
            candles = _parse_sina_rows(payload, ticker=symbol)
            candles = [
                candle
                for candle in candles
                if (requested_start is None or date.fromisoformat(candle.timestamp) >= requested_start)
                and date.fromisoformat(candle.timestamp) < completion_boundary
            ]
            if timeframe == Timeframe.WEEK:
                candles = _aggregate_sina_completed_weeks(
                    candles,
                    completion_boundary=completion_boundary,
                    requested_start=requested_start,
                )
            if not candles:
                raise ProviderError(
                    f"Sina returned no closed candles for {ticker} ({timeframe.value})",
                    suggestions=["Check the requested date range and whether a completed market week is available"],
                )
        except ProviderError as error:
            self.last_raw_response["error"] = str(error)
            raise
        if limit and len(candles) > limit:
            candles = candles[-limit:]
        self.last_raw_response["request_params"]["public_row_count"] = len(candles)
        return candles


def _normalize_sina_index_symbol(ticker: str) -> str:
    normalized = ticker.strip().lower()
    return _SINA_INDEX_ALIASES.get(normalized, normalized)


def _parse_optional_date(value: str | None, field: str) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError as exc:
        raise ProviderError(f"Sina index {field} date is invalid: {value}") from exc


def _requested_day_count(
    timeframe: Timeframe,
    *,
    start: date | None,
    end: date,
    limit: int,
) -> int:
    multiplier = 7 if timeframe == Timeframe.WEEK else 1
    requested_span = (end - start).days + 30 if start else limit * multiplier + 30
    return max(365, min(5000, requested_span))


def _sina_transform(timeframe: Timeframe, symbol: str) -> TimeframeTransform:
    if timeframe == Timeframe.WEEK:
        return TimeframeTransform(
            raw_timeframe=Timeframe.DAY,
            timeframe_origin="aggregated",
            aggregation={
                "kind": "ohlc_resample",
                "rule": "completed_iso_week",
                "input_timeframe": Timeframe.DAY.value,
                "bucket_timezone": "Asia/Shanghai",
                "input_source": {"source_id": "sina_index", "provider_symbol": symbol},
            },
        )
    return TimeframeTransform(
        raw_timeframe=Timeframe.DAY,
        timeframe_origin="native",
        aggregation={"kind": "none", "rule": "native_passthrough"},
    )


def _parse_sina_rows(rows: list[Any], *, ticker: str) -> list[Candle]:
    candles: list[Candle] = []
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise ProviderError(f"Sina K-line row {index} is malformed for {ticker}")
        try:
            timestamp = date.fromisoformat(str(row["day"])[:10]).isoformat()
            open_value = float(row["open"])
            high_value = float(row["high"])
            low_value = float(row["low"])
            close_value = float(row["close"])
            volume = float(row.get("volume", 0))
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderError(f"Sina K-line row {index} has invalid values for {ticker}") from exc
        if not all(math.isfinite(value) for value in (open_value, high_value, low_value, close_value, volume)):
            raise ProviderError(f"Sina K-line row {index} has non-finite values for {ticker}")
        if high_value < max(open_value, close_value) or low_value > min(open_value, close_value):
            raise ProviderError(f"Sina OHLC invariant failed for {ticker} at {timestamp}")
        if volume < 0:
            raise ProviderError(f"Sina volume is negative for {ticker}")
        candles.append(
            Candle(
                timestamp=timestamp,
                open=open_value,
                high=high_value,
                low=low_value,
                close=close_value,
                volume=volume,
            )
        )
    for previous, current in zip(candles, candles[1:]):
        if current.timestamp == previous.timestamp:
            raise ProviderError(f"Sina returned duplicate dates for {ticker}")
        if current.timestamp < previous.timestamp:
            raise ProviderError(f"Sina returned out-of-order dates for {ticker}")
    return candles


def _aggregate_sina_completed_weeks(
    candles: list[Candle],
    *,
    completion_boundary: date,
    requested_start: date | None,
) -> list[Candle]:
    groups: dict[tuple[int, int], list[Candle]] = {}
    for candle in candles:
        trading_date = date.fromisoformat(candle.timestamp)
        iso = trading_date.isocalendar()
        week_start = date.fromisocalendar(iso.year, iso.week, 1)
        week_end = date.fromisocalendar(iso.year, iso.week, 5)
        if week_end >= completion_boundary:
            continue
        if requested_start and week_start < requested_start:
            continue
        groups.setdefault((int(iso.year), int(iso.week)), []).append(candle)
    output: list[Candle] = []
    for rows in groups.values():
        rows.sort(key=lambda item: item.timestamp)
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
