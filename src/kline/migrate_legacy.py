"""One-time import of proven-source rows from the old trading market database."""

from __future__ import annotations

import argparse
import sqlite3
from collections import Counter
from pathlib import Path

from kline.config import get_settings
from kline.models import AssetClass, Candle, Timeframe
from kline.store import KlineStore

_SOURCE_MAP = {
    "binance_usdm": ("binance_usdm_futures", AssetClass.COMMODITY),
    "tiger_openapi:COMEX": ("tiger_openapi_comex", AssetClass.COMMODITY),
    "fred:DTWEXBGS": ("fred_public_csv_macro", AssetClass.MACRO),
    "fred:DFII10": ("fred_public_csv_macro", AssetClass.MACRO),
    "fred:GVZCLS": ("fred_public_csv_flow", AssetClass.FLOW),
    "fred:DFF": ("fred_public_csv_event", AssetClass.EVENT),
}
_SYMBOL_MAP = {
    ("binance_usdm_futures", "GOLD"): "XAUUSDT",
    ("fred_public_csv_macro", "DXY"): "DTWEXBGS",
    ("fred_public_csv_macro", "US10Y_REAL"): "DFII10",
    ("fred_public_csv_flow", "GLD_FLOW"): "GVZCLS",
    ("fred_public_csv_event", "FED_CPI_EVENTS"): "DFF",
}
_ASSET_BY_SOURCE = {source_id: asset_class for source_id, asset_class in _SOURCE_MAP.values()}


def import_legacy_market_db(
    source_db: Path,
    *,
    target_db: Path | None = None,
    batch_size: int = 10_000,
) -> dict:
    target = target_db or Path(get_settings().db_path)
    store = KlineStore(str(target))
    imported: Counter[str] = Counter()
    skipped: Counter[str] = Counter()

    source_uri = f"file:{source_db.resolve()}?mode=ro"
    with sqlite3.connect(source_uri, uri=True) as connection:
        cursor = connection.execute(
            "SELECT symbol, timeframe, timestamp, open, high, low, close, volume, provider "
            "FROM bars ORDER BY provider, symbol, timeframe, timestamp"
        )
        group_key: tuple[str, str, str] | None = None
        candles: list[Candle] = []

        def flush() -> None:
            nonlocal candles
            if not group_key or not candles:
                return
            source_id, symbol, timeframe_value = group_key
            asset_class = _ASSET_BY_SOURCE[source_id]
            store.save(
                _SYMBOL_MAP.get((source_id, symbol), symbol),
                asset_class,
                Timeframe(timeframe_value),
                candles,
                source_id=source_id,
            )
            imported[f"{source_id}:{symbol}:{timeframe_value}"] += len(candles)
            candles = []

        for row in cursor:
            provider = str(row[8])
            mapping = _SOURCE_MAP.get(provider)
            if mapping is None:
                skipped[provider] += 1
                continue
            source_id, _asset_class = mapping
            try:
                Timeframe(str(row[1]))
            except ValueError:
                skipped[f"{provider}:unsupported_timeframe:{row[1]}"] += 1
                continue
            key = (source_id, str(row[0]), str(row[1]))
            if group_key != key or len(candles) >= batch_size:
                flush()
                group_key = key
            candles.append(
                Candle(
                    timestamp=str(row[2]),
                    open=float(row[3]),
                    high=float(row[4]),
                    low=float(row[5]),
                    close=float(row[6]),
                    volume=float(row[7] or 0),
                )
            )
        flush()

    return {
        "source_db": str(source_db),
        "target_db": str(target),
        "imported": dict(imported),
        "imported_rows": sum(imported.values()),
        "skipped": dict(skipped),
        "skipped_rows": sum(skipped.values()),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_db", type=Path)
    parser.add_argument("--target-db", type=Path)
    args = parser.parse_args()
    result = import_legacy_market_db(args.source_db, target_db=args.target_db)
    for key, value in result.items():
        print(f"{key}={value}")


if __name__ == "__main__":
    main()
