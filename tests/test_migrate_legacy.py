import sqlite3
from pathlib import Path

from kline.migrate_legacy import import_legacy_market_db
from kline.models import AssetClass, Timeframe
from kline.store import KlineStore


def test_migration_imports_only_rows_with_proven_source(tmp_path: Path):
    source = tmp_path / "market.db"
    with sqlite3.connect(source) as connection:
        connection.execute(
            "CREATE TABLE bars (symbol, timeframe, timestamp, open, high, low, close, "
            "volume, provider)"
        )
        connection.executemany(
            "INSERT INTO bars VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("GOLD", "1m", "2026-07-15T01:00:00+00:00", 1, 2, 0, 1, 10, "binance_usdm"),
                ("UNKNOWN", "1d", "2026-07-15", 1, 2, 0, 1, 10, "unknown_provider"),
            ],
        )
    target = tmp_path / "datafeed.db"
    result = import_legacy_market_db(source, target_db=target)

    assert result["imported_rows"] == 1
    assert result["skipped"] == {"unknown_provider": 1}
    store = KlineStore(str(target))
    assert store.count(
        "XAUUSDT",
        AssetClass.COMMODITY,
        Timeframe.MIN_1,
        source_id="binance_usdm_futures",
    ) == 1
