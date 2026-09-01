"""Free personal-use US stock provider: Yahoo Chart with Sina daily fallback."""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
import re

import httpx

from kline.market_calendar import aggregate_15m_to_4h
from kline.models import Candle, Timeframe, TimeframeTransform
from kline.providers.base import ProviderError
from kline.providers.free_common import requested_cutoff
from kline.providers.us import USStockProvider
from kline.storage import CandleSeriesKey, MvpCandle


_SINA_DAILY_URL = (
    "https://stock.finance.sina.com.cn/usstock/api/jsonp.php/var/US_MinKService.getDailyK"
)


class USFreeProvider(USStockProvider):
    """Use the existing Yahoo implementation and fall back to Sina daily K-lines."""

    def __init__(self, *, timeout: float = 15.0, transport: httpx.AsyncBaseTransport | None = None):
        super().__init__()
        self._timeout = timeout
        self._transport = transport

    def supported_timeframes(self) -> list[Timeframe]:
        return [Timeframe.MIN_15, Timeframe.HOUR_1, Timeframe.HOUR_4, Timeframe.DAY, Timeframe.WEEK]

    async def fetch(
        self,
        ticker: str,
        timeframe: Timeframe,
        *,
        start: str | None = None,
        end: str | None = None,
        limit: int = 500,
    ) -> list[Candle]:
        if timeframe == Timeframe.HOUR_4:
            base = await super().fetch(
                ticker,
                Timeframe.MIN_15,
                start=start,
                end=end,
                limit=max(limit * 16, 320),
            )
            key = CandleSeriesKey(
                instrument_id=f"US.EQ.{ticker.upper()}",
                display_symbol=ticker.upper(),
                provider_symbol=ticker.upper(),
                source_id="yahoo_finance_free",
                asset_class="us_stock",
                timeframe="15m",
                adjustment_basis="raw_unadjusted",
                manifest_version="mvp_universe_v1",
            )
            rows = [
                MvpCandle(
                    key=key,
                    timestamp=candle.timestamp,
                    open=candle.open,
                    high=candle.high,
                    low=candle.low,
                    close=candle.close,
                    volume=candle.volume,
                    amount=candle.amount,
                    volume_semantics="traded",
                )
                for candle in base
            ]
            aggregate = aggregate_15m_to_4h(
                rows,
                calendar_id="us_equities",
                cutoff=requested_cutoff(end),
                run_id="free-yahoo-preview",
            )
            if not aggregate.candles:
                error = ProviderError(f"no complete 4h US bars returned for {ticker}")
                error.code = "transform_incomplete"
                raise error
            receipt = aggregate.transform_receipt
            self.timeframe_transform = TimeframeTransform(
                raw_timeframe=Timeframe.MIN_15,
                timeframe_origin="aggregated",
                aggregation={
                    "rule": receipt.aggregation_rule_version
                    if receipt
                    else "us_regular_fixed_4h_v1",
                    "partial_bucket_count": receipt.partial_bucket_count if receipt else 0,
                    "bucket_anchor": receipt.bucket_anchor if receipt else "09:30",
                    "partial_bucket_policy": receipt.partial_bucket_policy
                    if receipt
                    else "drop_and_record",
                },
            )
            self.source_identity = {
                **self.source_identity,
                "source_id": "yahoo_finance_free",
                "selected_source": "yahoo",
            }
            selected = aggregate.candles[-limit:] if limit else aggregate.candles
            return [
                Candle(
                    timestamp=row.timestamp,
                    open=row.open,
                    high=row.high,
                    low=row.low,
                    close=row.close,
                    volume=row.volume,
                    amount=row.amount,
                )
                for row in selected
            ]
        try:
            candles = await super().fetch(ticker, timeframe, start=start, end=end, limit=limit)
            selected = self.source_identity.get("selected_source", "yahoo")
            fallback_from = self.source_identity.get("fallback_from")
            self.source_identity = {
                **self.source_identity,
                "source_id": "yahoo_finance_free",
                "selected_source": selected,
                "fallback_from": fallback_from,
            }
            return candles
        except ProviderError as yahoo_error:
            if timeframe != Timeframe.DAY:
                raise
            try:
                candles = await self._fetch_sina_daily(ticker, start=start, end=end, limit=limit)
                self.timeframe_transform = None
                self.source_identity = {
                    "source_id": "yahoo_finance_free",
                    "provider_symbol": ticker.upper(),
                    "selected_source": "sina",
                    "fallback_from": "yahoo",
                }
                return candles
            except ProviderError as sina_error:
                raise ProviderError(
                    f"free US sources failed for {ticker}: Yahoo={yahoo_error}; Sina={sina_error}"
                ) from sina_error

    async def _fetch_sina_daily(
        self, ticker: str, *, start: str | None, end: str | None, limit: int
    ) -> list[Candle]:
        params = {"symbol": ticker.upper(), "num": max(120, min(limit, 5000))}
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                transport=self._transport,
                headers={"Referer": "https://finance.sina.com.cn/"},
            ) as client:
                response = await client.get(_SINA_DAILY_URL, params=params)
                response.raise_for_status()
            match = re.search(r"\((\[.*\])\)", response.text, flags=re.DOTALL)
            rows = json.loads(match.group(1)) if match else []
            if not isinstance(rows, list):
                rows = []
            candles: list[Candle] = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                try:
                    values = [float(row[name]) for name in ("o", "h", "l", "c")]
                    volume = float(row["v"]) if row.get("v") not in (None, "") else None
                    amount = float(row["a"]) if row.get("a") not in (None, "") else None
                    if values[1] < max(values[0], values[3]) or values[2] > min(
                        values[0], values[3]
                    ):
                        continue
                    candles.append(
                        Candle(
                            timestamp=datetime.fromisoformat(str(row["d"]))
                            .replace(tzinfo=timezone.utc)
                            .isoformat(),
                            open=values[0],
                            high=values[1],
                            low=values[2],
                            close=values[3],
                            volume=volume,
                            amount=amount,
                        )
                    )
                except (KeyError, TypeError, ValueError):
                    continue
            if start:
                candles = [candle for candle in candles if candle.timestamp >= start]
            if end:
                candles = [candle for candle in candles if candle.timestamp < end]
            if limit:
                candles = candles[-limit:]
            if not candles:
                raise ProviderError("Sina returned no daily rows")
            self.last_raw_response = {
                "endpoint": str(response.url),
                "http_status": response.status_code,
                "response_sha256": sha256(response.content).hexdigest(),
                "row_count": len(candles),
                "fallback_from": "yahoo",
            }
            return candles
        except (httpx.HTTPError, ValueError, ProviderError) as error:
            raise ProviderError(f"Sina daily request failed for {ticker}: {error}") from error
