import httpx
import pytest

from kline.models import Timeframe
from kline.providers.fred import FredCsvProvider


@pytest.mark.asyncio
async def test_fred_provider_normalizes_daily_series_and_filters_range():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["id"] == "DFII10"
        return httpx.Response(
            200,
            text="observation_date,DFII10\n2026-07-13,2.01\n2026-07-14,.\n2026-07-15,2.05\n",
            request=request,
        )

    provider = FredCsvProvider(transport=httpx.MockTransport(handler))
    bars = await provider.fetch(
        "DFII10",
        Timeframe.DAY,
        start="2026-07-14",
        limit=10,
    )

    assert len(bars) == 1
    assert bars[0].timestamp == "2026-07-15T00:00:00+00:00"
    assert bars[0].close == 2.05
