"""OANDA v20 pricing adapter; credentials stay inside datafeed."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, AsyncIterator

import httpx

from kline.models import (
    AssetClass,
    Candle,
    InstrumentDefinition,
    Timeframe,
    TimeframeTransform,
)
from kline.ports import MarketDataPort, ProviderMeta, SourceManifest
from kline.providers.base import ProviderError


class OandaV20Adapter(MarketDataPort):
    def __init__(self, config: dict[str, Any], transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.config = config
        self._transport = transport
        source_id = str(config.get("source_id") or "oanda_v20")
        instrument = str(config.get("instrument") or "XAU_USD")
        aliases = {"GOLD": instrument, "XAUUSD": instrument, instrument.upper(): instrument}
        self._manifest = SourceManifest(
            source_id=source_id,
            asset_class=AssetClass.COMMODITY,
            meta=ProviderMeta(
                name="oanda",
                source_mode=source_id,
                quality_flags=("official_broker_feed", "oanda_v20", "execution_venue"),
                continuous=False,
                execution_venue=True,
                realtime_supported=False,
                market_type="broker_spot_cfd",
                supported_symbols=(instrument,),
            ),
            ticker_aliases=aliases,
            canonical_instrument_ids={key: "GOLD" for key in aliases},
        )
        self._last_raw_response: dict[str, Any] | None = None

    @property
    def manifest(self) -> SourceManifest:
        return self._manifest

    @property
    def last_raw_response(self) -> dict[str, Any] | None:
        return self._last_raw_response

    @property
    def timeframe_transform(self) -> TimeframeTransform | None:
        return None

    @property
    def source_identity(self) -> dict[str, Any]:
        return {}

    def canonical_ticker(self, ticker: str) -> str:
        return self.manifest.canonical_ticker(ticker)

    def supported_timeframes(self) -> list[Timeframe]:
        return [Timeframe.MIN_1, Timeframe.MIN_5, Timeframe.MIN_15, Timeframe.HOUR_1, Timeframe.HOUR_4, Timeframe.DAY]

    async def fetch_candles(
        self,
        ticker: str,
        timeframe: Timeframe,
        *,
        start: str | None = None,
        end: str | None = None,
        limit: int = 500,
    ) -> list[Candle]:
        if timeframe not in self.supported_timeframes():
            raise ProviderError(f"OANDA timeframe unsupported: {timeframe.value}")
        token = str(self.config.get("token") or "")
        if not token:
            raise ProviderError("OANDA API token is missing")
        instrument = self.canonical_ticker(ticker)
        params: dict[str, Any] = {
            "price": str(self.config.get("price") or "M"),
            "granularity": self._granularity(timeframe),
            "count": min(max(1, limit), 5000),
        }
        if start:
            params["from"] = start
            params.pop("count", None)
        if end:
            params["to"] = end
        base_url = str(self.config.get("base_url") or "https://api-fxpractice.oanda.com").rstrip("/")
        url = f"{base_url}/v3/instruments/{instrument}/candles"
        try:
            async with httpx.AsyncClient(timeout=float(self.config.get("timeout_seconds", 15)), transport=self._transport) as client:
                response = await client.get(url, params=params, headers={"Authorization": f"Bearer {token}", "Accept-Datetime-Format": "RFC3339"})
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise ProviderError(f"OANDA request failed: {error}") from error
        self._last_raw_response = {"request_params": params, "response_body": payload, "status_code": response.status_code, "error": None}
        candles = []
        for item in payload.get("candles", []):
            if item.get("complete") is False:
                continue
            mid = item.get("mid") or {}
            if not all(key in mid for key in ("o", "h", "l", "c")):
                continue
            candles.append(Candle(timestamp=self._timestamp(str(item["time"])), open=float(mid["o"]), high=float(mid["h"]), low=float(mid["l"]), close=float(mid["c"]), volume=float(item.get("volume") or 0)))
        return candles[-limit:]

    async def stream_candles(self, ticker: str, timeframe: Timeframe) -> AsyncIterator[Candle]:
        raise ProviderError("OANDA candle streaming is not configured; use REST polling")
        if False:
            yield Candle(timestamp="", open=0, high=0, low=0, close=0, volume=0)

    async def fetch_instrument_definition(self, ticker: str) -> InstrumentDefinition:
        raise ProviderError("OANDA instrument definition requires an account-scoped endpoint")

    @staticmethod
    def _granularity(timeframe: Timeframe) -> str:
        return {Timeframe.MIN_1: "M1", Timeframe.MIN_5: "M5", Timeframe.MIN_15: "M15", Timeframe.HOUR_1: "H1", Timeframe.HOUR_4: "H4", Timeframe.DAY: "D"}[timeframe]

    @staticmethod
    def _timestamp(value: str) -> str:
        head, dot, tail = value.partition(".")
        if dot:
            zone = "Z" if tail.endswith("Z") else "+00:00" if "+" not in tail else "+" + tail.split("+", 1)[1]
            fraction = tail.rstrip("Z").split("+", 1)[0][:6]
            value = f"{head}.{fraction}{zone}"
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def create_adapter(config: dict[str, Any]) -> OandaV20Adapter:
    return OandaV20Adapter(config)
