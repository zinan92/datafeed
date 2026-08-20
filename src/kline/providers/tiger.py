"""Tiger OpenAPI market-data adapter (quote endpoints only, never execution)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

from kline.models import (
    AssetClass,
    Candle,
    InstrumentDefinition,
    Timeframe,
    TimeframeTransform,
)
from kline.ports import MarketDataPort, ProviderMeta, SourceManifest
from kline.providers.base import ProviderError


class TigerOpenFuturesAdapter(MarketDataPort):
    def __init__(self, config: dict[str, Any], quote_client: Any | None = None) -> None:
        self.config = config
        self._quote_client = quote_client
        self._injected_quote_client = quote_client is not None
        source_id = str(config.get("source_id") or "tiger_openapi_comex")
        contract = str(config.get("contract") or "MGCmain")
        aliases = {
            "MGC": contract,
            "MGCMAIN": contract,
            contract.upper(): contract,
        }
        self._manifest = SourceManifest(
            source_id=source_id,
            asset_class=AssetClass.COMMODITY,
            meta=ProviderMeta(
                name="tiger_openapi",
                source_mode=source_id,
                quality_flags=(
                    "official_broker_feed",
                    "execution_venue_feed",
                    "exchange_futures",
                    "tiger_openapi",
                    str(config.get("exchange") or "COMEX").lower(),
                ),
                continuous=False,
                execution_venue=True,
                realtime_supported=False,
                market_type="broker_futures",
                supported_symbols=(contract,),
            ),
            ticker_aliases=aliases,
            canonical_instrument_ids={key: "MGC_CONTINUOUS" for key in aliases},
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
        return [Timeframe.MIN_1, Timeframe.MIN_5, Timeframe.DAY]

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
            raise ProviderError(
                f"Tiger futures timeframe is unsupported: {timeframe.value}",
                suggestions=["Use 1m, 5m, or 1d"],
            )
        contract = self.canonical_ticker(ticker)
        client = self._client()
        begin_ms = self._to_ms(start) if start else -1
        end_ms = self._to_ms(end) if end else -1
        try:
            payload = client.get_future_bars(
                [contract],
                period=self._period(timeframe),
                begin_time=begin_ms,
                end_time=end_ms,
                limit=limit,
            )
        except Exception as error:
            self._last_raw_response = {
                "request_params": {
                    "contract": contract,
                    "period": timeframe.value,
                    "begin_time": begin_ms,
                    "end_time": end_ms,
                    "limit": limit,
                },
                "response_body": None,
                "status_code": None,
                "error": f"{type(error).__name__}: {error}",
            }
            raise ProviderError(f"Tiger OpenAPI futures request failed: {error}") from error

        rows = self._payload_rows(payload)
        self._last_raw_response = {
            "request_params": {
                "contract": contract,
                "period": timeframe.value,
                "begin_time": begin_ms,
                "end_time": end_ms,
                "limit": limit,
            },
            "response_body": rows,
            "status_code": 200,
            "error": None,
        }
        unique = {candle.timestamp: candle for candle in (self._to_candle(row) for row in rows)}
        return [unique[key] for key in sorted(unique)][-limit:]

    async def stream_candles(
        self, ticker: str, timeframe: Timeframe
    ) -> AsyncIterator[Candle]:
        raise ProviderError(
            "Tiger OpenAPI streaming is not configured",
            suggestions=["Use REST polling until a quote push adapter is installed"],
        )
        if False:
            yield Candle(timestamp="", open=0, high=0, low=0, close=0, volume=0)

    async def fetch_instrument_definition(self, ticker: str) -> InstrumentDefinition:
        raise ProviderError(
            "Tiger instrument-definition-v1 mapping is not implemented",
            suggestions=["Use candle data only for this adapter"],
        )

    async def fetch_market_sessions(self, ticker: str, trading_date: str | None = None) -> dict[str, Any]:
        contract = self.canonical_ticker(ticker)
        try:
            payload = self._client().get_future_trading_times(contract, trading_date=trading_date)
        except Exception as error:
            raise ProviderError(f"Tiger trading-times request failed: {error}") from error
        windows = []
        for row in self._payload_rows(payload):
            if row.get("start") is None or row.get("end") is None:
                continue
            windows.append(
                {
                    "kind": "trading" if bool(row.get("trading")) else "bidding" if bool(row.get("bidding")) else "closed",
                    "trading": bool(row.get("trading")),
                    "bidding": bool(row.get("bidding")),
                    "start": self._timestamp(row["start"]),
                    "end": self._timestamp(row["end"]),
                    "zone": str(row.get("zone") or row.get("time_zone") or self.config.get("time_zone", "")),
                }
            )
        windows.sort(key=lambda item: item["start"])
        return {
            "schema_version": "market-sessions-v1",
            "source": self.manifest.source_id,
            "instrument_id": self.manifest.canonical_instrument_id(ticker),
            "provider_symbol": contract,
            "trading_date": trading_date or "",
            "timezone": next((item["zone"] for item in windows if item.get("zone")), ""),
            "windows": windows,
            "trading_windows": [item for item in windows if item["trading"]],
        }

    def _client(self):
        if self._quote_client is not None:
            return self._quote_client
        props_path = str(self.config.get("props_path") or "")
        if not props_path or not Path(props_path).exists():
            raise ProviderError("Tiger OpenAPI props_path is missing or does not exist")
        try:
            from tigeropen.quote.quote_client import QuoteClient
            from tigeropen.tiger_open_config import TigerOpenClientConfig
        except ImportError as error:
            raise ProviderError(
                "tigeropen is not installed",
                suggestions=["Install datafeed with the tiger optional dependency"],
            ) from error
        client_config = TigerOpenClientConfig(props_path=props_path)
        self._quote_client = QuoteClient(
            client_config,
            is_grab_permission=bool(self.config.get("grab_quote_permission", False)),
        )
        return self._quote_client

    def _period(self, timeframe: Timeframe):
        if self._injected_quote_client:
            return timeframe.value
        try:
            from tigeropen.common.consts import BarPeriod
        except ImportError as error:
            raise ProviderError("tigeropen is not installed") from error
        return {
            Timeframe.MIN_1: BarPeriod.ONE_MINUTE,
            Timeframe.MIN_5: BarPeriod.FIVE_MINUTES,
            Timeframe.DAY: BarPeriod.DAY,
        }[timeframe]

    @staticmethod
    def _payload_rows(payload: Any) -> list[dict[str, Any]]:
        if payload is None:
            return []
        if hasattr(payload, "to_dict"):
            try:
                return list(payload.to_dict("records"))
            except TypeError:
                pass
        items = payload if isinstance(payload, (list, tuple)) else [payload]
        rows = []
        for item in items:
            if isinstance(item, dict):
                rows.append(item)
            elif hasattr(item, "_asdict"):
                rows.append(dict(item._asdict()))
            elif hasattr(item, "__dict__"):
                rows.append(dict(item.__dict__))
            else:
                raise ProviderError(f"Unsupported Tiger response row: {type(item).__name__}")
        return rows

    @classmethod
    def _to_candle(cls, row: dict[str, Any]) -> Candle:
        timestamp = row.get("time") or row.get("timestamp") or row.get("latest_time")
        if timestamp is None:
            raise ProviderError("Tiger response row is missing a timestamp")
        if isinstance(timestamp, (int, float)) or str(timestamp).isdigit():
            parsed = datetime.fromtimestamp(float(timestamp) / 1000, tz=timezone.utc)
        else:
            parsed = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
        return Candle(
            timestamp=parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat(),
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=float(row.get("volume") or 0),
            amount=float(row["amount"]) if row.get("amount") is not None else None,
        )

    @staticmethod
    def _to_ms(value: str) -> int:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp() * 1000)

    @staticmethod
    def _timestamp(value: Any) -> str:
        if isinstance(value, (int, float)) or str(value).isdigit():
            parsed = datetime.fromtimestamp(float(value) / 1000, tz=timezone.utc)
        else:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def create_adapter(config: dict[str, Any]) -> TigerOpenFuturesAdapter:
    return TigerOpenFuturesAdapter(config)
