from __future__ import annotations

from pathlib import Path

import pytest

from kline.cutover import CutoverError, perform_cutover, perform_rollback
from kline.storage_ops import SsdMountGuard


def _guard(root: Path) -> SsdMountGuard:
    return SsdMountGuard(
        root,
        expected_volume_uuid="SSD-UUID",
        database_root=root / "market-data",
        is_mount_fn=lambda _: True,
        filesystem_fn=lambda _: "apfs",
        volume_uuid_fn=lambda _: "SSD-UUID",
    )


def test_controlled_cutover_and_rollback_verify_identity_rows_and_health(tmp_path: Path) -> None:
    from tests.test_storage_ops import _seed_database

    source = tmp_path / "source.db"
    _seed_database(source)
    ssd = tmp_path / "ssd"
    ssd.mkdir()
    target = ssd / "market-data" / "mvp.db"
    rollback = tmp_path / "rollback" / "prior.db"
    manifest_hash = "a" * 64

    receipt = perform_cutover(
        source,
        target,
        rollback_path=rollback,
        guard=_guard(ssd),
        manifest_version="mvp_universe_v1",
        manifest_hash=manifest_hash,
        process_owner="mvp-worker-test",
        health_probe=lambda path: {
            "status": "partial",
            "database_path": str(path),
            "manifest_hash": manifest_hash,
        },
    )
    assert receipt.status == "verified"
    assert receipt.resident_untouched is True
    assert target.exists()
    assert source.exists()
    assert not Path(str(target) + "-wal").exists()
    assert not Path(str(target) + "-shm").exists()

    rollback_receipt = perform_rollback(
        target,
        rollback,
        guard=_guard(ssd),
        manifest_version="mvp_universe_v1",
        manifest_hash=manifest_hash,
        process_owner="mvp-worker-test",
    )
    assert rollback_receipt.status == "verified"
    assert rollback.exists()


def test_cutover_refuses_guard_failure_overwrite_and_resident_mutation(tmp_path: Path) -> None:
    from tests.test_storage_ops import _seed_database

    source = tmp_path / "source.db"
    _seed_database(source)
    target = tmp_path / "ssd" / "market-data" / "mvp.db"
    guard = _guard(tmp_path / "ssd")

    with pytest.raises(CutoverError, match="SSD guard blocked"):
        perform_cutover(
            source,
            target,
            rollback_path=tmp_path / "rollback.db",
            guard=SsdMountGuard(
                tmp_path / "ssd",
                expected_volume_uuid="SSD-UUID",
                database_root=tmp_path / "ssd" / "market-data",
                is_mount_fn=lambda _: False,
            ),
            manifest_version="mvp_universe_v1",
            manifest_hash="b" * 64,
            process_owner="test",
        )

    target.parent.mkdir(parents=True)
    target.write_bytes(b"existing")
    with pytest.raises(CutoverError, match="already exists"):
        perform_cutover(
            source,
            target,
            rollback_path=tmp_path / "rollback.db",
            guard=guard,
            manifest_version="mvp_universe_v1",
            manifest_hash="b" * 64,
            process_owner="test",
        )

    with pytest.raises(CutoverError, match="resident service"):
        perform_cutover(
            source,
            tmp_path / "ssd" / "market-data" / "another.db",
            rollback_path=tmp_path / "rollback.db",
            guard=guard,
            manifest_version="mvp_universe_v1",
            manifest_hash="b" * 64,
            process_owner="test",
            resident_untouched=False,
        )
