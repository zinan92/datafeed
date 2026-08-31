from __future__ import annotations

from collections import namedtuple
from pathlib import Path

from kline.storage_ops import (
    NasVerifier,
    SsdMountGuard,
    SqliteBackupManager,
    SqliteRestoreDrill,
)
from kline.store import KlineStore


Usage = namedtuple("Usage", "total used free")


def _seed_database(path: Path) -> KlineStore:
    from tests.test_mvp_storage import _candle, _key, _run

    store = KlineStore(str(path))
    key = _key()
    store.commit_mvp_run(_run(run_id="run-backup", key=key, candles=(_candle(key),)))
    return store


def test_ssd_mount_guard_fail_closed_for_absent_wrong_and_valid_targets(tmp_path: Path) -> None:
    mount = tmp_path / "ssd"
    db_root = mount / "market-data"
    absent = SsdMountGuard(
        mount,
        expected_volume_uuid="ABC",
        database_root=db_root,
        is_mount_fn=lambda _: False,
    ).check()
    assert absent.status == "blocked"
    assert "not mounted" in absent.detail

    wrong_fs = SsdMountGuard(
        mount,
        expected_volume_uuid="ABC",
        database_root=db_root,
        is_mount_fn=lambda _: True,
        filesystem_fn=lambda _: "hfs",
        volume_uuid_fn=lambda _: "ABC",
    ).check()
    assert wrong_fs.status == "blocked"
    assert "expected apfs" in wrong_fs.detail

    mount.mkdir()
    ready = SsdMountGuard(
        mount,
        expected_volume_uuid="ABC",
        database_root=db_root,
        is_mount_fn=lambda _: True,
        filesystem_fn=lambda _: "APFS",
        volume_uuid_fn=lambda _: "abc",
    ).check()
    assert ready.status == "ready"
    assert ready.to_worker_guard().status == "ready"


def test_ssd_mount_guard_rejects_database_root_outside_volume(tmp_path: Path) -> None:
    mount = tmp_path / "ssd"
    mount.mkdir()
    result = SsdMountGuard(
        mount,
        expected_volume_uuid="ABC",
        database_root=tmp_path / "elsewhere",
        is_mount_fn=lambda _: True,
        filesystem_fn=lambda _: "apfs",
        volume_uuid_fn=lambda _: "ABC",
    ).check()
    assert result.status == "blocked"
    assert "outside target volume" in result.detail


def test_nas_verifier_checks_space_write_readback_and_checksum(tmp_path: Path) -> None:
    nas = tmp_path / "nas"
    nas.mkdir()
    ready = NasVerifier(
        nas,
        required_bytes=100,
        retention_generations=2,
        filesystem_fn=lambda _: "smbfs",
        disk_usage_fn=lambda _: Usage(1000, 100, 900),
    ).check()
    assert ready.status == "ready"
    assert ready.probe_checksum

    backup = nas / "mvp-backup-001.db"
    backup.write_bytes(b"backup")
    verified = NasVerifier(nas, disk_usage_fn=lambda _: Usage(1000, 100, 900)).verify_backup(backup)
    assert verified.status == "ready"
    mismatch = NasVerifier(nas, disk_usage_fn=lambda _: Usage(1000, 100, 900)).verify_backup(
        backup, expected_checksum="0" * 64
    )
    assert mismatch.status == "blocked"
    assert "checksum mismatch" in mismatch.detail

    insufficient = NasVerifier(
        nas, required_bytes=901, disk_usage_fn=lambda _: Usage(1000, 100, 900)
    ).check()
    assert insufficient.status == "blocked"
    assert "insufficient" in insufficient.detail


def test_sqlite_online_backup_and_restore_drill_verify_mvp_schema(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    _seed_database(source)
    destination = tmp_path / "nas" / "mvp-backup-001.db"
    manager = SqliteBackupManager()
    result = manager.backup(source, destination, backup_id="backup-001", generation=1)

    assert result.status == "verified"
    assert result.checksum and result.size_bytes > 0
    assert not destination.with_suffix(destination.suffix + "-wal").exists()
    receipt = result.to_storage_receipt(run_id="run-backup")
    assert receipt.restore_verified is False
    assert receipt.checksum == result.checksum

    restored = SqliteRestoreDrill().restore(
        destination,
        tmp_path / "restore",
        expected_row_counts={"mvp_candles": 1, "mvp_runs": 1},
    )
    assert restored.status == "verified"
    assert restored.integrity == "ok"
    assert restored.latest_closed_bar
    assert "mvp_transform_receipts" in restored.table_names


def test_backup_interruption_is_typed_failure_and_does_not_publish(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    _seed_database(source)
    destination = tmp_path / "nas" / "mvp-backup-002.db"
    manager = SqliteBackupManager()
    manager.failpoint = lambda stage: (
        (_ for _ in ()).throw(RuntimeError("interrupted")) if stage == "before_publish" else None
    )
    result = manager.backup(source, destination, backup_id="backup-002", generation=2)

    assert result.status == "failed"
    assert "interrupted" in result.detail
    assert not destination.exists()


def test_restore_rejects_corrupt_or_wrong_row_count_backup(tmp_path: Path) -> None:
    backup = tmp_path / "corrupt.db"
    backup.write_bytes(b"not sqlite")
    result = SqliteRestoreDrill().restore(backup, tmp_path / "restore")
    assert result.status == "failed"

    source = tmp_path / "source.db"
    _seed_database(source)
    destination = tmp_path / "backup.db"
    verified = SqliteBackupManager().backup(
        source, destination, backup_id="backup-003", generation=3
    )
    assert verified.status == "verified"
    wrong = SqliteRestoreDrill().restore(
        destination,
        tmp_path / "restore-wrong",
        expected_row_counts={"mvp_candles": 99},
    )
    assert wrong.status == "failed"
    assert "row count mismatch" in wrong.detail
