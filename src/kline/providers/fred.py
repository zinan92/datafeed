"""FRED daily-series adapter for macro, flow-proxy, and event-proxy factors."""

from __future__ import annotations

from csv import DictReader
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
        return [Timeframe.DAY]

    async def fetch(
        self,
        ticker: str,
        timeframe: Timeframe,
        *,
        start: str | None = None,
        end: str | None = None,
        limit: int = 500,
    ) -> list[Candle]:
        if timeframe != Timeframe.DAY:
            raise ProviderError("FRED supports daily observations only")
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
        for row in DictReader(StringIO(response.text)):
            raw = str(row.get(series_id, "")).strip()
            observation_date = str(row.get("observation_date", "")).strip()
            if not observation_date or not raw or raw == ".":
                continue
            if start and observation_date < start[:10]:
                continue
            if end and observation_date > end[:10]:
                continue
            value = float(raw)
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
