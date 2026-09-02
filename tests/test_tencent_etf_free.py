from datetime import date

import httpx
import pytest

from kline.models import AssetClass, Timeframe
from kline.providers.ashare import TencentIndexProvider
from kline.providers.base import ProviderError
from kline.providers.free_ashare_etf import (
    TencentEtfFreeProvider,
    tencent_etf_provider_symbol,
)
from kline.provenance import source_manifest


@pytest.mark.parametrize(
    ("ticker", "provider_symbol"),
    [
        ("588180", "sh588180"),
        ("159510", "sz159510"),
        ("159516", "sz159516"),
        ("160000", "sz160000"),
    ],
)
def test_tencent_etf_prefix_contract(ticker: str, provider_symbol: str) -> None:
    assert tencent_etf_provider_symbol(ticker) == provider_symbol


@pytest.mark.asyncio
@pytest.mark.parametrize("ticker", ["588180", "159516"])
async def test_tencent_etf_daily_returns_qfq_rows_with_own_source_identity(ticker: str) -> None:
    provider_symbol = tencent_etf_provider_symbol(ticker)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["param"].startswith(f"{provider_symbol},day")
        return httpx.Response(
            200,
            json={
                "data": {
                    provider_symbol: {
                        "qfqday": [
                            ["2026-08-31", "1", "1.1", "1.2", "0.9", "1000"],
                            ["2026-09-01", "1.1", "1.2", "1.3", "1", "1200"],
                        ]
                    }
                }
            },
            request=request,
        )

    provider = TencentEtfFreeProvider(
        transport=httpx.MockTransport(handler),
        request_interval_seconds=0,
    )

    candles = await provider.fetch(ticker, Timeframe.DAY, limit=2)

    assert len(candles) == 2
    assert provider.source_identity == {
        "source_id": "tencent_etf_free",
        "provider_symbol": provider_symbol,
        "selected_source": "tencent",
        "adjustment_basis": "qfq",
        "canonical_adjustment_basis": "qfq",
        "adjustment_basis_evidence": "tencent_qfq",
        "fallback_chain": [],
    }
    assert provider.last_attempts[0]["status"] == "success"
    assert provider.last_attempts[0]["http_status"] == 200


def test_tencent_etf_source_manifest_is_daily_and_etf_typed() -> None:
    manifest = source_manifest("tencent_etf_free", AssetClass.ETF)

    assert manifest.asset_class == AssetClass.ETF
    assert manifest.supports_timeframe("588180", Timeframe.DAY)
    assert not manifest.supports_timeframe("588180", Timeframe.WEEK)
    assert not manifest.supports_timeframe("510050", Timeframe.DAY)


@pytest.mark.asyncio
async def test_tencent_index_provider_remains_three_index_only() -> None:
    provider = TencentIndexProvider(today=lambda: date(2026, 9, 2))

    with pytest.raises(ProviderError, match="explicit market-prefixed"):
        await provider.fetch("588180", Timeframe.DAY)
