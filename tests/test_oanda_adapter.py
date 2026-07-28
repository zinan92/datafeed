import httpx
import pytest

from kline.models import Timeframe
from kline.providers.oanda import OandaV20Adapter


@pytest.mark.asyncio
async def test_oanda_adapter_normalizes_complete_candles():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer secret"
        return httpx.Response(200, json={"candles": [{"complete": True, "time": "2026-07-15T10:00:00.000000000Z", "volume": 12, "mid": {"o": "4030", "h": "4032", "l": "4029", "c": "4031"}}]}, request=request)

    adapter = OandaV20Adapter({"token": "secret"}, transport=httpx.MockTransport(handler))
    bars = await adapter.fetch_candles("GOLD", Timeframe.MIN_5, limit=1)

    assert bars[0].timestamp == "2026-07-15T10:00:00+00:00"
    assert bars[0].close == 4031
    assert adapter.manifest.canonical_instrument_id("XAU_USD") == "GOLD"
