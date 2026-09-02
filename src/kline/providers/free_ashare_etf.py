"""Daily A-share ETF candles from Tencent's public qfq endpoint."""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import time
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from kline.models import Candle, Timeframe, TimeframeTransform
from kline.providers.base import ProviderError
from kline.providers.free_ashare import parse_tencent_rows
from kline.providers.free_common import RequestPacer


_TENCENT_DAILY_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"


def tencent_etf_provider_symbol(ticker: str) -> str:
    code = ticker.upper().strip().split(".", 1)[0]
    if not code.isdigit() or len(code) != 6:
        raise ProviderError(f"invalid A-share ETF symbol: {ticker}")
    if code.startswith("5"):
        return f"sh{code}"
    if code.startswith(("15", "16")):
        return f"sz{code}"
    raise ProviderError(f"unsupported A-share ETF prefix: {ticker}")


def _boundary(value: str) -> str:
    text = value[:10]
    return (
        datetime.fromisoformat(text)
        .replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        .astimezone(timezone.utc)
        .isoformat()
    )


class TencentEtfFreeProvider:
    """Fetch configured Shanghai/Shenzhen ETF daily bars without fallback."""

    def __init__(
        self,
        *,
        timeout: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
        request_interval_seconds: float = 0.25,
    ) -> None:
        self._timeout = timeout
        self._transport = transport
        self._pacer = RequestPacer(request_interval_seconds)
        self.last_raw_response: dict[str, Any] | None = None
        self.last_attempts: list[dict[str, Any]] = []
        self.timeframe_transform: TimeframeTransform | None = None
        self.source_identity: dict[str, Any] = {}

    def supported_timeframes(self) -> list[Timeframe]:
        return [Timeframe.DAY]

    async def fetch(
        self,
        ticker: str,
        timeframe: Timeframe,
        *,
        start: str | None = None,
        end: str | None = None,
        limit: int = 500,
    ) -> list[Candle]:
        if timeframe != Timeframe.DAY:
            raise ProviderError("Tencent ETF free source supports daily candles only")
        code = tencent_etf_provider_symbol(ticker)
        params = {
            "param": (
                f"{code},day,{(start or '')[:10]},{(end or '')[:10]},"
                f"{max(1, min(limit, 500))},qfq"
            )
        }
        self.last_attempts = []
        self.last_raw_response = None
        self.timeframe_transform = TimeframeTransform(
            raw_timeframe=Timeframe.DAY,
            timeframe_origin="native",
            aggregation={"kind": "none", "rule": "native_passthrough"},
        )
        self.source_identity = {
            "source_id": "tencent_etf_free",
            "provider_symbol": code,
            "selected_source": "tencent",
            "adjustment_basis": "qfq",
            "canonical_adjustment_basis": "qfq",
            "adjustment_basis_evidence": "tencent_qfq",
            "fallback_chain": [],
        }
        started_at = time.perf_counter()
        response: httpx.Response | None = None
        try:
            await self._pacer.wait()
            async with httpx.AsyncClient(
                timeout=self._timeout,
                transport=self._transport,
                headers={"Referer": "https://gu.qq.com/"},
            ) as client:
                response = await client.get(_TENCENT_DAILY_URL, params=params)
                response.raise_for_status()
                payload = response.json()
            item = (payload.get("data") or {}).get(code) or {}
            candles = parse_tencent_rows(
                item.get("qfqday") or item.get("day"), timezone_name="Asia/Shanghai"
            )
            if start:
                candles = [candle for candle in candles if candle.timestamp >= _boundary(start)]
            if end:
                candles = [candle for candle in candles if candle.timestamp < _boundary(end)]
            if limit:
                candles = candles[-limit:]
            if not candles:
                error = ProviderError(f"Tencent returned no daily ETF rows for {code}")
                error.code = "empty_response"
                raise error
            self.last_raw_response = {
                "endpoint": _TENCENT_DAILY_URL,
                "http_status": response.status_code,
                "response_sha256": sha256(response.content).hexdigest(),
                "row_count": len(candles),
            }
            self.last_attempts.append(
                {
                    "source": "tencent",
                    "endpoint": _TENCENT_DAILY_URL,
                    "status": "success",
                    "http_status": response.status_code,
                    "row_count": len(candles),
                    "latency_ms": round((time.perf_counter() - started_at) * 1000, 1),
                }
            )
            return candles
        except (httpx.HTTPError, ValueError, ProviderError) as exc:
            self.last_attempts.append(
                {
                    "source": "tencent",
                    "endpoint": _TENCENT_DAILY_URL,
                    "status": "error",
                    "http_status": response.status_code if response is not None else None,
                    "error": str(exc)[:240],
                    "latency_ms": round((time.perf_counter() - started_at) * 1000, 1),
                }
            )
            wrapped = ProviderError(f"Tencent ETF request failed for {code}: {exc}")
            wrapped.code = getattr(exc, "code", "provider_error")
            raise wrapped from exc
