"""Free personal-use US stock provider backed by Yahoo Finance."""

from __future__ import annotations

import time

import httpx

from kline.market_calendar import aggregate_15m_to_4h
from kline.models import Candle, Timeframe, TimeframeTransform
from kline.providers.base import ProviderError
from kline.providers.free_common import RequestPacer, requested_cutoff
from kline.providers.us import USStockProvider
from kline.storage import CandleSeriesKey, MvpCandle


_YAHOO_TICKER_ALIASES = {"BRK.B": "BRK-B"}


def _yahoo_ticker(ticker: str) -> str:
    normalized = ticker.upper().strip()
    return _YAHOO_TICKER_ALIASES.get(normalized, normalized)


class USFreeProvider(USStockProvider):
    """Use Yahoo Finance for every US-stock timeframe in the free profile."""

    def __init__(
        self,
        *,
        timeout: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
        request_interval_seconds: float = 0.25,
    ):
        super().__init__()
        self._timeout = timeout
        self._transport = transport
        self._pacer = RequestPacer(request_interval_seconds)
        self.last_attempts: list[dict[str, object]] = []

    def _record_attempt(
        self,
        *,
        source: str,
        started_at: float,
        status: str,
        endpoint: str | None = None,
        http_status: int | None = None,
        error: str | None = None,
        row_count: int | None = None,
    ) -> None:
        record: dict[str, object] = {
            "source": source,
            "status": status,
            "latency_ms": round((time.perf_counter() - started_at) * 1000, 1),
        }
        if endpoint:
            record["endpoint"] = endpoint
        if http_status is not None:
            record["http_status"] = http_status
        if row_count is not None:
            record["row_count"] = row_count
        if error:
            record["error"] = str(error)[:240]
        self.last_attempts.append(record)

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
        self.last_attempts = []
        self.last_raw_response = None
        self.source_identity = {}
        self.timeframe_transform = None
        requested_ticker = ticker.upper().strip()
        yahoo_ticker = _yahoo_ticker(requested_ticker)
        if timeframe == Timeframe.HOUR_4:
            started_at = time.perf_counter()
            await self._pacer.wait()
            try:
                base = await super().fetch(
                    yahoo_ticker,
                    Timeframe.MIN_15,
                    start=start,
                    end=end,
                    limit=max(limit * 16, 320),
                )
            except ProviderError as error:
                self._record_attempt(
                    source="yahoo",
                    started_at=started_at,
                    status="error",
                    error=str(error),
                )
                raise
            self._record_attempt(
                source="yahoo",
                started_at=started_at,
                status="success",
                row_count=len(base),
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
                "requested_symbol": requested_ticker,
                "provider_symbol": yahoo_ticker,
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
        if timeframe == Timeframe.WEEK:
            # The inherited weekly transform calls back into this provider for
            # daily bars; that inner call owns the Yahoo attempt receipt.
            return await super().fetch(ticker, timeframe, start=start, end=end, limit=limit)
        if timeframe == Timeframe.DAY:
            return await self._fetch_yahoo_native(
                ticker, Timeframe.DAY, start=start, end=end, limit=limit
            )
        return await self._fetch_yahoo_native(
            ticker, timeframe, start=start, end=end, limit=limit
        )

    async def _fetch_yahoo_native(
        self,
        ticker: str,
        timeframe: Timeframe,
        *,
        start: str | None,
        end: str | None,
        limit: int,
    ) -> list[Candle]:
        """Fetch one native timeframe from Yahoo and record its identity."""

        requested_ticker = ticker.upper().strip()
        yahoo_ticker = _yahoo_ticker(requested_ticker)
        started_at = time.perf_counter()
        await self._pacer.wait()
        try:
            candles = await super().fetch(
                yahoo_ticker, timeframe, start=start, end=end, limit=limit
            )
        except ProviderError as yahoo_error:
            self._record_attempt(
                source="yahoo",
                started_at=started_at,
                status="error",
                error=str(yahoo_error),
            )
            raise
        self._record_attempt(
            source="yahoo",
            started_at=started_at,
            status="success",
            row_count=len(candles),
        )
        self.source_identity = {
            **self.source_identity,
            "source_id": "yahoo_finance_free",
            "requested_symbol": requested_ticker,
            "provider_symbol": yahoo_ticker,
            "selected_source": "yahoo",
            "fallback_from": None,
            "adjustment_basis": "raw_unadjusted",
        }
        return candles
