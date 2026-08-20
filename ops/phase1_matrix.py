"""The committed 17-asset / 39-cell Phase 1 verification matrix."""

from __future__ import annotations

from typing import Any


PHASE1_MATRIX_VERSION = "weekly-macro-phase1-39-cell-v1"
PHASE1_POLICIES = {
    "cache_policy": "bypass",
    "quality": "strict",
    "quality_policy": "strict",
    "fallback_policy": "none",
}

_ASSETS: tuple[tuple[str, str, str, str, tuple[str, ...]], ...] = (
    ("dxy", "index", "DX-Y.NYB", "yahoo_finance_index", ("1d", "1w", "4h")),
    ("us2y", "macro", "DGS2", "treasury_official_csv", ("1d", "1w")),
    ("us10y", "macro", "DGS10", "treasury_official_csv", ("1d", "1w")),
    ("us2s10s", "macro", "T10Y2Y", "treasury_official_csv_derived", ("1d", "1w")),
    ("sp500", "index", "^GSPC", "yahoo_finance_index", ("1d", "1w")),
    ("nasdaq", "index", "^IXIC", "yahoo_finance_index", ("1d", "1w")),
    ("us_dividend", "etf", "SCHD", "yahoo_finance_etf", ("1d", "1w")),
    ("vix", "index", "^VIX", "yahoo_finance_index", ("1d", "1w")),
    ("bitcoin", "crypto", "BTC", "binance_spot_public", ("1d", "1w", "4h")),
    ("shanghai", "index", "sh000001", "tencent_kline", ("1d", "1w")),
    ("star50", "index", "sh000688", "tencent_kline", ("1d", "1w")),
    ("china_dividend", "index", "sh000015", "tencent_kline", ("1d", "1w")),
    ("nikkei", "index", "^N225", "yahoo_finance_index", ("1d", "1w")),
    ("kospi", "index", "^KS11", "yahoo_finance_index", ("1d", "1w")),
    ("wti", "commodity", "CL=F", "yahoo_finance_futures", ("1d", "1w", "4h")),
    ("gold", "commodity", "GC=F", "yahoo_finance_futures", ("1d", "1w", "4h")),
    ("silver", "commodity", "SI=F", "yahoo_finance_futures", ("1d", "1w", "4h")),
)

_EXPECTED_SOURCES = {
    "dxy": "yahoo_finance_index",
    "us2y": "treasury_official_csv",
    "us10y": "treasury_official_csv",
    "us2s10s": "treasury_official_csv_derived",
    "sp500": "yahoo_finance_index",
    "nasdaq": "yahoo_finance_index",
    "us_dividend": "yahoo_finance_etf",
    "vix": "yahoo_finance_index",
    "bitcoin": "binance_spot_public",
    "shanghai": "tencent_kline",
    "star50": "tencent_kline",
    "china_dividend": "tencent_kline",
    "nikkei": "yahoo_finance_index",
    "kospi": "yahoo_finance_index",
    "wti": "yahoo_finance_futures",
    "gold": "yahoo_finance_futures",
    "silver": "yahoo_finance_futures",
}

_EXPECTED_ASSETS = (
    ("dxy", "index", "DX-Y.NYB", "yahoo_finance_index", ("1d", "1w", "4h")),
    ("us2y", "macro", "DGS2", "treasury_official_csv", ("1d", "1w")),
    ("us10y", "macro", "DGS10", "treasury_official_csv", ("1d", "1w")),
    ("us2s10s", "macro", "T10Y2Y", "treasury_official_csv_derived", ("1d", "1w")),
    ("sp500", "index", "^GSPC", "yahoo_finance_index", ("1d", "1w")),
    ("nasdaq", "index", "^IXIC", "yahoo_finance_index", ("1d", "1w")),
    ("us_dividend", "etf", "SCHD", "yahoo_finance_etf", ("1d", "1w")),
    ("vix", "index", "^VIX", "yahoo_finance_index", ("1d", "1w")),
    ("bitcoin", "crypto", "BTC", "binance_spot_public", ("1d", "1w", "4h")),
    ("shanghai", "index", "sh000001", "tencent_kline", ("1d", "1w")),
    ("star50", "index", "sh000688", "tencent_kline", ("1d", "1w")),
    ("china_dividend", "index", "sh000015", "tencent_kline", ("1d", "1w")),
    ("nikkei", "index", "^N225", "yahoo_finance_index", ("1d", "1w")),
    ("kospi", "index", "^KS11", "yahoo_finance_index", ("1d", "1w")),
    ("wti", "commodity", "CL=F", "yahoo_finance_futures", ("1d", "1w", "4h")),
    ("gold", "commodity", "GC=F", "yahoo_finance_futures", ("1d", "1w", "4h")),
    ("silver", "commodity", "SI=F", "yahoo_finance_futures", ("1d", "1w", "4h")),
)

_EXPECTED_PROVIDER_SYMBOLS = {
    "dxy": "DX-Y.NYB",
    "us2y": "2 Yr",
    "us10y": "10 Yr",
    "us2s10s": "10 Yr-2 Yr",
    "sp500": "^GSPC",
    "nasdaq": "^IXIC",
    "us_dividend": "SCHD",
    "vix": "^VIX",
    "bitcoin": "BTCUSDT",
    "shanghai": "sh000001",
    "star50": "sh000688",
    "china_dividend": "sh000015",
    "nikkei": "^N225",
    "kospi": "^KS11",
    "wti": "CL=F",
    "gold": "GC=F",
    "silver": "SI=F",
}


def required_cells() -> tuple[dict[str, Any], ...]:
    """Return detached matrix cells in stable asset/timeframe order."""

    return tuple(
        {
            "asset_key": asset_key,
            "asset_class": asset_class,
            "ticker": ticker,
            "source": source,
            "timeframe": timeframe,
            **PHASE1_POLICIES,
        }
        for asset_key, asset_class, ticker, source, timeframes in _ASSETS
        for timeframe in timeframes
    )


def expected_provider_symbol(asset_key: str) -> str:
    return _EXPECTED_PROVIDER_SYMBOLS[asset_key]


def validate_matrix() -> None:
    """Fail fast if the committed matrix drifts from the 39-cell contract."""

    cells = required_cells()
    keys = [(cell["asset_key"], cell["timeframe"]) for cell in cells]
    if len(_ASSETS) != 17:
        raise ValueError(f"phase1_asset_count:{len(_ASSETS)}")
    if len(cells) != 39:
        raise ValueError(f"phase1_cell_count:{len(cells)}")
    if len(set(keys)) != len(keys):
        raise ValueError("phase1_duplicate_cells")
    if _ASSETS != _EXPECTED_ASSETS:
        raise ValueError("phase1_canonical_asset_tuple_mismatch")
    actual_sources = {asset[0]: asset[3] for asset in _ASSETS}
    if actual_sources != _EXPECTED_SOURCES:
        raise ValueError("phase1_source_registry_mismatch")
    actual_4h = {asset[0] for asset in _ASSETS if "4h" in asset[4]}
    if actual_4h != {"dxy", "bitcoin", "wti", "gold", "silver"}:
        raise ValueError("phase1_4h_registry_mismatch")


validate_matrix()
