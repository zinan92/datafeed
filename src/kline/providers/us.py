"""US stock provider — Yahoo Finance via yfinance."""

from __future__ import annotations

import logging

import yfinance as yf

from kline.models import Candle, Timeframe
from kline.providers.base import ProviderError

logger = logging.getLogger(__name__)

# yfinance interval mapping
_TF_MAP = {
    Timeframe.MIN_1: "1m",
    Timeframe.MIN_5: "5m",
    Timeframe.MIN_15: "15m",
    Timeframe.MIN_30: "30m",
    Timeframe.HOUR_1: "1h",
    Timeframe.DAY: "1d",
    Timeframe.WEEK: "1wk",
}

# yfinance period limits per interval
_MAX_PERIOD = {
    Timeframe.MIN_1: "7d",
    Timeframe.MIN_5: "60d",
    Timeframe.MIN_15: "60d",
    Timeframe.MIN_30: "60d",
    Timeframe.HOUR_1: "730d",
    Timeframe.DAY: "max",
    Timeframe.WEEK: "max",
}


class USStockProvider:
    """Fetch US stock K-line data via Yahoo Finance."""

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
        interval = _TF_MAP.get(timeframe)
        if not interval:
            raise ProviderError(
                f"Timeframe {timeframe.value} not supported for US stocks",
                suggestions=[f"Supported: {[t.value for t in self.supported_timeframes()]}"],
            )

        try:
            stock = yf.Ticker(ticker.upper())
            kwargs: dict = {"interval": interval}
            if start and end:
                kwargs["start"] = start
                kwargs["end"] = end
            else:
                kwargs["period"] = _MAX_PERIOD.get(timeframe, "2y")
            df = stock.history(**kwargs)
        except Exception as e:
            raise ProviderError(
                f"Yahoo Finance request failed for {ticker}: {e}",
                suggestions=[f"Verify {ticker} is a valid US stock ticker (e.g., AAPL, MSFT)"],
            ) from e

        if df is None or df.empty:
            raise ProviderError(
                f"No data returned for {ticker}",
                suggestions=[
                    f"Verify {ticker} is a valid ticker symbol",
                    "Try common symbols: AAPL, MSFT, GOOGL, AMZN",
                ],
            )

        candles = []
        for idx, row in df.iterrows():
            ts_str = idx.strftime("%Y-%m-%d") if timeframe in (Timeframe.DAY, Timeframe.WEEK) else idx.strftime("%Y-%m-%dT%H:%M:%S")
            candles.append(
                Candle(
                    timestamp=ts_str,
                    open=float(row["Open"]),
                    high=float(row["High"]),
                    low=float(row["Low"]),
                    close=float(row["Close"]),
                    volume=float(row.get("Volume", 0)),
                )
            )

        if limit and len(candles) > limit:
            candles = candles[-limit:]

        logger.info(f"Fetched {len(candles)} candles for {ticker} ({timeframe.value})")
        return candles
