"""Crypto provider — Binance public K-line API (no auth needed)."""

from __future__ import annotations

import logging

import httpx

from kline.models import Candle, Timeframe
from kline.providers.base import ProviderError

logger = logging.getLogger(__name__)

BINANCE_KLINE_URL = "https://api.binance.com/api/v3/klines"

# Binance interval mapping
_TF_MAP = {
    Timeframe.MIN_1: "1m",
    Timeframe.MIN_5: "5m",
    Timeframe.MIN_15: "15m",
    Timeframe.MIN_30: "30m",
    Timeframe.HOUR_1: "1h",
    Timeframe.HOUR_4: "4h",
    Timeframe.DAY: "1d",
    Timeframe.WEEK: "1w",
}


def _normalize_symbol(ticker: str) -> str:
    """Normalize to Binance format: BTC → BTCUSDT, btcusdt → BTCUSDT."""
    t = ticker.upper().strip()
    if not t.endswith("USDT"):
        t = f"{t}USDT"
    return t


class CryptoProvider:
    """Fetch crypto K-line data via Binance public API."""

    def __init__(self, timeout: int = 30) -> None:
        self._timeout = timeout

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
                f"Timeframe {timeframe.value} not supported for crypto",
                suggestions=[f"Supported: {[t.value for t in self.supported_timeframes()]}"],
            )

        symbol = _normalize_symbol(ticker)
        params: dict = {"symbol": symbol, "interval": interval, "limit": min(limit, 1000)}

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            try:
                resp = await client.get(BINANCE_KLINE_URL, params=params)
                resp.raise_for_status()
                data = resp.json()
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 400:
                    raise ProviderError(
                        f"Invalid symbol: {symbol}",
                        suggestions=[
                            "Use base symbol (BTC, ETH, SOL) or full pair (BTCUSDT)",
                            "Common symbols: BTC, ETH, SOL, BNB, DOGE, ADA",
                        ],
                    ) from e
                raise ProviderError(f"Binance API error: {e}") from e
            except httpx.RequestError as e:
                raise ProviderError(
                    f"Binance request failed: {e}",
                    suggestions=["Check internet connection", "Binance may be blocked in your region"],
                ) from e

        candles = []
        for item in data:
            # Binance kline format: [open_time, open, high, low, close, volume, ...]
            from datetime import datetime, timezone

            open_time_ms = item[0]
            dt = datetime.fromtimestamp(open_time_ms / 1000, tz=timezone.utc)

            if timeframe in (Timeframe.DAY, Timeframe.WEEK):
                ts_str = dt.strftime("%Y-%m-%d")
            else:
                ts_str = dt.strftime("%Y-%m-%dT%H:%M:%S")

            candles.append(
                Candle(
                    timestamp=ts_str,
                    open=float(item[1]),
                    high=float(item[2]),
                    low=float(item[3]),
                    close=float(item[4]),
                    volume=float(item[5]),
                    amount=float(item[7]),  # quote asset volume
                )
            )

        logger.info(f"Fetched {len(candles)} candles for {symbol} ({timeframe.value})")
        return candles
