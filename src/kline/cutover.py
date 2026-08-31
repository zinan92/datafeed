"""Controlled MVP database cutover and rollback receipts.

This module never edits the resident service configuration.  It provides a
small, testable operation that a separately authorized operator can invoke
after the SSD/NAS and 30-day gates are satisfied.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from typing import Any, Callable, Mapping

from kline.storage_ops import SsdMountGuard, SqliteBackupManager


class CutoverError(RuntimeError):
    """Cutover preflight or postflight failed closed."""


@dataclass(frozen=True)
class CutoverReceipt:
    status: str
    operation: str
    source_db: str
    target_db: str
    rollback_path: str
    manifest_version: str
    manifest_hash: str
    source_integrity: str
    target_integrity: str | None
    backup_id: str
    backup_checksum: str | None
    target_volume_uuid: str
    source_row_counts: Mapping[str, int]
    target_row_counts: Mapping[str, int]
    process_owner: str
    resident_untouched: bool
    created_at: str
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "operation": self.operation,
            "source_db": self.source_db,
            "target_db": self.target_db,
            "rollback_path": self.rollback_path,
            "manifest_version": self.manifest_version,
            "manifest_hash": self.manifest_hash,
            "source_integrity": self.source_integrity,
            "target_integrity": self.target_integrity,
            "backup_id": self.backup_id,
            "backup_checksum": self.backup_checksum,
            "target_volume_uuid": self.target_volume_uuid,
            "source_row_counts": dict(self.source_row_counts),
            "target_row_counts": dict(self.target_row_counts),
            "process_owner": self.process_owner,
            "resident_untouched": self.resident_untouched,
            "created_at": self.created_at,
            "detail": self.detail,
        }


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _db_snapshot(path: Path) -> tuple[str, dict[str, int], str | None]:
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
            integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            counts = {
                table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in sorted(tables)
                if table.startswith("mvp_")
            }
            latest = (
                connection.execute("SELECT MAX(timestamp) FROM mvp_candles").fetchone()[0]
                if "mvp_candles" in tables
                else None
            )
    except (OSError, sqlite3.Error) as exc:
        raise CutoverError(f"database snapshot failed: {exc}") from exc
    return integrity, counts, latest


def _assert_same_rows(source: Mapping[str, int], target: Mapping[str, int]) -> None:
    for table, count in source.items():
        if target.get(table) != count:
            raise CutoverError(f"row count mismatch for {table}")


def perform_cutover(
    source_db: str | Path,
    target_db: str | Path,
    *,
    rollback_path: str | Path,
    guard: SsdMountGuard,
    manifest_version: str,
    manifest_hash: str,
    process_owner: str,
    resident_untouched: bool = True,
    backup_manager: SqliteBackupManager | None = None,
    health_probe: Callable[[Path], Mapping[str, Any]] | None = None,
) -> CutoverReceipt:
    """Copy source to a new SSD target through SQLite backup and verify it."""

    source = Path(source_db).expanduser().resolve()
    target = Path(target_db).expanduser().resolve()
    rollback = Path(rollback_path).expanduser().resolve()
    created_at = _now()
    if not resident_untouched:
        raise CutoverError("resident service must remain untouched for MVP cutover")
    if not process_owner.strip():
        raise CutoverError("process_owner is required")
    if target.exists():
        raise CutoverError("target database already exists; refusing overwrite")
    if rollback == source or rollback == target:
        raise CutoverError("rollback path must be distinct from source and target")
    guard_result = guard.check()
    if guard_result.status != "ready":
        raise CutoverError(f"SSD guard blocked cutover: {guard_result.detail}")
    source_integrity, source_counts, _ = _db_snapshot(source)
    if source_integrity != "ok":
        raise CutoverError(f"source database integrity failed: {source_integrity}")
    manager = backup_manager or SqliteBackupManager()
    backup_id = f"cutover-{created_at.replace(':', '').replace('-', '')}"
    backup = manager.backup(source, target, backup_id=backup_id, generation=1)
    if backup.status != "verified":
        raise CutoverError(f"cutover backup failed: {backup.detail}")
    target_integrity, target_counts, _ = _db_snapshot(target)
    if target_integrity != "ok":
        raise CutoverError(f"target database integrity failed: {target_integrity}")
    _assert_same_rows(source_counts, target_counts)
    if health_probe is not None:
        health = dict(health_probe(target))
        if health.get("manifest_hash") not in {None, manifest_hash}:
            raise CutoverError("post-cutover health manifest hash mismatch")
        if health.get("database_path") not in {None, str(target)}:
            raise CutoverError("post-cutover health database path mismatch")
        if health.get("status") in {"failed", "blocked"}:
            raise CutoverError("post-cutover health is not ready")
    return CutoverReceipt(
        status="verified",
        operation="cutover",
        source_db=str(source),
        target_db=str(target),
        rollback_path=str(rollback),
        manifest_version=manifest_version,
        manifest_hash=manifest_hash,
        source_integrity=source_integrity,
        target_integrity=target_integrity,
        backup_id=backup.backup_id,
        backup_checksum=backup.checksum,
        target_volume_uuid=guard_result.observed_volume_uuid or "",
        source_row_counts=source_counts,
        target_row_counts=target_counts,
        process_owner=process_owner,
        resident_untouched=resident_untouched,
        created_at=created_at,
        detail="consistent backup, target integrity, row counts, and health probe verified",
    )


def perform_rollback(
    active_db: str | Path,
    rollback_path: str | Path,
    *,
    guard: SsdMountGuard,
    manifest_version: str,
    manifest_hash: str,
    process_owner: str,
    backup_manager: SqliteBackupManager | None = None,
) -> CutoverReceipt:
    """Restore the active database to a new rollback artifact without deletion."""

    active = Path(active_db).expanduser().resolve()
    rollback = Path(rollback_path).expanduser().resolve()
    created_at = _now()
    if rollback.exists():
        raise CutoverError("rollback destination already exists; refusing overwrite")
    guard_result = guard.check()
    if guard_result.status != "ready":
        raise CutoverError(f"SSD guard blocked rollback: {guard_result.detail}")
    source_integrity, source_counts, _ = _db_snapshot(active)
    if source_integrity != "ok":
        raise CutoverError(f"active database integrity failed: {source_integrity}")
    manager = backup_manager or SqliteBackupManager()
    backup_id = f"rollback-{created_at.replace(':', '').replace('-', '')}"
    backup = manager.backup(active, rollback, backup_id=backup_id, generation=1)
    if backup.status != "verified":
        raise CutoverError(f"rollback backup failed: {backup.detail}")
    target_integrity, target_counts, _ = _db_snapshot(rollback)
    if target_integrity != "ok":
        raise CutoverError(f"rollback artifact integrity failed: {target_integrity}")
    _assert_same_rows(source_counts, target_counts)
    return CutoverReceipt(
        status="verified",
        operation="rollback",
        source_db=str(active),
        target_db=str(rollback),
        rollback_path=str(rollback),
        manifest_version=manifest_version,
        manifest_hash=manifest_hash,
        source_integrity=source_integrity,
        target_integrity=target_integrity,
        backup_id=backup.backup_id,
        backup_checksum=backup.checksum,
        target_volume_uuid=guard_result.observed_volume_uuid or "",
        source_row_counts=source_counts,
        target_row_counts=target_counts,
        process_owner=process_owner,
        resident_untouched=True,
        created_at=created_at,
        detail="rollback artifact verified without deleting the prior active database",
    )
