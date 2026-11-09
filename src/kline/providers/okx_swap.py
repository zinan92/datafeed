"""OKX public USDT-margined perpetual swaps (``XAU-USDT-SWAP``, ``BTC-USDT-SWAP`` ...).

A second execution-live venue beside Binance USD-M Futures. It is selected on
purpose (``source=okx_swap_public``) and never used as a silent fallback: each
response carries ``provider=okx_swap_public`` and the OKX instrument id.

OKX differences the adapter hides from callers:

* Candles come newest first and are reversed to oldest first.
* ``/market/candles`` serves the most recent 1,440 bars; older bars come from
  ``/market/history-candles``. Both page backwards with ``after=<ts>``.
* Daily bars default to Hong Kong midnight; ``1Dutc`` is requested so a day is
  a UTC day, the same as every other source in this service.
* Swap volume is reported in contracts; ``volCcy`` (base currency, e.g. XAU)
  is used so volume means the same thing as Binance's base-asset volume.
* Sizes are in contracts; the instrument definition converts them to base
  units with ``ctVal`` so ``price × quantity`` is the USDT notional.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, AsyncIterator

import httpx

from kline.models import AssetClass, Candle, InstrumentDefinition, Timeframe, TimeframeTransform
from kline.providers.base import ProviderError

logger = logging.getLogger(__name__)

OKX_BASE_URL = "https://www.okx.com"
OKX_CANDLES_PATH = "/api/v5/market/candles"
OKX_HISTORY_CANDLES_PATH = "/api/v5/market/history-candles"
OKX_INSTRUMENTS_PATH = "/api/v5/public/instruments"
OKX_TIME_PATH = "/api/v5/public/time"
OKX_POSITION_TIERS_PATH = "/api/v5/public/position-tiers"
OKX_FUNDING_HISTORY_PATH = "/api/v5/public/funding-rate-history"

SOURCE_MODE = "okx_swap_public"

_TF_MAP = {
    Timeframe.MIN_1: "1m",
    Timeframe.MIN_5: "5m",
    Timeframe.MIN_15: "15m",
    Timeframe.MIN_30: "30m",
    Timeframe.HOUR_1: "1H",
    Timeframe.HOUR_4: "4H",
    Timeframe.DAY: "1Dutc",
    Timeframe.WEEK: "1Wutc",
}

# Recent endpoint pages hold up to 300 rows; history pages up to 100.
_RECENT_PAGE = 300
_HISTORY_PAGE = 100
_MAX_PAGES = 60

_COMMODITY_BASES = frozenset({"XAU", "XAG", "CL", "BZ"})

_ALIASES = {
    "GOLD": "XAU-USDT-SWAP",
    "XAU": "XAU-USDT-SWAP",
    "XAUUSD": "XAU-USDT-SWAP",
    "XAUUSDT": "XAU-USDT-SWAP",
    # Silver and crude (datafeed#188): listed on OKX in 2026, so their history is short.
    "SILVER": "XAG-USDT-SWAP",
    "XAG": "XAG-USDT-SWAP",
    "XAGUSDT": "XAG-USDT-SWAP",
    "WTI": "CL-USDT-SWAP",
    "CL": "CL-USDT-SWAP",
    "BRENT": "BZ-USDT-SWAP",
    "BZ": "BZ-USDT-SWAP",
    "BTC": "BTC-USDT-SWAP",
    "BTCUSDT": "BTC-USDT-SWAP",
    "ETH": "ETH-USDT-SWAP",
    "ETHUSDT": "ETH-USDT-SWAP",
}

DEFAULT_ALLOWED_SYMBOLS = frozenset({"XAU-USDT-SWAP", "XAG-USDT-SWAP", "CL-USDT-SWAP", "BZ-USDT-SWAP"})


def normalize_okx_symbol(ticker: str, allowed_symbols: set[str] | frozenset[str]) -> str:
    raw = ticker.upper().strip()
    symbol = _ALIASES.get(raw, raw)
    if symbol not in allowed_symbols:
        enabled = ", ".join(sorted(allowed_symbols))
        raise ProviderError(
            f"Symbol {ticker} is not enabled for OKX swaps",
            suggestions=[f"Use one of the explicitly enabled symbols: {enabled}"],
        )
    return symbol


def _to_millis(value: str) -> int:
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def _timestamp(open_ms: int, timeframe: Timeframe) -> str:
    stamp = datetime.fromtimestamp(open_ms / 1000, tz=timezone.utc)
    if timeframe in {Timeframe.DAY, Timeframe.WEEK}:
        return stamp.date().isoformat()
    return stamp.strftime("%Y-%m-%dT%H:%M:%S")


def _candle(row: list[Any], timeframe: Timeframe) -> Candle:
    # [ts, o, h, l, c, vol(contracts), volCcy(base), volCcyQuote, confirm]
    return Candle(
        timestamp=_timestamp(int(row[0]), timeframe),
        open=float(row[1]),
        high=float(row[2]),
        low=float(row[3]),
        close=float(row[4]),
        volume=float(row[6]),
        amount=float(row[7]),
    )


def confirmed_candles(rows: list[list[Any]], timeframe: Timeframe) -> list[Candle]:
    """Completed bars only; ``confirm`` is "0" while a bar is still forming."""
    return [_candle(row, timeframe) for row in rows if str(row[8]) == "1"]


def _decimal_text(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return text if text not in {"-0", ""} else "0"


def _precision(increment: str) -> int:
    exponent = Decimal(increment).normalize().as_tuple().exponent
    return max(0, -int(exponent))


class OkxSwapProvider:
    """Fetch an explicit allowlist of OKX USDT-margined perpetual swaps."""

    def __init__(
        self,
        timeout: float = 30,
        transport: httpx.AsyncBaseTransport | None = None,
        allowed_symbols: set[str] | frozenset[str] | None = None,
        base_url: str = OKX_BASE_URL,
        source_mode: str = SOURCE_MODE,
    ) -> None:
        self._timeout = timeout
        self._source_mode = source_mode
        self._transport = transport
        self._allowed_symbols = frozenset(allowed_symbols or DEFAULT_ALLOWED_SYMBOLS)
        self._base_url = base_url.rstrip("/")
        self.last_raw_response: dict[str, Any] | None = None
        # One kept-alive client per event loop. From this Mac the OKX TLS handshake
        # alone takes ~1.3s and a reused connection answers in ~0.5s; the Dashboard
        # bounds chart reads at 2s, so a fresh connection per request misses it.
        self._shared_client: httpx.AsyncClient | None = None
        self._shared_loop: asyncio.AbstractEventLoop | None = None
        self.timeframe_transform: TimeframeTransform | None = None
        self.source_identity: dict[str, Any] = {}

    def supported_timeframes(self) -> list[Timeframe]:
        return list(_TF_MAP.keys())

    def _new_client(self) -> httpx.AsyncClient:
        kwargs: dict[str, Any] = {
            "timeout": self._timeout,
            "base_url": self._base_url,
            "limits": httpx.Limits(max_keepalive_connections=4, keepalive_expiry=60),
        }
        if self._transport is not None:
            kwargs["transport"] = self._transport
        return httpx.AsyncClient(**kwargs)

    @contextlib.asynccontextmanager
    async def _client(self) -> AsyncIterator[httpx.AsyncClient]:
        """Yield the shared client for the running loop; never close it per request."""

        loop = asyncio.get_running_loop()
        client = self._shared_client
        if client is None or client.is_closed or self._shared_loop is not loop:
            client = self._new_client()
            self._shared_client, self._shared_loop = client, loop
        yield client

    async def aclose(self) -> None:
        if self._shared_client is not None and not self._shared_client.is_closed:
            await self._shared_client.aclose()
        self._shared_client = None
        self._shared_loop = None

    async def _get(self, client: httpx.AsyncClient, path: str, params: dict[str, Any]) -> list[Any]:
        try:
            response = await client.get(path, params=params)
        except httpx.RequestError as error:
            self.last_raw_response = {
                "request_params": params,
                "response_body": None,
                "status_code": None,
                "error": str(error),
            }
            raise ProviderError(
                f"OKX request failed: {error}",
                suggestions=["Check network access to www.okx.com"],
            ) from error
        try:
            payload = response.json()
        except ValueError:
            payload = response.text
        if response.status_code >= 400:
            raise ProviderError(f"OKX API error {response.status_code}: {payload}")
        if not isinstance(payload, dict) or str(payload.get("code")) != "0":
            message = payload.get("msg") if isinstance(payload, dict) else payload
            raise ProviderError(f"OKX API rejected the request: {message}")
        data = payload.get("data")
        if not isinstance(data, list):
            raise ProviderError("OKX returned a non-list data payload")
        return data

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
        bar = _TF_MAP.get(timeframe)
        if bar is None:
            raise ProviderError(
                f"Timeframe {timeframe.value} not supported for OKX swaps",
                suggestions=[f"Supported: {[t.value for t in self.supported_timeframes()]}"],
            )
        symbol = normalize_okx_symbol(ticker, self._allowed_symbols)
        self.source_identity = {"provider_symbol": symbol, "contract_type": "perpetual", "venue": "OKX"}
        self.timeframe_transform = TimeframeTransform(
            raw_timeframe=timeframe,
            timeframe_origin="native",
            aggregation={"kind": "none", "rule": "native_passthrough"},
        )

        requested = max(1, int(limit))
        start_ms = _to_millis(start) if start else None
        end_ms = _to_millis(end) if end else None
        rows: dict[int, list[Any]] = {}
        cursor: int | None = end_ms + 1 if end_ms is not None else None
        pages = 0
        async with self._client() as client:
            path = OKX_CANDLES_PATH
            page_size = _RECENT_PAGE
            while pages < _MAX_PAGES:
                params: dict[str, Any] = {"instId": symbol, "bar": bar, "limit": page_size}
                if cursor is not None:
                    params["after"] = cursor
                data = await self._get(client, path, params)
                pages += 1
                if not data and path == OKX_CANDLES_PATH:
                    # Past the recent window: continue from the history endpoint.
                    path, page_size = OKX_HISTORY_CANDLES_PATH, _HISTORY_PAGE
                    continue
                if not data:
                    break
                for row in data:
                    rows[int(row[0])] = row
                oldest = min(int(row[0]) for row in data)
                in_window = [ts for ts in rows if (start_ms is None or ts >= start_ms) and (end_ms is None or ts <= end_ms)]
                if start_ms is not None and oldest <= start_ms:
                    break
                if start_ms is None and len(in_window) >= requested:
                    break
                if cursor is not None and oldest >= cursor:
                    break
                cursor = oldest
                if len(data) < page_size and path == OKX_CANDLES_PATH:
                    path, page_size = OKX_HISTORY_CANDLES_PATH, _HISTORY_PAGE

        ordered = [
            rows[ts]
            for ts in sorted(rows)
            if (start_ms is None or ts >= start_ms) and (end_ms is None or ts <= end_ms)
        ]
        # With a start bound keep the first `limit` bars (forward window, like Binance);
        # without one keep the most recent `limit` bars.
        ordered = ordered[:requested] if start_ms is not None else ordered[-requested:]
        self.last_raw_response = {
            "request_params": {"instId": symbol, "bar": bar, "start": start, "end": end, "limit": limit},
            "response_body": ordered,
            "status_code": 200,
            "error": None,
            "page_count": pages,
        }
        if not ordered:
            raise ProviderError(f"No OKX candles returned for {symbol} {timeframe.value}")
        candles = [_candle(row, timeframe) for row in ordered]
        logger.info("Fetched %s OKX swap candles for %s", len(candles), symbol)
        return candles

    async def history_page(self, ticker: str, timeframe: Timeframe, *, after_ms: int) -> list[list[Any]]:
        """One page of history candles older than ``after_ms``, newest first, as OKX sent it."""
        symbol = normalize_okx_symbol(ticker, self._allowed_symbols)
        params = {"instId": symbol, "bar": _TF_MAP[timeframe], "limit": _HISTORY_PAGE, "after": after_ms}
        async with self._client() as client:
            return await self._get(client, OKX_HISTORY_CANDLES_PATH, params)

    async def funding_page(self, ticker: str, *, after_ms: int | None = None) -> list[dict[str, Any]]:
        """One page of funding settlements older than ``after_ms``, newest first, as OKX sent it."""
        symbol = normalize_okx_symbol(ticker, self._allowed_symbols)
        params: dict[str, Any] = {"instId": symbol, "limit": _HISTORY_PAGE}
        if after_ms is not None:
            params["after"] = after_ms
        async with self._client() as client:
            return await self._get(client, OKX_FUNDING_HISTORY_PATH, params)

    async def fetch_instrument_definition(self, ticker: str) -> InstrumentDefinition:
        symbol = normalize_okx_symbol(ticker, self._allowed_symbols)
        params = {"instType": "SWAP", "instId": symbol}
        family = str(symbol).removesuffix("-SWAP")
        tier_params = {"instType": "SWAP", "tdMode": "cross", "instFamily": family}
        async with self._client() as client:
            # Three independent public reads at once: one OKX round trip, not three (~1.5 s, #210).
            data, tiers, server_time = await asyncio.gather(
                self._get(client, OKX_INSTRUMENTS_PATH, params),
                self._get(client, OKX_POSITION_TIERS_PATH, tier_params),
                self._get(client, OKX_TIME_PATH, {}),
            )
        self.last_raw_response = {
            "request_params": params,
            "response_body": data,
            "status_code": 200,
            "error": None,
        }
        item = next((row for row in data if row.get("instId") == symbol), None)
        if item is None:
            raise ProviderError(f"OKX instruments did not include {symbol}")
        if str(item.get("ctType")) != "linear":
            raise ProviderError(f"OKX {symbol} is not a linear contract; only linear swaps are supported")

        contract_value = Decimal(str(item["ctVal"])) * Decimal(str(item.get("ctMult") or "1"))
        lot = Decimal(str(item["lotSz"]))
        size_increment = lot * contract_value
        min_quantity = Decimal(str(item["minSz"])) * contract_value
        max_limit = Decimal(str(item.get("maxLmtSz") or "0")) * contract_value
        max_market = Decimal(str(item.get("maxMktSz") or item.get("maxLmtSz") or "0")) * contract_value
        tick = str(item["tickSz"])
        base = str(item.get("ctValCcy") or symbol.split("-")[0])
        settle = str(item.get("settleCcy") or "USDT")
        leverage = Decimal(str(item.get("lever") or "0"))
        init_rate = _decimal_text(Decimal(1) / leverage) if leverage > 0 else "0"
        first_tier = next((row for row in tiers if str(row.get("tier")) == "1"), None)
        if first_tier is None or not str(first_tier.get("mmr") or "").strip():
            raise ProviderError(f"OKX position tiers did not include tier 1 for {family}")
        maint_rate = _decimal_text(Decimal(str(first_tier["mmr"])))
        try:
            upstream_ms = int(str(server_time[0]["ts"]))
        except (IndexError, KeyError, TypeError, ValueError) as error:
            raise ProviderError("OKX public time returned no timestamp") from error

        return InstrumentDefinition(
            instrument_id=f"{symbol}.OKX",
            venue="OKX",
            symbol=symbol,
            asset_class=AssetClass.COMMODITY if base in _COMMODITY_BASES else AssetClass.CRYPTO,
            market_type="usdt_perpetual_swap",
            contract_type="PERPETUAL",
            status="TRADING" if str(item.get("state")) == "live" else str(item.get("state") or "UNKNOWN").upper(),
            base_currency=base,
            quote_currency=settle,
            settlement_currency=settle,
            margin_currency=settle,
            is_inverse=False,
            price_precision=_precision(tick),
            price_increment=tick,
            min_price=tick,
            max_price=None,
            size_precision=_precision(_decimal_text(size_increment)),
            size_increment=_decimal_text(size_increment),
            min_quantity=_decimal_text(min_quantity),
            max_quantity=_decimal_text(max_limit),
            market_size_increment=_decimal_text(size_increment),
            market_min_quantity=_decimal_text(min_quantity),
            market_max_quantity=_decimal_text(max_market),
            min_notional=None,
            margin_init_rate=init_rate,
            margin_maint_rate=maint_rate,
            contract_multiplier="1",
            order_types=["LIMIT", "MARKET", "POST_ONLY", "IOC", "FOK"],
            time_in_force=["GTC", "IOC", "FOK"],
            provider=self._source_mode,
            source_mode=self._source_mode,
            execution_venue=True,
            observed_at=datetime.now(timezone.utc).isoformat(),
            upstream_server_time=upstream_ms,
            missing_fields=["maker_fee_rate", "taker_fee_rate"],
            derived_fields={
                "size_increment": "lotSz * ctVal * ctMult (contracts converted to base units)",
                "min_quantity": "minSz * ctVal * ctMult",
                "max_quantity": "maxLmtSz * ctVal * ctMult",
                "margin_init_rate": "1 / max leverage",
                "margin_maint_rate": "position-tiers tier 1 mmr (cross)",
                # Same meaning as Binance USD-M: quantity is in base units, so
                # notional = price * quantity.
                "contract_multiplier": "usd_m_notional_equals_price_times_quantity",
            },
            not_applicable_fields={
                "min_notional": "OKX has no minimum notional; the minimum order is minSz contracts (min_quantity)",
                "max_price": "OKX has no static maximum price; its price limit is a moving band",
            },
        )
