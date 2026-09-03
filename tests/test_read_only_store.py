from pathlib import Path

import pytest

from kline.store import KlineReadOnlyStore, KlineStore, StorageError


def test_read_only_store_rejects_missing_database_without_creating_it(tmp_path: Path) -> None:
    path = tmp_path / "missing.db"

    with pytest.raises(StorageError, match="does not exist"):
        KlineReadOnlyStore(str(path))

    assert not path.exists()


def test_read_only_store_can_read_without_schema_or_wal_writes(tmp_path: Path) -> None:
    path = tmp_path / "existing.db"
    writer = KlineStore(str(path))
    writer._engine.dispose()
    before = path.stat().st_mtime_ns
    store = KlineReadOnlyStore(str(path))
    assert store.mvp_storage_health()["status"] == "ok"
    assert path.stat().st_mtime_ns == before
    with store._engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA query_only").scalar_one() == 1


def test_read_only_store_rejects_mutating_methods(tmp_path: Path) -> None:
    path = tmp_path / "guarded.db"
    writer = KlineStore(str(path))
    writer._engine.dispose()
    store = KlineReadOnlyStore(str(path))

    with pytest.raises(StorageError, match="forbids save"):
        store.save("AAPL", "us_stock", "1d", [])
