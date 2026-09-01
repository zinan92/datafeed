"""Source-agnostic authorized US equities adapter for the MVP.

The concrete client is injected so the MVP can use a licensed Alpaca/Massive
client later without coupling the manifest to an unverified public endpoint.
Yahoo/yfinance is intentionally not used by this adapter.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from hashlib import sha256
import inspect
import json
import math
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol
from zoneinfo import ZoneInfo

from kline.market_calendar import (
    aggregate_15m_to_1h,
    aggregate_15m_to_4h,
    aggregate_daily_to_weekly,
)
from kline.models import Candle, Timeframe, TimeframeTransform
from kline.mvp_manifest import MvpManifest, load_manifest
from kline.providers.base import EntitlementBlocked, ProviderError
from kline.storage import CandleSeriesKey, EntitlementReceiptWrite, MvpCandle


MVP_US_TIMEFRAMES = (
    Timeframe.MIN_15,
    Timeframe.HOUR_1,
    Timeframe.HOUR_4,
    Timeframe.DAY,
    Timeframe.WEEK,
)
_US_ZONE = ZoneInfo("America/New_York")


class AuthorizedUSClient(Protocol):
    """Client seam implemented by a provider with written data rights."""

    def fetch_bars(
        self,
        provider_symbol: str,
        timeframe: str,
        *,
        start: str | None,
        end: str | None,
        limit: int,
    ) -> Any: ...

    def fetch_corporate_actions(
        self,
        provider_symbol: str,
        *,
        start: str | None,
        end: str | None,
    ) -> Any: ...


@dataclass(frozen=True)
class USDataEntitlement:
    """Operator-provided permission receipt; credentials never live here."""

    source_id: str = "us_authorized_pending"
    status: str = "active"
    allowed_timeframes: tuple[str, ...] = ("15m", "1h", "4h", "1d", "1w")
    persistence_allowed: bool = False
    derived_allowed: bool = False
    non_display_allowed: bool = False
    corporate_actions_allowed: bool = False
    valid_from: str | None = None
    valid_to: str | None = None
    evidence_ref: str = ""
    receipt_hash: str = ""
    allowed_history: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "allowed_timeframes", tuple(self.allowed_timeframes))

    @classmethod
    def from_json_file(cls, path: str | Path) -> "USDataEntitlement":
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ProviderError("US entitlement receipt file was not found") from exc
        except json.JSONDecodeError as exc:
            raise ProviderError("US entitlement receipt file is invalid JSON") from exc
        if not isinstance(payload, Mapping):
            raise ProviderError("US entitlement receipt must be an object")
        try:
            return cls(**dict(payload))
        except TypeError as exc:
            raise ProviderError("US entitlement receipt has invalid fields") from exc

    def permits(self, timeframe: Timeframe, *, now: datetime) -> bool:
        if self.status != "active" or timeframe.value not in set(self.allowed_timeframes):
            return False
        try:
            if self.valid_from and now.date() < date.fromisoformat(self.valid_from[:10]):
                return False
            if self.valid_to and now.date() > date.fromisoformat(self.valid_to[:10]):
                return False
        except ValueError:
            return False
        return True

    def as_receipt(self) -> EntitlementReceiptWrite:
        if not self.evidence_ref:
            raise ProviderError("US entitlement evidence reference is required")
        if len(self.receipt_hash) != 64 or any(
            char not in "0123456789abcdefABCDEF" for char in self.receipt_hash
        ):
            raise ProviderError("US entitlement receipt hash is required")
        return EntitlementReceiptWrite(
            receipt_id=f"{self.source_id}:{self.receipt_hash[:16]}",
            source_id=self.source_id,
            status=self.status,
            allowed_history=dict(self.allowed_history),
            timeframe_permissions=self.allowed_timeframes,
            persistence_allowed=self.persistence_allowed,
            derived_allowed=self.derived_allowed,
            non_display_allowed=self.non_display_allowed,
            valid_from=self.valid_from,
            valid_to=self.valid_to,
            evidence_ref=self.evidence_ref,
            receipt_hash=self.receipt_hash,
        )


class AuthorizedUSProvider:
    """Fetch exact manifest members through an explicitly authorized client."""

    def __init__(
        self,
        token: str = "",
        *,
        entitlement: USDataEntitlement | None = None,
        client: AuthorizedUSClient | None = None,
        manifest: MvpManifest | str | Path | None = None,
        source_id: str = "us_authorized_pending",
        clock: Callable[[], datetime] | None = None,
        max_retries: int = 2,
        retry_delay: float = 0.0,
    ) -> None:
        self._token = token
        self._entitlement = entitlement
        self._client = client
        self._source_id = source_id
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._max_retries = max(0, max_retries)
        self._retry_delay = max(0.0, retry_delay)
        if isinstance(manifest, (str, Path)):
            self._manifest = load_manifest(manifest)
        else:
            self._manifest = manifest
        self._members = {
            item.display_symbol: item
            for item in (self._manifest.instruments if self._manifest else ())
            if item.universe == "us_stock"
        }
        self._aliases = {
            alias.casefold(): item
            for item in self._members.values()
            for alias in item.ticker_aliases
        }
        self.last_raw_response: dict[str, Any] | None = None
        self.timeframe_transform: TimeframeTransform | None = None
        self.source_identity: dict[str, Any] = {}

    def supported_timeframes(self) -> list[Timeframe]:
        if (
            not self._token
            or self._entitlement is None
            or not self._entitlement.persistence_allowed
            or not self._entitlement.non_display_allowed
        ):
            return []
        now = self._clock()
        return [
            timeframe
            for timeframe in MVP_US_TIMEFRAMES
            if self._permission_allows(timeframe, now=now)
        ]

    def membership_report(self) -> dict[str, Any]:
        return {
            "manifest_version": self._manifest.version if self._manifest else None,
            "expected_members": 100,
            "mapped_members": len(self._members),
            "complete": len(self._members) == 100,
            "source_id": self._source_id,
        }

    async def fetch(
        self,
        ticker: str,
        timeframe: Timeframe,
        *,
        start: str | None = None,
        end: str | None = None,
        limit: int = 500,
    ) -> list[Candle]:
        instrument, alias_used = self._resolve_member(ticker, start=start, end=end)
        if timeframe == Timeframe.HOUR_1 and not self._native_permission_allows(Timeframe.HOUR_1):
            if self._derived_1h_permission_allows():
                return await self._fetch_derived_1h(
                    instrument,
                    alias_used=alias_used,
                    start=start,
                    end=end,
                    limit=limit,
                )
        self._require_entitlement(timeframe)
        if timeframe in {Timeframe.HOUR_4, Timeframe.WEEK}:
            if self._entitlement is None or not self._entitlement.derived_allowed:
                raise EntitlementBlocked(
                    "US derived timeframe persistence is blocked_for_entitlement"
                )
        if timeframe == Timeframe.HOUR_4:
            raw = await self.fetch(
                instrument.display_symbol,
                Timeframe.MIN_15,
                start=start,
                end=end,
                limit=max(limit * 26, 300),
            )
            key = self._key(instrument, Timeframe.MIN_15)
            result = aggregate_15m_to_4h(
                [self._to_mvp(key, candle) for candle in raw],
                calendar_id="us_equities",
                cutoff=self._clock(),
                run_id=f"us-preview:{instrument.instrument_id}",
            )
            self._set_transform(
                Timeframe.MIN_15,
                result.transform_receipt.aggregation_rule_version
                if result.transform_receipt
                else "us_regular_fixed_4h_v1",
                len(result.partial_buckets),
            )
            self.source_identity["alias_used"] = alias_used
            return [self._from_mvp(row) for row in result.candles[-limit:]]
        if timeframe == Timeframe.WEEK:
            raw = await self.fetch(
                instrument.display_symbol,
                Timeframe.DAY,
                start=start,
                end=end,
                limit=max(limit * 7 + 10, 200),
            )
            key = self._key(instrument, Timeframe.DAY)
            result = aggregate_daily_to_weekly(
                [self._to_mvp(key, candle) for candle in raw],
                calendar_id="us_equities",
                cutoff=self._clock(),
                run_id=f"us-preview:{instrument.instrument_id}",
            )
            self._set_transform(
                Timeframe.DAY,
                result.transform_receipt.aggregation_rule_version
                if result.transform_receipt
                else "completed_local_calendar_week_v1",
                len(result.partial_buckets),
            )
            self.source_identity["alias_used"] = alias_used
            return [self._from_mvp(row) for row in result.candles[-limit:]]
        rows = await self._fetch_native(instrument, timeframe, start=start, end=end, limit=limit)
        self.source_identity["alias_used"] = alias_used
        return rows

    async def _fetch_derived_1h(
        self,
        instrument: Any,
        *,
        alias_used: str | None,
        start: str | None,
        end: str | None,
        limit: int,
    ) -> list[Candle]:
        raw = await self.fetch(
            instrument.display_symbol,
            Timeframe.MIN_15,
            start=start,
            end=end,
            limit=max(limit * 4, 200),
        )
        key = self._key(instrument, Timeframe.MIN_15)
        result = aggregate_15m_to_1h(
            [self._to_mvp(key, candle) for candle in raw],
            calendar_id="us_equities",
            cutoff=self._clock(),
            run_id=f"us-preview:{instrument.instrument_id}:1h",
        )
        if not result.candles:
            raise ProviderError(
                f"authorized US returned no complete 1h rows for {instrument.display_symbol}"
            )
        self._set_transform(
            Timeframe.MIN_15,
            "us_regular_fixed_1h_v1",
            len(result.partial_buckets),
        )
        self.source_identity["alias_used"] = alias_used
        self.source_identity["timeframe_origin"] = "aggregated"
        self.last_raw_response = dict(self.last_raw_response or {})
        self.last_raw_response["requested_timeframe"] = Timeframe.HOUR_1.value
        return [self._from_mvp(row) for row in result.candles[-limit:]]

    def _native_permission_allows(self, timeframe: Timeframe) -> bool:
        return bool(
            self._token
            and self._entitlement is not None
            and self._entitlement.permits(timeframe, now=self._clock())
            and self._entitlement.persistence_allowed
            and self._entitlement.non_display_allowed
        )

    def _derived_1h_permission_allows(self) -> bool:
        return bool(
            self._token
            and self._entitlement is not None
            and self._entitlement.permits(Timeframe.MIN_15, now=self._clock())
            and self._entitlement.derived_allowed
            and self._entitlement.persistence_allowed
            and self._entitlement.non_display_allowed
        )

    def _permission_allows(self, timeframe: Timeframe, *, now: datetime) -> bool:
        if (
            self._entitlement is None
            or not self._token
            or not self._entitlement.persistence_allowed
            or not self._entitlement.non_display_allowed
        ):
            return False
        if timeframe == Timeframe.HOUR_1:
            return self._entitlement.permits(timeframe, now=now) or (
                self._entitlement.permits(Timeframe.MIN_15, now=now)
                and self._entitlement.derived_allowed
            )
        return self._entitlement.permits(timeframe, now=now) and (
            timeframe not in {Timeframe.HOUR_4, Timeframe.WEEK} or self._entitlement.derived_allowed
        )

    async def fetch_corporate_actions(
        self,
        ticker: str,
        *,
        start: str | None = None,
        end: str | None = None,
    ) -> Any:
        instrument, _ = self._resolve_member(ticker, start=start, end=end)
        if (
            not self._token
            or self._entitlement is None
            or not self._entitlement.corporate_actions_allowed
            or not self._entitlement.persistence_allowed
        ):
            raise EntitlementBlocked("US corporate actions are blocked_for_entitlement")
        client = self._get_client()
        return await self._call(
            lambda: client.fetch_corporate_actions(
                instrument.provider_symbol,
                start=start,
                end=end,
            )
        )

    async def _fetch_native(
        self,
        instrument: Any,
        timeframe: Timeframe,
        *,
        start: str | None,
        end: str | None,
        limit: int,
    ) -> list[Candle]:
        if timeframe not in {Timeframe.MIN_15, Timeframe.HOUR_1, Timeframe.DAY}:
            raise ProviderError(f"US native timeframe {timeframe.value} is unsupported")
        client = self._get_client()
        request_params = {
            "provider_symbol": instrument.provider_symbol,
            "timeframe": timeframe.value,
            "start": start,
            "end": end,
            "limit": limit,
            "adjustment": "raw_unadjusted",
        }
        self.last_raw_response = {
            "request_params": request_params,
            "response_body": None,
            "status_code": None,
            "error": None,
        }
        try:
            payload = await self._call(
                lambda: client.fetch_bars(
                    instrument.provider_symbol,
                    timeframe.value,
                    start=start,
                    end=end,
                    limit=limit,
                )
            )
        except ProviderError:
            raise
        except Exception as exc:
            failure = ProviderError(f"authorized US request failed for {instrument.display_symbol}")
            failure.code = str((self.last_raw_response or {}).get("error") or "provider_error")
            raise failure from exc
        rows = self._parse_rows(payload, instrument=instrument, timeframe=timeframe, limit=limit)
        if not rows:
            self.last_raw_response["error"] = "empty_window"
            failure = ProviderError(
                f"authorized US returned no closed rows for {instrument.display_symbol}"
            )
            failure.code = "market_closed"
            raise failure
        self.last_raw_response["response_body"] = {
            "row_count": len(rows),
            "sha256": self._payload_hash(payload),
        }
        self.timeframe_transform = TimeframeTransform(
            raw_timeframe=timeframe,
            timeframe_origin="native",
            aggregation={"kind": "none", "rule": "native_passthrough"},
        )
        self.source_identity = {
            "source_id": self._source_id,
            "provider_symbol": instrument.provider_symbol,
            "instrument_id": instrument.instrument_id,
            "manifest_version": self._manifest.version if self._manifest else None,
            "exchange": instrument.venue,
            "session_policy": instrument.session_policy,
            "adjustment_basis": "raw_unadjusted",
            "share_class": instrument.share_class,
            "security_type": instrument.security_type,
            "adr_ratio": instrument.adr_ratio,
            "venue_valid_from": instrument.venue_valid_from,
            "venue_valid_to": instrument.venue_valid_to,
            "entitlement_status": self._entitlement.status if self._entitlement else "blocked",
        }
        return rows

    def _get_client(self) -> AuthorizedUSClient:
        if self._client is None:
            raise EntitlementBlocked("authorized US client is not configured")
        return self._client

    async def _call(self, operation: Callable[[], Any]) -> Any:
        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                result = operation()
                if inspect.isawaitable(result):
                    return await result
                return result
            except Exception as exc:
                last_error = exc
                if attempt >= self._max_retries:
                    text = str(exc).lower()
                    self.last_raw_response = dict(self.last_raw_response or {})
                    self.last_raw_response["error"] = (
                        "rate_limited"
                        if "429" in text or "rate limit" in text
                        else "provider_error"
                    )
                    raise
                if self._retry_delay:
                    await asyncio.sleep(self._retry_delay * (attempt + 1))
        assert last_error is not None
        raise last_error

    def _require_entitlement(self, timeframe: Timeframe) -> None:
        if not self._token or self._entitlement is None:
            raise EntitlementBlocked("authorized US source is blocked_for_entitlement")
        if not self._entitlement.permits(timeframe, now=self._clock()):
            raise EntitlementBlocked(f"authorized US {timeframe.value} is blocked_for_entitlement")
        if not self._entitlement.persistence_allowed or not self._entitlement.non_display_allowed:
            raise EntitlementBlocked(
                "authorized US persistence/non-display use is blocked_for_entitlement"
            )

    def _resolve_member(
        self, ticker: str, *, start: str | None, end: str | None
    ) -> tuple[Any, str | None]:
        symbol = ticker.strip()
        member = self._members.get(symbol) or next(
            (
                item
                for display, item in self._members.items()
                if display.casefold() == symbol.casefold()
            ),
            None,
        )
        if member is not None:
            return member, None
        if symbol.casefold() in self._aliases:
            item = self._aliases[symbol.casefold()]
            validity = next(
                (
                    value
                    for alias, value in item.ticker_alias_validity.items()
                    if alias.casefold() == symbol.casefold()
                ),
                {},
            )
            valid_to = validity.get("valid_to") if isinstance(validity, Mapping) else None
            valid_from = validity.get("valid_from") if isinstance(validity, Mapping) else None
            window_start = start[:10] if start else None
            window_end = end[:10] if end else self._clock().date().isoformat()
            if (valid_to and window_end > valid_to[:10]) or (
                valid_from and window_start and window_start < valid_from[:10]
            ):
                raise ProviderError(f"ticker alias {ticker} is not valid for requested window")
            return item, symbol
        for item in self._members.values():
            if item.provider_symbol.upper() == symbol.upper():
                return item, None
        if self._manifest is not None:
            raise ProviderError(f"US ticker {ticker} is not in the MVP manifest")
        raise ProviderError("authorized US provider requires a manifest")

    def _key(self, instrument: Any, timeframe: Timeframe) -> CandleSeriesKey:
        return CandleSeriesKey(
            instrument_id=instrument.instrument_id,
            display_symbol=instrument.display_symbol,
            provider_symbol=instrument.provider_symbol,
            source_id=self._source_id,
            asset_class="us_stock",
            timeframe=timeframe,
            adjustment_basis="raw_unadjusted",
            manifest_version=self._manifest.version if self._manifest else "mvp_universe_v1",
        )

    @staticmethod
    def _to_mvp(key: CandleSeriesKey, candle: Candle) -> MvpCandle:
        return MvpCandle(
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

    @staticmethod
    def _from_mvp(candle: MvpCandle) -> Candle:
        return Candle(
            timestamp=candle.timestamp,
            open=candle.open,
            high=candle.high,
            low=candle.low,
            close=candle.close,
            volume=candle.volume or 0,
            amount=candle.amount,
        )

    def _set_transform(self, raw_timeframe: Timeframe, rule: str, partial_count: int) -> None:
        self.timeframe_transform = TimeframeTransform(
            raw_timeframe=raw_timeframe,
            timeframe_origin="aggregated",
            aggregation={
                "rule": rule,
                "partial_bucket_policy": "drop_and_record",
                "partial_bucket_count": partial_count,
            },
        )

    def _parse_rows(
        self, payload: Any, *, instrument: Any, timeframe: Timeframe, limit: int
    ) -> list[Candle]:
        if payload is None:
            return []
        if isinstance(payload, Mapping):
            rows = payload.get("bars", payload.get("data", []))
        else:
            rows = payload
        if not isinstance(rows, list):
            raise ProviderError("authorized US returned malformed bar payload")
        output: list[Candle] = []
        now = self._clock().astimezone(timezone.utc)
        for raw in rows:
            if not isinstance(raw, Mapping):
                raise ProviderError("authorized US returned malformed bar row")
            try:
                timestamp = raw.get("timestamp", raw.get("t", raw.get("time")))
                stamp = self._parse_timestamp(timestamp, timeframe)
                local = stamp.astimezone(_US_ZONE)
                if timeframe in {Timeframe.MIN_15, Timeframe.HOUR_1}:
                    interval = (
                        timedelta(minutes=15)
                        if timeframe == Timeframe.MIN_15
                        else timedelta(hours=1)
                    )
                    if stamp + interval > now:
                        continue
                else:
                    close = datetime.combine(local.date(), time(16, 0), tzinfo=_US_ZONE).astimezone(
                        timezone.utc
                    )
                    if close > now:
                        continue
                adjustment = raw.get("adjustment_basis", raw.get("adjustment"))
                if raw.get("adjusted") is True or (
                    adjustment is not None and str(adjustment) != "raw_unadjusted"
                ):
                    raise ValueError("adjusted row is not allowed")
                values = [
                    float(self._raw_value(raw, name, short))
                    for name, short in (("open", "o"), ("high", "h"), ("low", "l"), ("close", "c"))
                ]
                volume = float(self._raw_value(raw, "volume", "v"))
                amount = self._raw_value(raw, "amount", "nvalue", default=None)
                amount_value = float(amount) if amount is not None else None
                if not all(math.isfinite(value) for value in (*values, volume)) or volume < 0:
                    raise ValueError("non-finite OHLCV")
                if values[1] < max(values[0], values[3]) or values[2] > min(values[0], values[3]):
                    raise ValueError("OHLC invariant")
                output.append(
                    Candle(
                        timestamp=stamp.isoformat(),
                        open=values[0],
                        high=values[1],
                        low=values[2],
                        close=values[3],
                        volume=volume,
                        amount=amount_value,
                    )
                )
            except (KeyError, TypeError, ValueError, OverflowError) as exc:
                self.last_raw_response["error"] = "malformed_row"
                failure = ProviderError(f"authorized US returned malformed {timeframe.value} row")
                failure.code = "malformed_row"
                raise failure from exc
        return sorted(output, key=lambda candle: candle.timestamp)[-limit:] if limit else output

    @staticmethod
    def _parse_timestamp(value: Any, timeframe: Timeframe) -> datetime:
        if value is None:
            raise ValueError("missing timestamp")
        if isinstance(value, (int, float)):
            seconds = float(value) / (1000 if float(value) > 10_000_000_000 else 1)
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        text = str(value).strip().replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(
                tzinfo=_US_ZONE
                if timeframe in {Timeframe.MIN_15, Timeframe.HOUR_1}
                else timezone.utc
            )
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _raw_value(raw: Mapping[str, Any], *names: str, default: Any = ...) -> Any:
        for name in names:
            if name in raw and raw[name] is not None:
                return raw[name]
        if default is not ...:
            return default
        raise KeyError(names[0])

    @staticmethod
    def _payload_hash(payload: Any) -> str:
        try:
            encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        except (TypeError, ValueError):
            encoded = repr(payload)
        return sha256(encoded.encode("utf-8")).hexdigest()
