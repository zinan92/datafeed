"""Binance USD-M Futures provider for trading-live candles."""

from __future__ import annotations

import asyncio
import json
import logging
from decimal import Decimal
from datetime import datetime, timezone
from typing import Any, AsyncIterator

import httpx

from kline.models import AssetClass, Candle, InstrumentDefinition, Timeframe
from kline.providers.base import ProviderError

logger = logging.getLogger(__name__)

BINANCE_USDM_KLINE_URL = "https://fapi.binance.com/fapi/v1/klines"
BINANCE_USDM_EXCHANGE_INFO_URL = "https://fapi.binance.com/fapi/v1/exchangeInfo"
BINANCE_USDM_WS_URL = "wss://fstream.binance.com/stream?streams="

_TF_MAP = {
    Timeframe.MIN_1: "1m",
    Timeframe.MIN_5: "5m",
    Timeframe.MIN_15: "15m",
    Timeframe.MIN_30: "30m",
    Timeframe.HOUR_1: "1h",
    Timeframe.HOUR_4: "4h",
    Timeframe.DAY: "1d",
}
_TF_MILLIS = {
    Timeframe.MIN_1: 60_000,
    Timeframe.MIN_5: 300_000,
    Timeframe.MIN_15: 900_000,
    Timeframe.MIN_30: 1_800_000,
    Timeframe.HOUR_1: 3_600_000,
    Timeframe.HOUR_4: 14_400_000,
}

_ALIASES = {
    "GOLD": "XAUUSDT",
    "XAUUSD": "XAUUSDT",
    "XAUUSDT": "XAUUSDT",
}


def _normalize_symbol(ticker: str) -> str:
    symbol = _ALIASES.get(ticker.upper().strip(), ticker.upper().strip())
    if symbol != "XAUUSDT":
        raise ProviderError(
            f"Symbol {ticker} is not enabled for Binance USD-M Futures live mode",
            suggestions=["Use XAUUSDT for live gold futures candles"],
        )
    return symbol


def _to_millis(value: str) -> int:
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _timestamp_from_ms(open_time_ms: int) -> str:
    dt = datetime.fromtimestamp(open_time_ms / 1000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _candle_from_rest_item(item: list[Any]) -> Candle:
    return Candle(
        timestamp=_timestamp_from_ms(int(item[0])),
        open=float(item[1]),
        high=float(item[2]),
        low=float(item[3]),
        close=float(item[4]),
        volume=float(item[5]),
        amount=float(item[7]),
    )


def _candle_from_ws_kline(item: dict[str, Any]) -> Candle:
    return Candle(
        timestamp=_timestamp_from_ms(int(item["t"])),
        open=float(item["o"]),
        high=float(item["h"]),
        low=float(item["l"]),
        close=float(item["c"]),
        volume=float(item["v"]),
        amount=float(item.get("q", 0)),
    )


def _decimal_rate_from_percent(value: str) -> str:
    return format(Decimal(value) / Decimal(100), "f")


def _required_filter(filters: list[dict[str, Any]], filter_type: str) -> dict[str, Any]:
    match = next((item for item in filters if item.get("filterType") == filter_type), None)
    if match is None:
        raise ProviderError(f"Binance exchangeInfo omitted required {filter_type} filter")
    return match


class BinanceUsdmFuturesProvider:
    """Fetch and stream XAUUSDT candles from Binance USD-M Futures."""

    def __init__(
        self,
        timeout: float = 30,
        transport: httpx.AsyncBaseTransport | None = None,
        websocket_connect: Any | None = None,
    ) -> None:
        self._timeout = timeout
        self._transport = transport
        self._websocket_connect = websocket_connect
        self.last_raw_response: dict[str, Any] | None = None

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
        self.last_raw_response = None
        interval = _TF_MAP.get(timeframe)
        if not interval:
            raise ProviderError(
                f"Timeframe {timeframe.value} not supported for Binance USD-M Futures",
                suggestions=[f"Supported: {[t.value for t in self.supported_timeframes()]}"],
            )

        symbol = _normalize_symbol(ticker)
        client_kwargs: dict[str, Any] = {"timeout": self._timeout}
        if self._transport is not None:
            client_kwargs["transport"] = self._transport

        requested = max(1, limit)
        cursor = _to_millis(start) if start else None
        end_ms = _to_millis(end) if end else None
        raw_pages: list[list[Any]] = []
        async with httpx.AsyncClient(**client_kwargs) as client:
            while len(raw_pages) == 0 or (cursor is not None and sum(map(len, raw_pages)) < requested):
                remaining = requested - sum(map(len, raw_pages))
                params: dict[str, Any] = {
                    "symbol": symbol,
                    "interval": interval,
                    "limit": min(max(1, remaining), 1500),
                }
                if cursor is not None:
                    params["startTime"] = cursor
                if end_ms is not None:
                    params["endTime"] = end_ms
                try:
                    resp = await client.get(BINANCE_USDM_KLINE_URL, params=params)
                except httpx.RequestError as e:
                    self.last_raw_response = {
                        "request_params": params,
                        "response_body": None,
                        "status_code": None,
                        "error": str(e),
                    }
                    raise ProviderError(
                        f"Binance USD-M Futures request failed: {e}",
                        suggestions=["Check network access to fapi.binance.com"],
                    ) from e
                try:
                    data = resp.json()
                except ValueError:
                    data = resp.text
                if resp.status_code >= 400:
                    raise ProviderError(
                        f"Binance USD-M Futures API error {resp.status_code}: {data}",
                        suggestions=["Verify XAUUSDT is available on Binance USD-M Futures"],
                    )
                if not isinstance(data, list):
                    raise ProviderError("Binance USD-M Futures returned a non-list kline payload")
                raw_pages.append(data)
                if cursor is None or not data or len(data) < params["limit"]:
                    break
                next_cursor = int(data[-1][0]) + _TF_MILLIS[timeframe]
                if next_cursor <= cursor or (end_ms is not None and next_cursor > end_ms):
                    break
                cursor = next_cursor

        flat_data = [item for page in raw_pages for item in page]
        self.last_raw_response = {
            "request_params": {"symbol": symbol, "interval": interval, "start": start, "end": end, "limit": limit},
            "response_body": flat_data,
            "status_code": 200,
            "error": None,
            "page_count": len(raw_pages),
        }
        candles = [_candle_from_rest_item(item) for item in flat_data]
        logger.info("Fetched %s Binance USD-M Futures candles for %s", len(candles), symbol)
        return candles

    async def stream(self, ticker: str, timeframe: Timeframe) -> AsyncIterator[Candle]:
        interval = _TF_MAP.get(timeframe)
        if not interval:
            raise ProviderError(
                f"Timeframe {timeframe.value} not supported for Binance USD-M Futures",
                suggestions=[f"Supported: {[t.value for t in self.supported_timeframes()]}"],
            )

        symbol = _normalize_symbol(ticker)
        stream_name = f"{symbol.lower()}@kline_{interval}"
        url = f"{BINANCE_USDM_WS_URL}{stream_name}"

        try:
            import websockets
        except ImportError as e:
            raise ProviderError(
                "WebSocket client dependency is not installed",
                suggestions=["Install the websockets package"],
            ) from e

        connect = self._websocket_connect or websockets.connect
        try:
            async with connect(url, open_timeout=self._timeout) as websocket:
                while True:
                    try:
                        raw_message = await asyncio.wait_for(websocket.recv(), timeout=self._timeout)
                    except TimeoutError as e:
                        raise ProviderError(
                            f"Binance USD-M Futures stream returned no kline update within {self._timeout:g}s",
                            suggestions=["Check the WebSocket proxy path to fstream.binance.com"],
                        ) from e
                    message = json.loads(raw_message)
                    kline = message.get("data", {}).get("k")
                    if not kline:
                        continue
                    yield _candle_from_ws_kline(kline)
        except ProviderError:
            raise
        except (OSError, TimeoutError) as e:
            raise ProviderError(
                f"Binance USD-M Futures stream failed: {e}",
                suggestions=["Check network and proxy access to fstream.binance.com"],
            ) from e

    async def fetch_instrument_definition(self, ticker: str) -> InstrumentDefinition:
        symbol = _normalize_symbol(ticker)
        params = {"symbol": symbol}
        client_kwargs: dict[str, Any] = {"timeout": self._timeout}
        if self._transport is not None:
            client_kwargs["transport"] = self._transport

        async with httpx.AsyncClient(**client_kwargs) as client:
            try:
                response = await client.get(BINANCE_USDM_EXCHANGE_INFO_URL, params=params)
            except httpx.RequestError as e:
                self.last_raw_response = {
                    "request_params": params,
                    "response_body": None,
                    "status_code": None,
                    "error": str(e),
                }
                raise ProviderError(
                    f"Binance USD-M Futures exchangeInfo request failed: {e}",
                    suggestions=["Check network access to fapi.binance.com"],
                ) from e

        try:
            payload = response.json()
        except ValueError:
            payload = response.text
        self.last_raw_response = {
            "request_params": params,
            "response_body": payload,
            "status_code": response.status_code,
            "error": None,
        }
        if response.status_code >= 400:
            raise ProviderError(
                f"Binance USD-M Futures exchangeInfo error {response.status_code}: {payload}"
            )
        if not isinstance(payload, dict) or not isinstance(payload.get("symbols"), list):
            raise ProviderError("Binance USD-M Futures returned invalid exchangeInfo")

        item = next((row for row in payload["symbols"] if row.get("symbol") == symbol), None)
        if item is None:
            raise ProviderError(f"Binance exchangeInfo did not include {symbol}")
        filters = item.get("filters") or []
        price_filter = _required_filter(filters, "PRICE_FILTER")
        lot_filter = _required_filter(filters, "LOT_SIZE")
        market_lot_filter = _required_filter(filters, "MARKET_LOT_SIZE")
        notional_filter = _required_filter(filters, "MIN_NOTIONAL")
        observed_at = datetime.now(timezone.utc).isoformat()

        return InstrumentDefinition(
            instrument_id=f"{symbol}.BINANCE",
            venue="BINANCE",
            symbol=symbol,
            asset_class=AssetClass.COMMODITY,
            market_type="usd_m_futures",
            contract_type=str(item["contractType"]),
            status=str(item["status"]),
            base_currency=str(item["baseAsset"]),
            quote_currency=str(item["quoteAsset"]),
            settlement_currency=str(item["marginAsset"]),
            margin_currency=str(item["marginAsset"]),
            is_inverse=False,
            price_precision=int(item["pricePrecision"]),
            price_increment=str(price_filter["tickSize"]),
            min_price=str(price_filter["minPrice"]),
            max_price=str(price_filter["maxPrice"]),
            size_precision=int(item["quantityPrecision"]),
            size_increment=str(lot_filter["stepSize"]),
            min_quantity=str(lot_filter["minQty"]),
            max_quantity=str(lot_filter["maxQty"]),
            market_size_increment=str(market_lot_filter["stepSize"]),
            market_min_quantity=str(market_lot_filter["minQty"]),
            market_max_quantity=str(market_lot_filter["maxQty"]),
            min_notional=str(notional_filter["notional"]),
            margin_init_rate=_decimal_rate_from_percent(str(item["requiredMarginPercent"])),
            margin_maint_rate=_decimal_rate_from_percent(str(item["maintMarginPercent"])),
            contract_multiplier="1",
            order_types=[str(value) for value in item.get("orderTypes", [])],
            time_in_force=[str(value) for value in item.get("timeInForce", [])],
            provider="binance_usdm_futures",
            source_mode="binance_usdm_futures",
            execution_venue=True,
            upstream_server_time=payload.get("serverTime"),
            observed_at=observed_at,
            missing_fields=["maker_fee_rate", "taker_fee_rate"],
            derived_fields={
                "is_inverse": "binance_usdm_linear_contract",
                "contract_multiplier": "usd_m_notional_equals_price_times_quantity",
            },
        )
