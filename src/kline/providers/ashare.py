"""A-share provider — TuShare Pro for daily, AKShare as fallback."""

from __future__ import annotations

import logging
from datetime import date, timedelta

import tushare as ts

from kline.models import Candle, Timeframe
from kline.providers.base import ProviderError

logger = logging.getLogger(__name__)

# TuShare timeframe mapping
_TF_MAP = {
    Timeframe.DAY: "D",
    Timeframe.WEEK: "W",
}


def _to_tushare_code(ticker: str) -> str:
    """Convert 6-digit code to TuShare format: 000001 → 000001.SZ"""
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

        if limit and len(candles) > limit:
            candles = candles[-limit:]

        logger.info(f"Fetched {len(candles)} candles for {ticker} ({timeframe.value})")
        return candles
