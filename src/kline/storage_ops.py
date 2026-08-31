"""Safe SSD mount, SQLite backup, NAS verification, and restore operations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import plistlib
import shutil
import sqlite3
import subprocess
from typing import Any, Callable, Mapping
from uuid import uuid4


class StorageOpsError(RuntimeError):
    """An operational storage safety check failed."""


@dataclass(frozen=True)
class MountGuardResult:
    status: str
    mount_path: str
    expected_volume_uuid: str
    observed_volume_uuid: str | None
    filesystem: str | None
    database_root: str
    detail: str

    def to_worker_guard(self):
        from kline.mvp_worker import TargetGuardResult

        return TargetGuardResult(self.status, self.mount_path, self.detail)


CommandRunner = Callable[[list[str]], str]


class SsdMountGuard:
    """Fail closed unless the expected APFS volume is mounted at the target path."""

    def __init__(
        self,
        mount_path: str | Path,
        *,
        expected_volume_uuid: str,
        database_root: str | Path,
        is_mount_fn: Callable[[str], bool] | None = None,
        filesystem_fn: Callable[[str], str] | None = None,
        volume_uuid_fn: Callable[[str], str | None] | None = None,
    ) -> None:
        self.mount_path = Path(mount_path).expanduser()
        self.expected_volume_uuid = expected_volume_uuid.strip().upper()
        self.database_root = Path(database_root).expanduser()
        self._is_mount = is_mount_fn or os.path.ismount
        self._filesystem = filesystem_fn or self._default_filesystem
        self._volume_uuid = volume_uuid_fn or self._default_volume_uuid

    @staticmethod
    def _default_filesystem(path: str) -> str:
        try:
            result = subprocess.run(
                ["stat", "-f", "%T", path],
                check=True,
                capture_output=True,
                text=True,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise StorageOpsError("unable to inspect target filesystem") from exc
        return result.stdout.strip()

    @staticmethod
    def _default_volume_uuid(path: str) -> str | None:
        try:
            result = subprocess.run(
                ["diskutil", "info", "-plist", path],
                check=True,
                capture_output=True,
            )
            payload = plistlib.loads(result.stdout)
        except (OSError, subprocess.CalledProcessError, plistlib.InvalidFileException):
            return None
        for key in ("VolumeUUID", "APFSVolumeUUID", "DiskUUID"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    def check(self) -> MountGuardResult:
        mount = str(self.mount_path.resolve())
        db_root = str(self.database_root.resolve())
        if not self.expected_volume_uuid:
            return MountGuardResult(
                "blocked",
                mount,
                "",
                None,
                None,
                db_root,
                "expected volume UUID is required",
            )
        if not self._is_mount(mount):
            return MountGuardResult(
                "blocked",
                mount,
                self.expected_volume_uuid,
                None,
                None,
                db_root,
                "target volume is not mounted",
            )
        try:
            relative = self.database_root.resolve().relative_to(self.mount_path.resolve())
        except ValueError:
            return MountGuardResult(
                "blocked",
                mount,
                self.expected_volume_uuid,
                None,
                None,
                db_root,
                "database root is outside target volume",
            )
        if str(relative) == ".":
            return MountGuardResult(
                "blocked",
                mount,
                self.expected_volume_uuid,
                None,
                None,
                db_root,
                "database root must be a child of target volume",
            )
        try:
            filesystem = self._filesystem(mount).strip().casefold()
        except StorageOpsError as exc:
            return MountGuardResult(
                "blocked",
                mount,
                self.expected_volume_uuid,
                None,
                None,
                db_root,
                str(exc),
            )
        observed = self._volume_uuid(mount)
        if filesystem != "apfs":
            detail = f"target filesystem is {filesystem or 'unknown'}, expected apfs"
            status = "blocked"
        elif not observed or observed.upper() != self.expected_volume_uuid:
            detail = "target volume UUID does not match expected UUID"
            status = "blocked"
        else:
            detail = "target APFS volume and database root verified"
            status = "ready"
        return MountGuardResult(
            status, mount, self.expected_volume_uuid, observed, filesystem, db_root, detail
        )


@dataclass(frozen=True)
class NasHealthResult:
    status: str
    target_root: str
    filesystem: str | None
    free_bytes: int
    required_bytes: int
    probe_checksum: str | None
    generations: int
    detail: str


class NasVerifier:
    """Verify a NAS backup target without ever opening SQLite on that share."""

    def __init__(
        self,
        target_root: str | Path,
        *,
        required_bytes: int = 0,
        retention_generations: int = 3,
        filesystem_fn: Callable[[str], str | None] | None = None,
        disk_usage_fn: Callable[[str], Any] | None = None,
    ) -> None:
        self.target_root = Path(target_root).expanduser()
        self.required_bytes = max(0, required_bytes)
        self.retention_generations = max(1, retention_generations)
        self._filesystem = filesystem_fn or (lambda _path: None)
        self._disk_usage = disk_usage_fn or shutil.disk_usage

    def check(self) -> NasHealthResult:
        target = str(self.target_root.resolve())
        if not self.target_root.is_dir():
            return NasHealthResult(
                "blocked",
                target,
                None,
                0,
                self.required_bytes,
                None,
                0,
                "NAS target directory is unavailable",
            )
        usage = self._disk_usage(target)
        free_bytes = int(getattr(usage, "free", usage[2] if isinstance(usage, tuple) else 0))
        if free_bytes < self.required_bytes:
            return NasHealthResult(
                "blocked",
                target,
                self._filesystem(target),
                free_bytes,
                self.required_bytes,
                None,
                0,
                "NAS target has insufficient free space",
            )
        probe = self.target_root / f".mvp-write-probe-{uuid4().hex}"
        payload = uuid4().hex.encode("ascii")
        checksum = hashlib.sha256(payload).hexdigest()
        try:
            probe.write_bytes(payload)
            if probe.read_bytes() != payload:
                raise OSError("NAS read-back mismatch")
        except (OSError, ValueError) as exc:
            return NasHealthResult(
                "blocked",
                target,
                self._filesystem(target),
                free_bytes,
                self.required_bytes,
                None,
                0,
                f"NAS write/read-back failed: {exc}",
            )
        finally:
            try:
                probe.unlink()
            except FileNotFoundError:
                pass
        generations = len(list(self.target_root.glob("mvp-backup-*.db")))
        status = "ready" if generations <= self.retention_generations else "retention_overdue"
        detail = (
            "NAS target write/read-back verified"
            if status == "ready"
            else "NAS target exceeds configured retention generations"
        )
        return NasHealthResult(
            status,
            target,
            self._filesystem(target),
            free_bytes,
            self.required_bytes,
            checksum,
            generations,
            detail,
        )

    def verify_backup(
        self, backup_path: str | Path, *, expected_checksum: str | None = None
    ) -> NasHealthResult:
        health = self.check()
        if health.status not in {"ready", "retention_overdue"}:
            return health
        path = Path(backup_path).resolve()
        try:
            path.relative_to(self.target_root.resolve())
        except ValueError:
            return NasHealthResult(
                "blocked",
                health.target_root,
                health.filesystem,
                health.free_bytes,
                health.required_bytes,
                None,
                health.generations,
                "backup is outside configured NAS target",
            )
        if not path.is_file():
            return NasHealthResult(
                "blocked",
                health.target_root,
                health.filesystem,
                health.free_bytes,
                health.required_bytes,
                None,
                health.generations,
                "backup file is missing",
            )
        checksum = _sha256_file(path)
        if expected_checksum and checksum.lower() != expected_checksum.lower():
            return NasHealthResult(
                "blocked",
                health.target_root,
                health.filesystem,
                health.free_bytes,
                health.required_bytes,
                checksum,
                health.generations,
                "backup checksum mismatch",
            )
        return NasHealthResult(
            "ready",
            health.target_root,
            health.filesystem,
            health.free_bytes,
            health.required_bytes,
            checksum,
            health.generations,
            "backup checksum verified",
        )


@dataclass(frozen=True)
class BackupResult:
    backup_id: str
    status: str
    source_db: str
    destination: str
    checksum: str | None
    size_bytes: int
    generation: int
    created_at: str
    detail: str
    restore_verified: bool = False

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()

    def to_storage_receipt(self, *, run_id: str):
        """Convert a verified backup result into the #45 durable receipt type."""

        from kline.storage import BackupReceiptWrite

        if not self.checksum:
            raise StorageOpsError("failed backup has no checksum to persist")
        return BackupReceiptWrite(
            backup_id=self.backup_id,
            run_id=run_id,
            destination=self.destination,
            status=self.status,
            checksum=self.checksum,
            size_bytes=self.size_bytes,
            restore_verified=self.restore_verified,
            policy={"generation": self.generation, "source_db": self.source_db},
        )


class SqliteBackupManager:
    """Use SQLite's online backup API and atomically publish one main DB file."""

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.failpoint: Callable[[str], None] | None = None

    def backup(
        self,
        source_db: str | Path,
        destination_db: str | Path,
        *,
        backup_id: str,
        generation: int,
    ) -> BackupResult:
        source = Path(source_db).expanduser().resolve()
        destination = Path(destination_db).expanduser().resolve()
        created_at = self._clock().astimezone(timezone.utc).replace(microsecond=0).isoformat()
        if not source.is_file():
            return BackupResult(
                backup_id,
                "failed",
                str(source),
                str(destination),
                None,
                0,
                generation,
                created_at,
                "source database is missing",
            )
        if generation < 1:
            return BackupResult(
                backup_id,
                "failed",
                str(source),
                str(destination),
                None,
                0,
                generation,
                created_at,
                "generation must be positive",
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.tmp-{uuid4().hex}")
        try:
            with (
                sqlite3.connect(f"file:{source}?mode=ro", uri=True) as source_connection,
                sqlite3.connect(temporary) as target_connection,
            ):
                source_connection.backup(target_connection)
                target_connection.commit()
                target_connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                target_connection.execute("PRAGMA journal_mode=DELETE")
                integrity = target_connection.execute("PRAGMA integrity_check").fetchone()[0]
                if integrity != "ok":
                    raise StorageOpsError(f"backup integrity check failed: {integrity}")
            if callable(self.failpoint):
                self.failpoint("before_publish")
            os.replace(temporary, destination)
            checksum = _sha256_file(destination)
            return BackupResult(
                backup_id,
                "verified",
                str(source),
                str(destination),
                checksum,
                destination.stat().st_size,
                generation,
                created_at,
                "SQLite online backup and integrity check passed",
            )
        except Exception as exc:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            return BackupResult(
                backup_id,
                "failed",
                str(source),
                str(destination),
                None,
                0,
                generation,
                created_at,
                f"consistent backup failed: {exc}",
            )


@dataclass(frozen=True)
class RestoreResult:
    status: str
    backup_path: str
    restored_path: str | None
    integrity: str
    table_names: tuple[str, ...]
    row_counts: Mapping[str, int]
    latest_closed_bar: str | None
    detail: str


class SqliteRestoreDrill:
    """Restore one backup into a disposable local DB and verify MVP receipts."""

    REQUIRED_TABLES = (
        "mvp_candles",
        "mvp_runs",
        "mvp_watermarks",
        "mvp_source_observations",
        "mvp_quality_receipts",
        "mvp_transform_receipts",
        "mvp_entitlement_receipts",
        "mvp_backup_receipts",
    )

    def restore(
        self,
        backup_path: str | Path,
        restore_root: str | Path,
        *,
        expected_row_counts: Mapping[str, int] | None = None,
    ) -> RestoreResult:
        backup = Path(backup_path).expanduser().resolve()
        root = Path(restore_root).expanduser().resolve()
        if not backup.is_file():
            return RestoreResult(
                "failed", str(backup), None, "missing", (), {}, None, "backup file is missing"
            )
        root.mkdir(parents=True, exist_ok=True)
        restored = root / f"mvp-restore-{uuid4().hex}.db"
        try:
            with (
                sqlite3.connect(f"file:{backup}?mode=ro", uri=True) as source,
                sqlite3.connect(restored) as target,
            ):
                source.backup(target)
                integrity = str(target.execute("PRAGMA integrity_check").fetchone()[0])
                tables = tuple(
                    row[0]
                    for row in target.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                    ).fetchall()
                )
                missing = [name for name in self.REQUIRED_TABLES if name not in tables]
                if integrity != "ok" or missing:
                    raise StorageOpsError(
                        f"restore checks failed: integrity={integrity}, missing={missing}"
                    )
                counts = {
                    name: int(target.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0])
                    for name in self.REQUIRED_TABLES
                }
                if expected_row_counts:
                    for name, expected in expected_row_counts.items():
                        if counts.get(name) != expected:
                            raise StorageOpsError(f"row count mismatch for {name}")
                latest = target.execute("SELECT MAX(timestamp) FROM mvp_candles").fetchone()[0]
            return RestoreResult(
                "verified",
                str(backup),
                str(restored),
                integrity,
                tables,
                counts,
                latest,
                "restore integrity/schema/row-count/receipt checks passed",
            )
        except Exception as exc:
            try:
                restored.unlink()
            except FileNotFoundError:
                pass
            return RestoreResult(
                "failed",
                str(backup),
                str(restored),
                "failed",
                (),
                {},
                None,
                f"restore drill failed: {exc}",
            )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
