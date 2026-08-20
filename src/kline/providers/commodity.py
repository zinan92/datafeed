"""Commodity provider — Yahoo Finance for futures (GC=F, CL=F, SI=F, etc.)."""

from __future__ import annotations

import logging

from kline.models import Candle, Timeframe
from kline.providers.base import ProviderError
from kline.providers.us import USStockProvider

logger = logging.getLogger(__name__)

# Common commodity ticker aliases
_ALIASES: dict[str, str] = {
    "GOLD": "GC=F",
    "XAUUSD": "GC=F",
    "SILVER": "SI=F",
    "XAGUSD": "SI=F",
    "OIL": "CL=F",
    "CRUDE": "CL=F",
    "WTI": "CL=F",
    "BRENT": "BZ=F",
    "NATGAS": "NG=F",
    "COPPER": "HG=F",
    "PLATINUM": "PL=F",
    "CORN": "ZC=F",
    "WHEAT": "ZW=F",
    "SOYBEAN": "ZS=F",
}


def _resolve_ticker(ticker: str) -> str:
    """Resolve common aliases to Yahoo Finance futures symbols."""
    return _ALIASES.get(ticker.upper(), ticker.upper())


class CommodityProvider:
    """Fetch commodity K-line data via Yahoo Finance futures symbols."""

    def __init__(self) -> None:
        self._us_provider = USStockProvider()

    @property
    def last_raw_response(self) -> dict | None:
        return self._us_provider.last_raw_response

    @property
    def timeframe_transform(self):
        return self._us_provider.timeframe_transform

    @property
    def source_identity(self) -> dict:
        return self._us_provider.source_identity

    def supported_timeframes(self) -> list[Timeframe]:
        return self._us_provider.supported_timeframes()

    async def fetch(
        self,
        ticker: str,
        timeframe: Timeframe,
        *,
        start: str | None = None,
        end: str | None = None,
        limit: int = 500,
    ) -> list[Candle]:
        resolved = _resolve_ticker(ticker)
        try:
            return await self._us_provider.fetch(
                resolved, timeframe, start=start, end=end, limit=limit
            )
        except ProviderError:
            raise ProviderError(
                f"No data for commodity {ticker} (resolved to {resolved})",
                suggestions=[
                    "Common symbols: GOLD (GC=F), OIL (CL=F), SILVER (SI=F)",
                    "Use Yahoo Finance futures format: GC=F, CL=F, SI=F",
                    f"Available aliases: {list(_ALIASES.keys())}",
                ],
            )
