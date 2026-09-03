from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from kline.config import Settings
from kline.models import AssetClass, Candle, Timeframe
from kline.ports import FetchReceipt, ProviderBackedMarketDataAdapter
from kline.provenance import canonical_ticker_for_source, source_manifest
from kline.providers.hk import HKStockProvider
from kline.providers.us import _source_timezone
from kline.registry import get_adapter_for_source, init


def test_hong_kong_source_normalizes_exact_registry_codes() -> None:
    source = source_manifest("yahoo_finance_hk", AssetClass.HK_STOCK)
    assert source.asset_class is AssetClass.HK_STOCK
    assert source.supports_timeframe("00100", Timeframe.DAY)
    assert source.supports_timeframe("0100.HK", Timeframe.DAY)
    assert canonical_ticker_for_source("yahoo_finance_hk", AssetClass.HK_STOCK, "00100") == "0100.HK"
    assert canonical_ticker_for_source("yahoo_finance_hk", AssetClass.HK_STOCK, "02513") == "2513.HK"
    assert canonical_ticker_for_source("yahoo_finance_hk", AssetClass.HK_STOCK, "00700") == "0700.HK"
    assert canonical_ticker_for_source("yahoo_finance_hk", AssetClass.HK_STOCK, "09988") == "9988.HK"
    assert not source.supports_timeframe("00001", Timeframe.DAY)


def test_hong_kong_symbols_use_hong_kong_market_timezone() -> None:
    assert _source_timezone("0100.HK") == "Asia/Hong_Kong"


def test_hong_kong_adapter_is_registered_without_changing_us_adapter(tmp_path: Path) -> None:
    init(Settings(db_path=str(tmp_path / "hk.db"), load_entrypoint_adapters=False))
    hk = get_adapter_for_source("yahoo_finance_hk", AssetClass.HK_STOCK)
    us = get_adapter_for_source("yahoo_finance", AssetClass.US_STOCK)
    assert hk.manifest.asset_class is AssetClass.HK_STOCK
    assert hk.manifest.source_id == "yahoo_finance_hk"
    assert us.manifest.asset_class is AssetClass.US_STOCK


@pytest.mark.asyncio
async def test_hong_kong_adapter_serializes_shared_provider_state(monkeypatch) -> None:
    active = 0
    max_active = 0

    async def fake_us_fetch(self, ticker, timeframe, *, start=None, end=None, limit=500):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        self.source_identity = {"provider_symbol": ticker}
        active -= 1
        return [
            Candle(
                timestamp="2026-09-03",
                open=100,
                high=102,
                low=99,
                close=101,
                volume=10,
            )
        ]

    monkeypatch.setattr("kline.providers.us.USStockProvider.fetch", fake_us_fetch)
    adapter = ProviderBackedMarketDataAdapter(
        source_manifest("yahoo_finance_hk", AssetClass.HK_STOCK),
        HKStockProvider(),
    )
    first, second = await asyncio.gather(
        adapter.fetch_candles_with_receipt("0100.HK", Timeframe.DAY, limit=1),
        adapter.fetch_candles_with_receipt("2513.HK", Timeframe.DAY, limit=1),
    )

    assert isinstance(first, FetchReceipt)
    assert isinstance(second, FetchReceipt)
    assert max_active == 1
    assert first.source_identity["provider_symbol"] == "0100.HK"
    assert second.source_identity["provider_symbol"] == "2513.HK"
    assert first.source_identity["market"] == "HK"
    assert second.source_identity["listing_venue"] == "HKEX"
