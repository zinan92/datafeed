from __future__ import annotations

from pathlib import Path

from kline.config import Settings
from kline.models import AssetClass, Timeframe
from kline.provenance import canonical_ticker_for_source, source_manifest
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
