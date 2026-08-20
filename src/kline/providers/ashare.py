"""A-share provider — TuShare Pro for daily, AKShare as fallback."""

from __future__ import annotations

import logging
from datetime import date, timedelta
from hashlib import sha256
import math
from typing import Any, Callable, Mapping

import httpx
import tushare as ts

from kline.models import Candle, Timeframe, TimeframeTransform
from kline.providers.base import ProviderError

logger = logging.getLogger(__name__)

# TuShare timeframe mapping
_TF_MAP = {
    Timeframe.DAY: "D",
    Timeframe.WEEK: "W",
}
_INDEX_CODES = {"000001.SH", "000688.SH", "000015.SH"}

_TENCENT_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
_TENCENT_INDEX_SYMBOLS = frozenset({"sh000001", "sh000688", "sh000015"})
_TENCENT_INDEX_ALIASES = {
    "000001.sh": "sh000001",
    "000688.sh": "sh000688",
    "000015.sh": "sh000015",
}


def _to_tushare_code(ticker: str) -> str:
    """Convert 6-digit code to TuShare format: 000001 → 000001.SZ"""
    if "." in ticker:
        return ticker.upper().strip()
    if ticker.startswith("6"):
        return f"{ticker}.SH"
    if ticker.startswith(("4", "8")):
        return f"{ticker}.BJ"
    return f"{ticker}.SZ"


class AShareProvider:
    """Fetch A-share K-line data via TuShare Pro."""

    def __init__(self, token: str) -> None:
        if not token:
            raise ProviderError(
                "TuShare token is required for A-share data",
                suggestions=["Set KLINE_TUSHARE_TOKEN in .env", "Get a token at tushare.pro"],
            )
        ts.set_token(token)
        self._pro = ts.pro_api()

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
        if timeframe not in self.supported_timeframes():
            raise ProviderError(
                f"Timeframe {timeframe.value} not supported for A-shares",
                suggestions=[f"Supported: {[t.value for t in self.supported_timeframes()]}"],
            )

        ts_code = _to_tushare_code(ticker)
        start_date = start.replace("-", "") if start else None
        end_date = end.replace("-", "") if end else None

        # Default to last 2 years if no range specified
        if not start_date:
            start_date = (date.today() - timedelta(days=730)).strftime("%Y%m%d")
        if not end_date:
            end_date = date.today().strftime("%Y%m%d")

        try:
            if ts_code in _INDEX_CODES:
                df = self._pro.index_daily(
                    ts_code=ts_code,
                    start_date=start_date,
                    end_date=end_date,
                )
            else:
                df = self._pro.daily(
                    ts_code=ts_code,
                    start_date=start_date,
                    end_date=end_date,
                )
        except Exception as e:
            raise ProviderError(
                f"TuShare request failed for {ticker}: {e}",
                suggestions=["Check your TuShare token", "Verify ticker is a valid A-share code"],
            ) from e

        if df is None or df.empty:
            raise ProviderError(
                f"No data returned for {ticker}",
                suggestions=[
                    f"Verify {ticker} is a valid A-share ticker",
                    "Check if market is open",
                ],
            )

        candles = []
        for _, row in df.iterrows():
            raw_date = str(row["trade_date"])
            iso_date = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}"
            candles.append(
                Candle(
                    timestamp=iso_date,
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row.get("vol", 0)),
                    amount=float(row.get("amount", 0)),
                )
            )

        # TuShare returns newest-first, we want oldest-first
        candles.sort(key=lambda c: c.timestamp)

        if timeframe == Timeframe.WEEK:
            candles = _aggregate_weekly(candles)

        if limit and len(candles) > limit:
            candles = candles[-limit:]

        logger.info(f"Fetched {len(candles)} candles for {ticker} ({timeframe.value})")
        return candles


def _aggregate_weekly(candles: list[Candle]) -> list[Candle]:
    """Aggregate sorted daily candles into completed Monday-Friday weeks."""
    groups: dict[tuple[int, int], list[Candle]] = {}
    for candle in candles:
        day = date.fromisoformat(candle.timestamp[:10])
        key = day.isocalendar()[:2]
        groups.setdefault((int(key[0]), int(key[1])), []).append(candle)
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
                amount=sum(row.amount or 0 for row in rows),
            )
        )
    return output


class TencentIndexProvider:
    """Fetch the Phase 1 A-share index candles from Tencent Finance.

    The Tencent endpoint is intentionally bound to the three explicit Shanghai
    index symbols.  A bare ``000xxx`` code is never guessed here because
    ``000001`` is both the Shanghai Composite index and a Shenzhen equity.
    Public weekly candles are derived from closed daily rows; the current ISO
    week is therefore never promoted as a completed week.
    """

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
        symbol = _normalize_tencent_index_symbol(ticker)
        self.source_identity = {
            "source_id": "tencent_kline",
            "provider_symbol": symbol,
            "endpoint": _TENCENT_KLINE_URL,
            "price_adjustment": "qfq",
        }
        if symbol not in _TENCENT_INDEX_SYMBOLS:
            raise ProviderError(
                f"Unsupported Tencent index symbol: {ticker}; explicit market-prefixed symbol required",
                suggestions=[
                    "Use explicit market-prefixed symbols: sh000001, sh000688, sh000015",
                ],
            )
        if timeframe not in self.supported_timeframes():
            raise ProviderError(
                f"Tencent index timeframe {timeframe.value} is not supported",
                suggestions=[f"Supported: {[item.value for item in self.supported_timeframes()]}"] ,
            )

        self.timeframe_transform = _tencent_transform(timeframe, symbol)
        self.last_raw_response = {
            "request_params": {
                "endpoint": _TENCENT_KLINE_URL,
                "provider_symbol": symbol,
                "raw_timeframe": Timeframe.DAY.value,
                "requested_timeframe": timeframe.value,
                "requests": [],
            },
            "response_body": {"responses": []},
            "status_code": None,
            "error": None,
        }

        try:
            today = self._today()
            requested_start = _parse_optional_date(start, "start")
            requested_end = _parse_optional_date(end, "end")
            if requested_start and requested_end and requested_start >= requested_end:
                raise ProviderError("Tencent index date range is empty")
            completion_boundary = min(requested_end or today, today)
            if requested_start and requested_start >= completion_boundary:
                raise ProviderError("Tencent index date range contains no closed sessions")

            if timeframe == Timeframe.DAY:
                query_start = requested_start
                query_end = requested_end or today
                if requested_end and query_start is None:
                    query_start = query_end - timedelta(days=max(limit * 3, 365))
                rows = await self._fetch_daily_rows(
                    symbol,
                    start=query_start,
                    end=query_end,
                    limit=limit,
                    paginate=bool(query_start or requested_end),
                )
                candles = _parse_tencent_rows(rows, ticker=symbol)
                candles = _closed_daily_rows(
                    candles,
                    start=requested_start,
                    end=completion_boundary,
                )
            else:
                query_start = requested_start or (completion_boundary - timedelta(days=max(limit * 7 + 30, 365)))
                rows = await self._fetch_daily_rows(
                    symbol,
                    start=query_start,
                    end=requested_end or today,
                    limit=limit,
                    paginate=True,
                )
                candles = _parse_tencent_rows(rows, ticker=symbol)
                candles = _closed_daily_rows(
                    candles,
                    start=requested_start,
                    end=completion_boundary,
                )
                candles = _aggregate_tencent_completed_weeks(
                    candles,
                    completion_boundary=completion_boundary,
                    requested_start=requested_start,
                )
        except ProviderError as error:
            if self.last_raw_response is not None:
                self.last_raw_response["error"] = str(error)
            raise

        if not candles:
            error = ProviderError(
                f"Tencent returned no closed candles for {ticker} ({timeframe.value})",
                suggestions=[
                    "Check the requested date range and whether a completed market week is available",
                ],
            )
            self.last_raw_response["error"] = str(error)
            raise error
        if limit and len(candles) > limit:
            candles = candles[-limit:]
        self.last_raw_response["request_params"]["public_row_count"] = len(candles)
        logger.info("Fetched %s Tencent candles for %s (%s)", len(candles), symbol, timeframe.value)
        return candles

    async def _fetch_daily_rows(
        self,
        symbol: str,
        *,
        start: date | None,
        end: date | None,
        limit: int,
        paginate: bool,
    ) -> list[Any]:
        if not paginate:
            windows = [(None, None, min(max(limit + 1, 1), 500))]
        else:
            if start is None or end is None or start >= end:
                return []
            windows = []
            cursor = start
            while cursor < end:
                window_end = min(cursor + timedelta(days=365), end)
                windows.append((cursor, window_end, 500))
                cursor = window_end + timedelta(days=1)

        rows: list[Any] = []
        for window_start, window_end, window_limit in windows:
            rows.extend(
                await self._fetch_daily_window(
                    symbol,
                    start=window_start,
                    end=window_end,
                    limit=window_limit,
                )
            )
        return rows

    async def _fetch_daily_window(
        self,
        symbol: str,
        *,
        start: date | None,
        end: date | None,
        limit: int,
    ) -> list[Any]:
        start_text = start.isoformat() if start else ""
        end_text = end.isoformat() if end else ""
        param = f"{symbol},day,{start_text},{end_text},{min(limit, 500)},qfq"
        request_params = {"param": param}
        raw = self.last_raw_response
        assert raw is not None
        raw["request_params"]["requests"].append(request_params)
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                transport=self._transport,
                follow_redirects=True,
            ) as client:
                response = await client.get(_TENCENT_KLINE_URL, params=request_params)
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raw["status_code"] = exc.response.status_code
            raw["error"] = str(exc)
            raise ProviderError(f"Tencent K-line request failed for {symbol}: {exc}") from exc
        except httpx.RequestError as exc:
            raw["error"] = str(exc)
            raise ProviderError(f"Tencent K-line request failed for {symbol}: {exc}") from exc

        raw["status_code"] = response.status_code
        body = response.text
        try:
            payload = response.json()
        except ValueError as exc:
            raw["response_body"]["responses"].append({"request": request_params, "body_sha256": sha256(body.encode()).hexdigest()})
            raw["error"] = "non_json_response"
            raise ProviderError(f"Tencent returned non-JSON K-line data for {symbol}") from exc
        raw["response_body"]["responses"].append({"request": request_params, "payload": payload})

        if not isinstance(payload, Mapping) or payload.get("code") != 0:
            error = str(payload.get("msg") if isinstance(payload, Mapping) else "invalid_response")
            raw["error"] = error
            raise ProviderError(f"Tencent K-line response rejected for {symbol}: {error}")
        data = payload.get("data")
        if not isinstance(data, Mapping):
            raw["error"] = "empty_data"
            raise ProviderError(f"Tencent returned no data for {symbol}")
        item = data.get(symbol)
        if not isinstance(item, Mapping):
            returned = ",".join(sorted(str(key) for key in data.keys())) or "none"
            raw["error"] = "wrong_symbol_response"
            raise ProviderError(
                f"Tencent returned wrong symbol for {symbol}; returned symbols: {returned}"
            )
        rows = item.get("day")
        if not isinstance(rows, list) or not rows:
            raw["error"] = "empty_rows"
            raise ProviderError(f"Tencent returned no daily K-line rows for {symbol}")
        return rows


def _normalize_tencent_index_symbol(ticker: str) -> str:
    normalized = ticker.strip().lower()
    return _TENCENT_INDEX_ALIASES.get(normalized, normalized)


def _parse_optional_date(value: str | None, field: str) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError as exc:
        raise ProviderError(f"Tencent index {field} date is invalid: {value}") from exc


def _tencent_transform(timeframe: Timeframe, symbol: str) -> TimeframeTransform:
    if timeframe == Timeframe.WEEK:
        return TimeframeTransform(
            raw_timeframe=Timeframe.DAY,
            timeframe_origin="aggregated",
            aggregation={
                "kind": "ohlc_resample",
                "rule": "completed_iso_week",
                "input_timeframe": Timeframe.DAY.value,
                "bucket_timezone": "Asia/Shanghai",
                "input_source": {
                    "source_id": "tencent_kline",
                    "provider_symbol": symbol,
                },
            },
        )
    return TimeframeTransform(
        raw_timeframe=Timeframe.DAY,
        timeframe_origin="native",
        aggregation={"kind": "none", "rule": "native_passthrough"},
    )


def _parse_tencent_rows(rows: list[Any], *, ticker: str) -> list[Candle]:
    candles: list[Candle] = []
    for index, row in enumerate(rows):
        if not isinstance(row, (list, tuple)) or len(row) != 6:
            raise ProviderError(f"Tencent K-line row {index} is malformed for {ticker}")
        try:
            timestamp = date.fromisoformat(str(row[0])).isoformat()
            values = [float(row[position]) for position in range(1, 6)]
        except (TypeError, ValueError) as exc:
            raise ProviderError(f"Tencent K-line row {index} has invalid values for {ticker}") from exc
        if not all(math.isfinite(value) for value in values):
            raise ProviderError(f"Tencent K-line row {index} has non-finite values for {ticker}")
        open_value, close_value, high_value, low_value, volume = values
        if high_value < max(open_value, close_value) or low_value > min(open_value, close_value):
            raise ProviderError(f"Tencent OHLC invariant failed for {ticker} at {timestamp}")
        if volume < 0:
            raise ProviderError(f"Tencent volume is negative for {ticker} at {timestamp}")
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
            raise ProviderError(f"Tencent returned duplicate dates for {ticker}")
        if current.timestamp < previous.timestamp:
            raise ProviderError(f"Tencent returned out-of-order dates for {ticker}")
    return candles


def _closed_daily_rows(
    candles: list[Candle], *, start: date | None, end: date
) -> list[Candle]:
    return [
        candle
        for candle in candles
        if (start is None or date.fromisoformat(candle.timestamp) >= start)
        and date.fromisoformat(candle.timestamp) < end
    ]


def _aggregate_tencent_completed_weeks(
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
