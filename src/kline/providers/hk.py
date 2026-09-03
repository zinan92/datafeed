"""Hong Kong equity provider using Yahoo Finance's explicit ``.HK`` symbols."""

from __future__ import annotations

import asyncio

from kline.models import Candle, Timeframe
from kline.providers.us import USStockProvider


class HKStockProvider(USStockProvider):
    """Reuse Yahoo OHLC parsing while stamping Hong Kong market provenance."""

    def __init__(self) -> None:
        super().__init__()
        self._hk_fetch_lock = asyncio.Lock()

    async def fetch(
        self,
        ticker: str,
        timeframe: Timeframe,
        *,
        start: str | None = None,
        end: str | None = None,
        limit: int = 500,
    ) -> list[Candle]:
        async with self._hk_fetch_lock:
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
