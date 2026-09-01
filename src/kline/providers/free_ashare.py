"""Free, personal-use A-share OHLCV adapters.

Tencent is the primary technical endpoint for 15m/1h and daily bars.  The
Tonghuashun line endpoint is a narrow fallback for 1h/daily requests.  The
adapter records which endpoint answered; it does not infer commercial rights.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import math
import re
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from kline.market_calendar import aggregate_15m_to_4h, aggregate_daily_to_weekly
from kline.models import Candle, Timeframe, TimeframeTransform
from kline.providers.base import ProviderError
from kline.providers.free_common import requested_cutoff
from kline.storage import CandleSeriesKey, MvpCandle


_TENCENT_MINUTE_URL = "https://ifzq.gtimg.cn/appstock/app/kline/mkline"
_TENCENT_DAILY_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
_TENJQKA_URL = "https://d.10jqka.com.cn/v6/line/hs_{code}/{period}/last.js"
_A_SHARE_TIMEFRAMES = (
    Timeframe.MIN_15,
    Timeframe.HOUR_1,
    Timeframe.HOUR_4,
    Timeframe.DAY,
    Timeframe.WEEK,
)


def _provider_code(ticker: str) -> str:
    normalized = ticker.upper().strip().split(".", 1)[0]
    if not normalized or not normalized.isdigit():
        raise ProviderError(f"invalid A-share symbol: {ticker}")
    return ("sh" if normalized.startswith(("6", "9")) else "sz") + normalized


def _stamp(raw: Any, *, timezone_name: str = "Asia/Shanghai") -> str:
    text = str(raw).strip()
    zone = ZoneInfo(timezone_name)
    for fmt in ("%Y%m%d%H%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return (
                datetime.strptime(text, fmt)
                .replace(tzinfo=zone)
                .astimezone(timezone.utc)
                .isoformat()
            )
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        if re.fullmatch(r"\d{8}", text):
            return (
                datetime.strptime(text, "%Y%m%d")
                .replace(tzinfo=zone)
                .astimezone(timezone.utc)
                .isoformat()
            )
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            return (
                datetime.strptime(text, "%Y-%m-%d")
                .replace(tzinfo=zone)
                .astimezone(timezone.utc)
                .isoformat()
            )
        raise ProviderError(f"invalid Tencent timestamp: {raw}")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=zone)
    return parsed.astimezone(timezone.utc).isoformat()


def _number(value: Any, *, required: bool = True) -> float | None:
    if value in (None, "", {}):
        if required:
            raise ValueError("missing numeric value")
        return None
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("non-finite numeric value")
    return result


def _candle(
    row: list[Any] | tuple[Any, ...], *, timezone_name: str, interval: timedelta | None = None
) -> Candle:
    if len(row) < 6:
        raise ValueError("A-share row has fewer than six columns")
    timestamp = _stamp(row[0], timezone_name=timezone_name)
    if interval is not None:
        timestamp = (datetime.fromisoformat(timestamp) - interval).isoformat()
    open_value = _number(row[1])
    close_value = _number(row[2])
    high_value = _number(row[3])
    low_value = _number(row[4])
    volume = _number(row[5], required=False)
    amount = _number(row[6], required=False) if len(row) > 6 else None
    assert open_value is not None and close_value is not None
    assert high_value is not None and low_value is not None
    if high_value < max(open_value, close_value) or low_value > min(open_value, close_value):
        raise ValueError("OHLC bounds are invalid")
    return Candle(
        timestamp=timestamp,
        open=open_value,
        high=high_value,
        low=low_value,
        close=close_value,
        volume=volume,
        amount=amount,
    )


def parse_tencent_rows(
    rows: Any, *, timezone_name: str = "Asia/Shanghai", interval: timedelta | None = None
) -> list[Candle]:
    if not isinstance(rows, list):
        raise ValueError("Tencent K-line rows are not a list")
    result: list[Candle] = []
    for row in rows:
        if not isinstance(row, (list, tuple)):
            continue
        try:
            result.append(_candle(row, timezone_name=timezone_name, interval=interval))
        except (TypeError, ValueError):
            continue
    return sorted(result, key=lambda candle: candle.timestamp)


def parse_10jqka_rows(
    text_or_rows: str | list[str], *, timezone_name: str = "Asia/Shanghai"
) -> list[Candle]:
    rows = text_or_rows if isinstance(text_or_rows, list) else text_or_rows.split(";")
    result: list[Candle] = []
    for raw in rows:
        if not raw:
            continue
        values = raw.split(",") if isinstance(raw, str) else raw
        try:
            if len(values) < 6:
                continue
            result.append(
                Candle(
                    timestamp=_stamp(values[0], timezone_name=timezone_name),
                    open=_number(values[1]),
                    high=_number(values[2]),
                    low=_number(values[3]),
                    close=_number(values[4]),
                    volume=_number(values[5], required=False),
                    amount=_number(values[6], required=False) if len(values) > 6 else None,
                )
            )
        except (TypeError, ValueError):
            continue
    return sorted(result, key=lambda candle: candle.timestamp)


class AShareFreeProvider:
    """Fetch A-share bars from Tencent with a Tonghuashun fallback."""

    def __init__(self, *, timeout: float = 15.0, transport: httpx.AsyncBaseTransport | None = None):
        self._timeout = timeout
        self._transport = transport
        self.last_raw_response: dict[str, Any] | None = None
        self.timeframe_transform: TimeframeTransform | None = None
        self.source_identity: dict[str, Any] = {}

    def supported_timeframes(self) -> list[Timeframe]:
        return list(_A_SHARE_TIMEFRAMES)

    async def fetch(
        self,
        ticker: str,
        timeframe: Timeframe,
        *,
        start: str | None = None,
        end: str | None = None,
        limit: int = 500,
    ) -> list[Candle]:
        if timeframe not in _A_SHARE_TIMEFRAMES:
            raise ProviderError(f"free A-share source does not support {timeframe.value}")
        self.timeframe_transform = None
        self.last_raw_response = None
        if timeframe == Timeframe.HOUR_4:
            base = await self.fetch(
                ticker, Timeframe.MIN_15, start=start, end=end, limit=max(limit * 16, 320)
            )
            rows = self._as_mvp(base, ticker, "15m")
            aggregate = aggregate_15m_to_4h(
                rows,
                calendar_id="cn_a",
                cutoff=requested_cutoff(end),
                run_id="free-tencent-preview",
            )
            if not aggregate.candles:
                error = ProviderError(f"no complete 4h A-share bars returned for {ticker}")
                error.code = "transform_incomplete"
                raise error
            receipt = aggregate.transform_receipt
            self.timeframe_transform = TimeframeTransform(
                raw_timeframe=Timeframe.MIN_15,
                timeframe_origin="aggregated",
                aggregation={
                    "rule": receipt.aggregation_rule_version if receipt else "cn_a_session_4h_v1",
                    "partial_bucket_count": receipt.partial_bucket_count if receipt else 0,
                    "bucket_anchor": receipt.bucket_anchor if receipt else "09:30",
                    "partial_bucket_policy": receipt.partial_bucket_policy
                    if receipt
                    else "drop_and_record",
                },
            )
            return self._from_mvp(aggregate.candles, limit)
        if timeframe == Timeframe.WEEK:
            base = await self.fetch(
                ticker, Timeframe.DAY, start=start, end=end, limit=max(limit * 7, 120)
            )
            rows = self._as_mvp(base, ticker, "1d")
            aggregate = aggregate_daily_to_weekly(
                rows,
                calendar_id="cn_a",
                cutoff=requested_cutoff(end),
                run_id="free-tencent-preview",
            )
            if not aggregate.candles:
                error = ProviderError(f"no completed weekly A-share bars returned for {ticker}")
                error.code = "transform_incomplete"
                raise error
            receipt = aggregate.transform_receipt
            self.timeframe_transform = TimeframeTransform(
                raw_timeframe=Timeframe.DAY,
                timeframe_origin="aggregated",
                aggregation={
                    "rule": receipt.aggregation_rule_version
                    if receipt
                    else "completed_local_calendar_week_v1",
                    "partial_bucket_count": receipt.partial_bucket_count if receipt else 0,
                    "bucket_anchor": receipt.bucket_anchor if receipt else "local_week",
                    "partial_bucket_policy": receipt.partial_bucket_policy
                    if receipt
                    else "defer_until_closed",
                },
            )
            return self._from_mvp(aggregate.candles, limit)

        code = _provider_code(ticker)
        if timeframe == Timeframe.MIN_15:
            rows, selected = await self._tencent_minute(code, "m15", limit=limit)
        elif timeframe == Timeframe.HOUR_1:
            rows, selected = await self._with_fallback(
                code, "60", "m60", start=start, end=end, limit=limit
            )
        else:
            rows, selected = await self._with_fallback(
                code, "01", "day", start=start, end=end, limit=limit
            )
        candles = rows
        if start:
            candles = [candle for candle in candles if candle.timestamp >= _stamp(start)]
        if end:
            candles = [candle for candle in candles if candle.timestamp < _stamp(end)]
        if limit:
            candles = candles[-limit:]
        self.source_identity = {
            "source_id": "tencent_stock_free",
            "provider_symbol": code,
            "selected_source": selected,
            "adjustment_basis": "qfq" if selected == "tencent" else "unverified",
            "canonical_adjustment_basis": "qfq",
            "adjustment_basis_evidence": "tonghuashun_unverified" if selected == "tonghuashun" else "tencent_qfq",
            "fallback_from": "tencent" if selected == "tonghuashun" else None,
            "fallback_chain": ["tonghuashun"] if selected == "tonghuashun" else [],
        }
        return candles

    async def _tencent_minute(self, code: str, key: str, *, limit: int) -> tuple[list[Candle], str]:
        params = {"param": f"{code},{key},,{max(1, min(limit, 320))}"}
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                transport=self._transport,
                headers={"Referer": "https://gu.qq.com/"},
            ) as client:
                response = await client.get(_TENCENT_MINUTE_URL, params=params)
                response.raise_for_status()
                payload = response.json()
            item = (payload.get("data") or {}).get(code) or {}
            interval = timedelta(minutes=15) if key == "m15" else timedelta(hours=1)
            rows = parse_tencent_rows(
                item.get(key), timezone_name="Asia/Shanghai", interval=interval
            )
            if not rows:
                raise ProviderError("Tencent returned no minute rows")
            self.last_raw_response = {
                "endpoint": _TENCENT_MINUTE_URL,
                "http_status": response.status_code,
                "response_sha256": sha256(response.content).hexdigest(),
                "row_count": len(rows),
            }
            return rows, "tencent"
        except (httpx.HTTPError, ValueError, ProviderError) as error:
            raise ProviderError(f"Tencent minute request failed for {code}: {error}") from error

    async def _tencent_daily(
        self, code: str, *, start: str | None, end: str | None, limit: int
    ) -> tuple[list[Candle], str]:
        params = {
            "param": f"{code},day,{(start or '')[:10]},{(end or '')[:10]},{max(1, min(limit, 500))},qfq"
        }
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                response = await client.get(_TENCENT_DAILY_URL, params=params)
                response.raise_for_status()
                payload = response.json()
            item = (payload.get("data") or {}).get(code) or {}
            rows = parse_tencent_rows(
                item.get("qfqday") or item.get("day"), timezone_name="Asia/Shanghai"
            )
            if not rows:
                raise ProviderError("Tencent returned no daily rows")
            self.last_raw_response = {
                "endpoint": _TENCENT_DAILY_URL,
                "http_status": response.status_code,
                "response_sha256": sha256(response.content).hexdigest(),
                "row_count": len(rows),
            }
            return rows, "tencent"
        except (httpx.HTTPError, ValueError, ProviderError) as error:
            raise ProviderError(f"Tencent daily request failed for {code}: {error}") from error

    async def _with_fallback(
        self,
        code: str,
        tenjqka_period: str,
        tencent_key: str,
        *,
        start: str | None,
        end: str | None,
        limit: int,
    ) -> tuple[list[Candle], str]:
        try:
            if tencent_key == "day":
                return await self._tencent_daily(code, start=start, end=end, limit=limit)
            return await self._tencent_minute(code, tencent_key, limit=limit)
        except ProviderError as tencent_error:
            try:
                async with httpx.AsyncClient(
                    timeout=self._timeout, transport=self._transport
                ) as client:
                    response = await client.get(
                        _TENJQKA_URL.format(code=code[2:], period=tenjqka_period)
                    )
                    response.raise_for_status()
                text = response.text
                match = re.search(r"\{.*\}", text, flags=re.DOTALL)
                payload = json.loads(match.group(0)) if match else {}
                rows = parse_10jqka_rows(payload.get("data", ""), timezone_name="Asia/Shanghai")
                if not rows:
                    raise ProviderError("Tonghuashun returned no rows")
                self.last_raw_response = {
                    "endpoint": str(response.url),
                    "http_status": response.status_code,
                    "response_sha256": sha256(response.content).hexdigest(),
                    "row_count": len(rows),
                    "fallback_from": "tencent",
                }
                self.source_identity = {
                    "selected_source": "tonghuashun",
                    "fallback_from": "tencent",
                    "adjustment_basis": "unverified",
                    "adjustment_basis_evidence": "unverified_tonghuashun_fallback",
                }
                return rows, "tonghuashun"
            except (httpx.HTTPError, ValueError, ProviderError) as fallback_error:
                raise ProviderError(
                    f"free A-share sources failed for {code}: Tencent={tencent_error}; Tonghuashun={fallback_error}"
                ) from fallback_error

    @staticmethod
    def _as_mvp(candles: list[Candle], ticker: str, timeframe: str) -> list[MvpCandle]:
        code = _provider_code(ticker)
        key = CandleSeriesKey(
            instrument_id=f"CN.A.{code[2:]}",
            display_symbol=code[2:],
            provider_symbol=code,
            source_id="tencent_stock_free",
            asset_class="a_share",
            timeframe=timeframe,
            adjustment_basis="qfq",
            manifest_version="mvp_universe_v1",
        )
        return [
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
                is_derived=False,
            )
            for candle in candles
        ]

    @staticmethod
    def _from_mvp(rows: tuple[MvpCandle, ...], limit: int) -> list[Candle]:
        selected = rows[-limit:] if limit else rows
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
