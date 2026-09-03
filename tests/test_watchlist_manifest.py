from pathlib import Path

import pytest

from kline.models import AssetClass
from kline.mvp_manifest import ManifestError
from kline.provenance import source_manifest
from kline.watchlist_manifest import load_watchlist_manifest


MANIFEST_PATH = Path(__file__).parents[1] / "configs" / "watchlist_manifest.json"


def test_watchlist_manifest_loads_exact_approved_daily_universe() -> None:
    manifest = load_watchlist_manifest(MANIFEST_PATH)

    assert manifest.version == "watchlist_universe_v1"
    assert len(manifest.instruments) == 58
    assert all(item.instrument_id.startswith("WATCH.") for item in manifest.instruments)
    assert all(item.required_timeframes == ("1d",) for item in manifest.instruments)
    assert all(
        set(item.not_applicable_timeframes) == {"15m", "1h", "4h", "1w"}
        for item in manifest.instruments
    )
    assert "051505" not in {item.display_symbol for item in manifest.instruments}
    hynix = next(item for item in manifest.instruments if item.display_symbol == "000660.KS")
    assert hynix.source_status == "configured"
    assert hynix.source_id == "yahoo_finance"
    assert hynix.calendar_id == "kr_equities"


def test_watchlist_manifest_contains_all_sixteen_cross_market_bindings() -> None:
    manifest = load_watchlist_manifest(MANIFEST_PATH)
    expected = {
        "SPX": ("yahoo_finance_etf", "SPY", "proxy"),
        "NDX": ("yahoo_finance_etf", "QQQ", "proxy"),
        "DXY": ("yahoo_finance_etf", "UUP", "proxy"),
        "SCHD": ("yahoo_finance_etf", "SCHD", None),
        "VIX": ("yahoo_finance_index", "^VIX", None),
        "BTC": ("hyperliquid_perpetual_public", "BTC", None),
        "ETH": ("hyperliquid_perpetual_public", "ETH", None),
        "HYPE": ("hyperliquid_perpetual_public", "HYPE", None),
        "sh000001": ("tencent_kline", "sh000001", None),
        "sh000688": ("tencent_kline", "sh000688", None),
        "sh000015": ("tencent_kline", "sh000015", None),
        "^N225": ("yahoo_finance_index", "^N225", None),
        "^KS11": ("yahoo_finance_index", "^KS11", None),
        "CL=F": ("yahoo_finance_futures", "CL=F", None),
        "GC=F": ("yahoo_finance_futures", "GC=F", None),
        "SI=F": ("yahoo_finance_futures", "SI=F", None),
    }
    entries = {item.display_symbol: item for item in manifest.instruments}

    assert set(expected).issubset(entries)
    for symbol, (source_id, provider_symbol, identity_role) in expected.items():
        item = entries[symbol]
        assert (item.source_id, item.provider_symbol) == (source_id, provider_symbol)
        assert item.required_timeframes == ("1d",)
        assert item.asset_class in {"index", "etf", "crypto", "commodity"}
        if identity_role is None:
            assert "identity_role" not in item.metadata
        else:
            assert item.metadata["identity_role"] == identity_role
            assert item.metadata["proxy_for"]


def test_watchlist_proxy_metadata_is_uniform_and_excludes_real_indices() -> None:
    manifest = load_watchlist_manifest(MANIFEST_PATH)
    proxies = {
        item.display_symbol: item
        for item in manifest.instruments
        if item.metadata.get("proxy_for")
    }

    assert set(proxies) == {"SPX", "NDX", "DXY"}
    assert {
        symbol: (item.provider_symbol, item.metadata["identity_role"], item.metadata["proxy_for"])
        for symbol, item in proxies.items()
    } == {
        "SPX": ("SPY", "proxy", "S&P 500 Index"),
        "NDX": ("QQQ", "proxy", "Nasdaq-100 Index"),
        "DXY": ("UUP", "proxy", "DXY"),
    }
    assert all(item.provider_symbol != item.display_symbol for item in proxies.values())
    vix = next(item for item in manifest.instruments if item.display_symbol == "VIX")
    assert vix.provider_symbol == "^VIX"
    assert "identity_role" not in vix.metadata
    assert "proxy_for" not in vix.metadata


def test_tencent_kline_allowlist_remains_three_indices() -> None:
    source = source_manifest("tencent_kline", AssetClass.INDEX)

    assert source.supports_timeframe("sh000001", "1d")
    assert source.supports_timeframe("sh000688", "1d")
    assert source.supports_timeframe("sh000015", "1d")
    assert not source.supports_timeframe("300308", "1d")


def test_watchlist_manifest_reuses_required_field_validation(tmp_path: Path) -> None:
    invalid = tmp_path / "watchlist.json"
    invalid.write_text('{"version":"watchlist_universe_v1","instruments":[{}]}')

    with pytest.raises(ManifestError, match=r"instrument\[0\].universe"):
        load_watchlist_manifest(invalid)
