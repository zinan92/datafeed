"""Entitlement-gated TuShare Pro adapter for the A-share MVP universe.

The adapter is deliberately inert without an operator-supplied entitlement.
It never falls back to Tencent/Sina/Yahoo and never includes the token in a
receipt or error message.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from hashlib import sha256
import json
import math
from pathlib import Path
from typing import Any, Callable, Mapping

import tushare as ts

from kline.market_calendar import aggregate_15m_to_4h, aggregate_daily_to_weekly
from kline.models import Candle, Timeframe, TimeframeTransform
from kline.mvp_manifest import MvpManifest, load_manifest
from kline.providers.ashare import _to_tushare_code
from kline.providers.base import EntitlementBlocked, ProviderError
from kline.storage import CandleSeriesKey, EntitlementReceiptWrite, MvpCandle


MVP_TUSHARE_TIMEFRAMES = (Timeframe.MIN_15, Timeframe.HOUR_4, Timeframe.DAY, Timeframe.WEEK)
_A_SHARE_CALENDAR = "cn_a"
_A_SHARE_ZONE = timezone(timedelta(hours=8))


@dataclass(frozen=True)
class TuShareEntitlement:
    """Operator-provided permission receipt; no token is stored here."""

    source_id: str = "tushare_pro"
    status: str = "active"
    allowed_timeframes: tuple[str, ...] = ("15m", "4h", "1d", "1w")
    persistence_allowed: bool = False
    derived_allowed: bool = False
    non_display_allowed: bool = False
    valid_from: str | None = None
    valid_to: str | None = None
    evidence_ref: str = ""
    receipt_hash: str = ""
    allowed_history: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "allowed_timeframes", tuple(self.allowed_timeframes))

    @classmethod
    def from_json_file(cls, path: str | Path) -> "TuShareEntitlement":
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ProviderError("TuShare entitlement receipt file was not found") from exc
        except json.JSONDecodeError as exc:
            raise ProviderError("TuShare entitlement receipt file is invalid JSON") from exc
        if not isinstance(payload, Mapping):
            raise ProviderError("TuShare entitlement receipt must be an object")
        try:
            return cls(**dict(payload))
        except TypeError as exc:
            raise ProviderError("TuShare entitlement receipt has invalid fields") from exc

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
        if not self.receipt_hash or len(self.receipt_hash) != 64:
            raise ProviderError("TuShare entitlement receipt hash is required")
        if not self.evidence_ref:
            raise ProviderError("TuShare entitlement evidence reference is required")
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


class TuShareMvpProvider:
    """Fetch the exact 100 A-share members through authorized TuShare APIs."""

    def __init__(
        self,
        token: str = "",
        *,
        entitlement: TuShareEntitlement | None = None,
        client: Any | None = None,
        manifest: MvpManifest | str | Path | None = None,
        clock: Callable[[], datetime] | None = None,
        max_retries: int = 2,
        retry_delay: float = 0.0,
    ) -> None:
        self._token = token
        self._entitlement = entitlement
        self._client = client
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
            if item.universe == "a_share"
        }
        self.last_raw_response: dict[str, Any] | None = None
        self.timeframe_transform: TimeframeTransform | None = None
        self.source_identity: dict[str, Any] = {}

    def supported_timeframes(self) -> list[Timeframe]:
        if (
            self._entitlement is None
            or not self._token
            or not self._entitlement.persistence_allowed
            or not self._entitlement.non_display_allowed
        ):
            return []
        now = self._clock()
        return [
            timeframe
            for timeframe in MVP_TUSHARE_TIMEFRAMES
            if self._entitlement.permits(timeframe, now=now)
            and (
                timeframe not in {Timeframe.HOUR_4, Timeframe.WEEK}
                or self._entitlement.derived_allowed
            )
        ]

    def membership_report(self) -> dict[str, Any]:
        expected = 100
        return {
            "manifest_version": self._manifest.version if self._manifest else None,
            "expected_members": expected,
            "mapped_members": len(self._members),
            "complete": len(self._members) == expected,
            "source_id": "tushare_pro",
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
        instrument = self._resolve_member(ticker)
        self._require_entitlement(timeframe)
        if timeframe in {Timeframe.HOUR_4, Timeframe.WEEK}:
            if self._entitlement is None or not self._entitlement.derived_allowed:
                raise EntitlementBlocked(
                    "TuShare derived timeframe persistence is blocked_for_entitlement"
                )
        if timeframe == Timeframe.HOUR_4:
            raw = await self.fetch(
                ticker, Timeframe.MIN_15, start=start, end=end, limit=max(limit * 16, 200)
            )
            key = self._key(instrument, Timeframe.MIN_15)
            mvp_rows = [self._to_mvp(key, candle) for candle in raw]
            result = aggregate_15m_to_4h(
                mvp_rows,
                calendar_id=_A_SHARE_CALENDAR,
                cutoff=self._clock(),
                run_id=f"tushare-preview:{instrument.instrument_id}",
            )
            self.timeframe_transform = TimeframeTransform(
                raw_timeframe=Timeframe.MIN_15,
                timeframe_origin="aggregated",
                aggregation={
                    "rule": result.transform_receipt.aggregation_rule_version
                    if result.transform_receipt
                    else "cn_a_session_4h_v1",
                    "partial_bucket_policy": "drop_and_record",
                    "partial_bucket_count": len(result.partial_buckets),
                },
            )
            self.last_raw_response = dict(self.last_raw_response or {})
            self.last_raw_response["requested_timeframe"] = timeframe.value
            return [self._from_mvp(row) for row in result.candles[-limit:]]
        if timeframe == Timeframe.WEEK:
            raw = await self.fetch(
                ticker, Timeframe.DAY, start=start, end=end, limit=max(limit * 7 + 10, 200)
            )
            key = self._key(instrument, Timeframe.DAY)
            mvp_rows = [self._to_mvp(key, candle) for candle in raw]
            result = aggregate_daily_to_weekly(
                mvp_rows,
                calendar_id=_A_SHARE_CALENDAR,
                cutoff=self._clock(),
                run_id=f"tushare-preview:{instrument.instrument_id}",
            )
            self.timeframe_transform = TimeframeTransform(
                raw_timeframe=Timeframe.DAY,
                timeframe_origin="aggregated",
                aggregation={
                    "rule": result.transform_receipt.aggregation_rule_version
                    if result.transform_receipt
                    else "completed_local_calendar_week_v1",
                    "partial_bucket_policy": "defer_until_closed",
                    "partial_bucket_count": len(result.partial_buckets),
                },
            )
            self.last_raw_response = dict(self.last_raw_response or {})
            self.last_raw_response["requested_timeframe"] = timeframe.value
            return [self._from_mvp(row) for row in result.candles[-limit:]]
        return await self._fetch_native(instrument, timeframe, start=start, end=end, limit=limit)

    async def _fetch_native(
        self,
        instrument: Any,
        timeframe: Timeframe,
        *,
        start: str | None,
        end: str | None,
        limit: int,
    ) -> list[Candle]:
        client = self._get_client()
        ts_code = instrument.provider_symbol or _to_tushare_code(instrument.display_symbol)
        request_params: dict[str, Any] = {
            "ts_code": ts_code,
            "start_date": start,
            "end_date": end,
            "limit": limit,
            "freq": "15min" if timeframe == Timeframe.MIN_15 else None,
        }
        self.last_raw_response = {
            "request_params": request_params,
            "response_body": None,
            "status_code": None,
            "error": None,
        }
        try:
            if timeframe == Timeframe.MIN_15:
                frame = await self._call(
                    lambda: client.stk_mins(
                        ts_code=ts_code,
                        start_date=start,
                        end_date=end,
                        freq="15min",
                    )
                )
            elif timeframe == Timeframe.DAY:
                method = (
                    client.index_daily
                    if ts_code in {"000001.SH", "000688.SH", "000015.SH"}
                    else client.daily
                )
                frame = await self._call(
                    lambda: method(ts_code=ts_code, start_date=start, end_date=end)
                )
            else:
                raise ProviderError(f"TuShare native timeframe {timeframe.value} is unsupported")
        except ProviderError:
            raise
        except Exception as exc:
            self._set_error(exc)
            failure = ProviderError(f"TuShare request failed for {instrument.display_symbol}")
            failure.code = str((self.last_raw_response or {}).get("error") or "provider_error")
            raise failure from exc
        rows = self._parse_rows(frame, instrument=instrument, timeframe=timeframe, limit=limit)
        if not rows:
            error = "market_closed_or_no_closed_rows"
            self.last_raw_response["error"] = error
            failure = ProviderError(
                f"TuShare returned no closed rows for {instrument.display_symbol}"
            )
            failure.code = "market_closed"
            raise failure
        self.last_raw_response["response_body"] = {
            "row_count": len(rows),
            "sha256": self._frame_hash(frame),
        }
        self.timeframe_transform = TimeframeTransform(
            raw_timeframe=timeframe,
            timeframe_origin="native",
            aggregation={"kind": "none", "rule": "native_passthrough"},
        )
        history_status = (
            "new_listing_exception"
            if instrument.display_symbol in {"688825", "688836"}
            else "standard"
        )
        self.source_identity = {
            "source_id": "tushare_pro",
            "provider_symbol": ts_code,
            "instrument_id": instrument.instrument_id,
            "manifest_version": self._manifest.version if self._manifest else None,
            "entitlement_status": self._entitlement.status if self._entitlement else "blocked",
            "history_status": history_status,
        }
        return rows

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        if not self._token:
            raise EntitlementBlocked("TuShare token/entitlement is required")
        ts.set_token(self._token)
        self._client = ts.pro_api()
        return self._client

    async def _call(self, operation: Callable[[], Any]) -> Any:
        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                return operation()
            except Exception as exc:  # provider SDK raises several exception types
                last_error = exc
                if attempt >= self._max_retries:
                    self._set_error(exc)
                    raise
                if self._retry_delay:
                    await asyncio.sleep(self._retry_delay * (attempt + 1))
        assert last_error is not None
        raise last_error

    def _set_error(self, error: Exception) -> None:
        text = str(error).lower()
        code = "rate_limited" if "429" in text or "rate limit" in text else "provider_error"
        self.last_raw_response = dict(self.last_raw_response or {})
        self.last_raw_response["error"] = code

    def _require_entitlement(self, timeframe: Timeframe) -> None:
        now = self._clock()
        if not self._token or self._entitlement is None:
            raise EntitlementBlocked("TuShare source is blocked_for_entitlement")
        if not self._entitlement.permits(timeframe, now=now):
            raise EntitlementBlocked(f"TuShare {timeframe.value} is blocked_for_entitlement")
        if not self._entitlement.persistence_allowed or not self._entitlement.non_display_allowed:
            raise EntitlementBlocked(
                "TuShare persistence/non-display use is blocked_for_entitlement"
            )

    def _resolve_member(self, ticker: str) -> Any:
        symbol = ticker.strip()
        if symbol in self._members:
            return self._members[symbol]
        ts_code = _to_tushare_code(symbol)
        for item in self._members.values():
            if item.provider_symbol.upper() == ts_code.upper():
                return item
        if self._manifest is not None:
            raise ProviderError(f"A-share ticker {ticker} is not in the MVP manifest")
        return type(
            "FallbackInstrument",
            (),
            {
                "display_symbol": symbol,
                "provider_symbol": ts_code,
                "instrument_id": f"CN.A.{symbol}",
            },
        )()

    def _key(self, instrument: Any, timeframe: Timeframe) -> CandleSeriesKey:
        return CandleSeriesKey(
            instrument_id=instrument.instrument_id,
            display_symbol=instrument.display_symbol,
            provider_symbol=instrument.provider_symbol,
            source_id="tushare_pro",
            asset_class="a_share",
            timeframe=timeframe,
            adjustment_basis=getattr(instrument, "adjustment_basis", "raw_unadjusted"),
            manifest_version=self._manifest.version if self._manifest else "mvp_universe_v1",
        )

    def _to_mvp(self, key: CandleSeriesKey, candle: Candle) -> MvpCandle:
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

    def _parse_rows(
        self, frame: Any, *, instrument: Any, timeframe: Timeframe, limit: int
    ) -> list[Candle]:
        if frame is None or getattr(frame, "empty", False):
            return []
        rows: list[Candle] = []
        now = self._clock().astimezone(timezone.utc)
        for _, row in frame.iterrows():
            try:
                stamp_value = self._row_value(row, "trade_time", "trade_date", "datetime")
                stamp = self._parse_row_timestamp(stamp_value, timeframe)
                close_boundary = (
                    stamp + timedelta(minutes=15)
                    if timeframe == Timeframe.MIN_15
                    else self._daily_close(stamp)
                )
                if close_boundary > now:
                    continue
                values = [
                    float(self._row_value(row, name)) for name in ("open", "high", "low", "close")
                ]
                volume = float(self._row_value(row, "vol", "volume"))
                amount_value = self._row_value(row, "amount", "成交额", default=None)
                amount = float(amount_value) if amount_value not in (None, "", "nan") else None
                if not all(math.isfinite(value) for value in (*values, volume)) or volume < 0:
                    raise ValueError("non-finite OHLCV")
                if values[1] < max(values[0], values[3]) or values[2] > min(values[0], values[3]):
                    raise ValueError("OHLC invariant")
                rows.append(
                    Candle(
                        timestamp=stamp.isoformat(),
                        open=values[0],
                        high=values[1],
                        low=values[2],
                        close=values[3],
                        volume=volume,
                        amount=amount,
                    )
                )
            except (KeyError, TypeError, ValueError, OverflowError) as exc:
                self.last_raw_response["error"] = "malformed_row"
                failure = ProviderError(f"TuShare returned malformed {timeframe.value} row")
                failure.code = "malformed_row"
                raise failure from exc
        rows.sort(key=lambda candle: candle.timestamp)
        return rows[-limit:] if limit else rows

    @staticmethod
    def _row_value(row: Any, *names: str, default: Any = ...) -> Any:
        for name in names:
            if hasattr(row, "get"):
                value = row.get(name, None)
            else:
                value = getattr(row, name, None)
            if value is not None:
                return value
        if default is not ...:
            return default
        raise KeyError(names[0])

    @staticmethod
    def _parse_row_timestamp(value: Any, timeframe: Timeframe) -> datetime:
        text = str(value).strip()
        if timeframe == Timeframe.DAY and len(text) == 8 and text.isdigit():
            text = f"{text[:4]}-{text[4:6]}-{text[6:8]}"
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(
                tzinfo=_A_SHARE_ZONE if timeframe == Timeframe.MIN_15 else timezone.utc
            )
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _daily_close(stamp: datetime) -> datetime:
        local = stamp.astimezone(_A_SHARE_ZONE)
        return datetime.combine(local.date(), time(15, 0), tzinfo=_A_SHARE_ZONE).astimezone(
            timezone.utc
        )

    @staticmethod
    def _frame_hash(frame: Any) -> str:
        try:
            payload = frame.to_json(orient="records", date_format="iso")
        except Exception:
            payload = repr(frame)
        return sha256(payload.encode("utf-8")).hexdigest()
