from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from kline.models import Timeframe
from kline.providers.base import ProviderError
from kline.providers.hyperliquid import HyperliquidPerpetualProvider


def _row(open_ms: int, close: str = "60.0") -> dict[str, object]:
    return {
        "t": open_ms,
        "T": open_ms + 4 * 60 * 60 * 1000 - 1,
        "s": "HYPE",
        "i": "4h",
        "o": "59.0",
        "c": close,
        "h": "61.0",
        "l": "58.5",
        "v": "100.0",
        "n": 10,
    }


@pytest.mark.asyncio
async def test_hyperliquid_native_4h_preserves_perpetual_identity():
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["type"] == "candleSnapshot"
        assert payload["req"]["coin"] == "HYPE"
        assert payload["req"]["interval"] == "4h"
        return httpx.Response(200, json=[_row(1786838400000)])

    provider = HyperliquidPerpetualProvider(transport=httpx.MockTransport(handler))
    candles = await provider.fetch("HYPE", Timeframe.HOUR_4, limit=1)

    assert len(candles) == 1
    assert provider.timeframe_transform is not None
    assert provider.timeframe_transform.timeframe_origin == "native"
    assert provider.source_identity == {
        "provider_symbol": "HYPE",
        "venue": "hyperliquid",
        "contract_type": "perpetual",
        "settlement": "USDC",
    }


@pytest.mark.asyncio
async def test_hyperliquid_rejects_unlisted_symbol():
    provider = HyperliquidPerpetualProvider(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=[])))
    with pytest.raises(ProviderError, match="only enables HYPE"):
        await provider.fetch("BTC", Timeframe.HOUR_4)


@pytest.mark.asyncio
async def test_hyperliquid_weekly_bar_is_dated_on_completed_friday():
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["req"]["interval"] == "1d"
        start = datetime(2026, 8, 10, tzinfo=timezone.utc)
        rows = []
        for offset in range(7):
            stamp = start + timedelta(days=offset)
            rows.append({**_row(int(stamp.timestamp() * 1000)), "T": int((stamp + timedelta(days=1)).timestamp() * 1000) - 1, "i": "1d"})
        return httpx.Response(200, json=rows)

    provider = HyperliquidPerpetualProvider(transport=httpx.MockTransport(handler))
    candles = await provider.fetch("HYPE", Timeframe.WEEK, limit=1)

    assert candles[0].timestamp == "2026-08-14"
