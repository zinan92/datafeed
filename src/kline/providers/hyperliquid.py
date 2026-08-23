"""Hyperliquid public perpetual candles for research-only market context."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import math
from typing import Any

import httpx

from kline.models import Candle, Timeframe, TimeframeTransform
from kline.providers.base import ProviderError


HYPERLIQUID_INFO_URL = "https://api.hyperliquid.xyz/info"

_TF_MAP = {
    Timeframe.HOUR_4: ("4h", 4 * 60 * 60 * 1000),
    Timeframe.DAY: ("1d", 24 * 60 * 60 * 1000),
}


def _parse_bound(value: str | None) -> int | None:
    if not value:
        return None
    raw = value.strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def _completed_weekly(candles: list[Candle], *, cutoff: datetime) -> list[Candle]:
    groups: dict[tuple[int, int], list[Candle]] = {}
    for candle in candles:
        stamp = datetime.fromisoformat(candle.timestamp.replace("Z", "+00:00"))
        if stamp > cutoff:
            continue
        iso = stamp.date().isocalendar()
        groups.setdefault((iso.year, iso.week), []).append(candle)
    result: list[Candle] = []
    for _, rows in sorted(groups.items()):
        if len(rows) < 7:
            continue
        first_date = datetime.fromisoformat(rows[0].timestamp.replace("Z", "+00:00")).date()
        week_end = date.fromisocalendar(first_date.isocalendar().year, first_date.isocalendar().week, 5)
        result.append(
            Candle(
                timestamp=week_end.isoformat(),
                open=rows[0].open,
                high=max(row.high for row in rows),
                low=min(row.low for row in rows),
                close=rows[-1].close,
                volume=sum(row.volume for row in rows),
                amount=sum(row.amount or 0 for row in rows),
            )
        )
    return result


class HyperliquidPerpetualProvider:
    """Fetch an explicit crypto-perpetual allowlist from Hyperliquid."""

    def __init__(self, timeout: float = 30, transport: httpx.AsyncBaseTransport | None = None, allowed_symbols: set[str] | None = None) -> None:
        self._timeout = timeout
        self._transport = transport
        self._allowed_symbols = {item.upper() for item in (allowed_symbols or {"BTC", "ETH", "HYPE"})}
        self.last_raw_response: dict[str, Any] | None = None
        self.timeframe_transform: TimeframeTransform | None = None
        self.source_identity: dict[str, Any] = {}

    def supported_timeframes(self) -> list[Timeframe]:
        return [Timeframe.HOUR_4, Timeframe.DAY, Timeframe.WEEK]

    async def fetch(
        self,
        ticker: str,
        timeframe: Timeframe,
        *,
        start: str | None = None,
        end: str | None = None,
        limit: int = 500,
    ) -> list[Candle]:
        symbol = ticker.upper().strip()
        if symbol not in self._allowed_symbols:
            raise ProviderError(
                f"Hyperliquid perpetual source does not enable {symbol}",
                suggestions=[f"Use one of: {', '.join(sorted(self._allowed_symbols))}"],
            )
        if timeframe == Timeframe.WEEK:
            daily = await self.fetch(
                symbol,
                Timeframe.DAY,
                start=start,
                end=end,
                limit=max(limit * 7 + 8, 100),
            )
            cutoff = datetime.now(timezone.utc)
            candles = _completed_weekly(daily, cutoff=cutoff)
            if not candles:
                raise ProviderError(f"No completed weekly data returned for {symbol}")
            if limit:
                candles = candles[-limit:]
            self.timeframe_transform = TimeframeTransform(
                raw_timeframe=Timeframe.DAY,
                timeframe_origin="aggregated",
                aggregation={
                    "kind": "ohlc_resample",
                    "rule": "completed_iso_week",
                    "input_timeframe": "1d",
                    "bucket_timezone": "UTC",
                },
            )
            return candles

        interval, interval_ms = _TF_MAP.get(timeframe, (None, None))
        if interval is None or interval_ms is None:
            raise ProviderError("Hyperliquid perpetual source supports only 4h, 1d, and 1w")
        now = datetime.now(timezone.utc)
        start_ms = _parse_bound(start)
        end_ms = _parse_bound(end)
        if start_ms is None:
            start_ms = int((now - timedelta(milliseconds=interval_ms * max(limit + 2, 20))).timestamp() * 1000)
        if end_ms is None:
            end_ms = int(now.timestamp() * 1000)
        else:
            end_ms -= 1
        payload = {
            "type": "candleSnapshot",
            "req": {"coin": symbol, "interval": interval, "startTime": start_ms, "endTime": end_ms},
        }
        self.last_raw_response = {
            "request_payload": payload,
            "response_body": None,
            "status_code": None,
            "error": None,
        }
        client_kwargs: dict[str, Any] = {"timeout": self._timeout}
        if self._transport is not None:
            client_kwargs["transport"] = self._transport
        async with httpx.AsyncClient(**client_kwargs) as client:
            try:
                response = await client.post(
                    HYPERLIQUID_INFO_URL,
                    json=payload,
                    headers={"User-Agent": "datafeed-research/1.0"},
                )
                response.raise_for_status()
                data = response.json()
            except httpx.HTTPStatusError as exc:
                self.last_raw_response["status_code"] = exc.response.status_code
                self.last_raw_response["error"] = exc.response.text[:500]
                raise ProviderError(f"Hyperliquid API error {exc.response.status_code}") from exc
            except (httpx.RequestError, ValueError) as exc:
                self.last_raw_response["error"] = str(exc)
                raise ProviderError(f"Hyperliquid request failed: {exc}") from exc
        self.last_raw_response["status_code"] = 200
        self.last_raw_response["response_body"] = data
        if not isinstance(data, list):
            raise ProviderError("Hyperliquid returned a non-list candle payload")
        candles: list[Candle] = []
        for item in data:
            try:
                open_value = float(item["o"])
                high_value = float(item["h"])
                low_value = float(item["l"])
                close_value = float(item["c"])
                volume = float(item["v"])
                stamp = datetime.fromtimestamp(float(item["t"]) / 1000, tz=timezone.utc)
            except (KeyError, TypeError, ValueError, OverflowError) as exc:
                raise ProviderError("Hyperliquid candle payload is malformed") from exc
            if not all(math.isfinite(value) for value in (open_value, high_value, low_value, close_value, volume)):
                raise ProviderError("Hyperliquid candle contains a non-finite value")
            if high_value < max(open_value, close_value) or low_value > min(open_value, close_value) or volume < 0:
                raise ProviderError("Hyperliquid OHLC invariant failed")
            end_stamp = datetime.fromtimestamp(float(item.get("T", item["t"])) / 1000, tz=timezone.utc)
            if end_stamp > now:
                continue
            candles.append(
                Candle(
                    timestamp=stamp.isoformat().replace("+00:00", "Z"),
                    open=open_value,
                    high=high_value,
                    low=low_value,
                    close=close_value,
                    volume=volume,
                    amount=None,
                )
            )
        candles.sort(key=lambda row: row.timestamp)
        if limit:
            candles = candles[-limit:]
        if not candles:
            raise ProviderError(f"No completed Hyperliquid candles returned for {symbol}")
        self.timeframe_transform = TimeframeTransform(
            raw_timeframe=timeframe,
            timeframe_origin="native",
            aggregation={"kind": "none", "rule": "native_passthrough"},
        )
        self.source_identity = {
            "provider_symbol": symbol,
            "venue": "hyperliquid",
            "contract_type": "perpetual",
            "settlement": "USDC",
        }
        return candles
