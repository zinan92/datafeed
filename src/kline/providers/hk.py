"""Hong Kong equity provider using Yahoo Finance's explicit ``.HK`` symbols."""

from __future__ import annotations

from kline.models import Candle, Timeframe
from kline.providers.us import USStockProvider


class HKStockProvider(USStockProvider):
    """Reuse Yahoo OHLC parsing while stamping Hong Kong market provenance."""

    async def fetch(
        self,
        ticker: str,
        timeframe: Timeframe,
        *,
        start: str | None = None,
        end: str | None = None,
        limit: int = 500,
    ) -> list[Candle]:
        candles = await super().fetch(
            ticker,
            timeframe,
            start=start,
            end=end,
            limit=limit,
        )
        self.source_identity = {
            **self.source_identity,
            "market": "HK",
            "listing_venue": "HKEX",
        }
        return candles
