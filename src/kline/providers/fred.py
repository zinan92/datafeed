"""FRED daily-series adapter for macro, flow-proxy, and event-proxy factors."""

from __future__ import annotations

from csv import DictReader
from datetime import date, timedelta
from io import StringIO

import httpx

from kline.models import Candle, Timeframe
from kline.providers.base import ProviderError


class FredCsvProvider:
    def __init__(self, *, timeout: float = 30, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._timeout = timeout
        self._transport = transport
        self.last_raw_response: dict | None = None

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
        if timeframe == Timeframe.WEEK:
            daily = await self.fetch(
                ticker,
                Timeframe.DAY,
                start=start,
                end=end,
                limit=max(limit * 7, 500),
            )
            return _aggregate_weekly_levels(daily, end=end)[-max(1, limit):]
        if timeframe != Timeframe.DAY:
            raise ProviderError("FRED supports daily and weekly observations only")
        series_id = ticker.upper().strip()
        url = "https://fred.stlouisfed.org/graph/fredgraph.csv"
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                transport=self._transport,
                follow_redirects=True,
            ) as client:
                response = await client.get(url, params={"id": series_id})
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ProviderError(f"FRED request failed: {exc}") from exc
        self.last_raw_response = {
            "url": str(response.request.url),
            "status_code": response.status_code,
            "series_id": series_id,
            "body_preview": response.text[:500],
        }
        candles: list[Candle] = []
        scale = 100.0 if series_id == "T10Y2Y" else 1.0
        for row in DictReader(StringIO(response.text)):
            raw = str(row.get(series_id, "")).strip()
            observation_date = str(row.get("observation_date", "")).strip()
            if not observation_date or not raw or raw == ".":
                continue
            if start and observation_date < start[:10]:
                continue
            if end and observation_date > end[:10]:
                continue
            value = float(raw) * scale
            candles.append(
                Candle(
                    timestamp=f"{observation_date}T00:00:00+00:00",
                    open=value,
                    high=value,
                    low=value,
                    close=value,
                    volume=0,
                )
            )
        if not candles:
            raise ProviderError(f"FRED returned no usable observations for {series_id}")
        return candles[-max(1, limit) :]


def _aggregate_weekly_levels(candles: list[Candle], *, end: str | None = None) -> list[Candle]:
    """Aggregate daily FRED levels to completed ISO weeks.

    Treasury yields and curve spreads are levels, not OHLC prices.  The
    weekly output therefore repeats the last observed level in the week and
    never invents a high/low range or a bond-price candle.
    """

    cutoff = date.fromisoformat(end[:10]) if end else date.today()
    groups: dict[tuple[int, int], list[Candle]] = {}
    for candle in candles:
        trading_date = date.fromisoformat(candle.timestamp[:10])
        iso = trading_date.isocalendar()
        groups.setdefault((int(iso.year), int(iso.week)), []).append(candle)
    output: list[Candle] = []
    for (iso_year, iso_week), rows in groups.items():
        week_monday = date.fromisocalendar(iso_year, iso_week, 1)
        if week_monday + timedelta(days=4) > cutoff:
            continue
        rows.sort(key=lambda item: item.timestamp)
        value = rows[-1].close
        output.append(Candle(timestamp=rows[-1].timestamp, open=value, high=value, low=value, close=value, volume=0))
    return sorted(output, key=lambda item: item.timestamp)
