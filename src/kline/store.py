"""SQLite storage — save and query candles."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import quote

from sqlalchemy import create_engine, event, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import sessionmaker

from kline.models import (
    AssetClass,
    Base,
    Candle,
    KlineRow,
    RawUpstreamResponse,
    SourceObservation,
    Timeframe,
    MvpBackupReceiptRow,
    MvpCandleRow,
    MvpEntitlementReceiptRow,
    MvpQualityReceiptRow,
    MvpRunRow,
    MvpSourceObservationRow,
    MvpTransformReceiptRow,
    MvpWatermarkRow,
)
from kline.storage import (
    BackupReceiptWrite,
    CandleSeriesKey,
    EntitlementReceiptWrite,
    MvpCandle,
    MvpRunReceipt,
    MvpRunWrite,
    QualityReceiptWrite,
    SourceObservationWrite,
    StorageError,
    TransformReceiptWrite,
    WatermarkState,
    WatermarkWrite,
)

LEGACY_SOURCE_ID = "legacy_unknown"


class KlineStore:
    """Thin wrapper around SQLite for candle CRUD."""

    def __init__(self, db_path: str) -> None:
        self._engine = create_engine(f"sqlite:///{db_path}", echo=False)
        self._mvp_commit_failpoint = None
        # Enable WAL mode for concurrent reads
        event.listen(self._engine, "connect", self._enable_wal)
        Base.metadata.create_all(self._engine)
        self._migrate_source_aware_schema()
        self._normalize_stored_timestamps()
        self._session_factory = sessionmaker(bind=self._engine)

    @staticmethod
    def _enable_wal(dbapi_conn, _connection_record) -> None:
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

    def _migrate_source_aware_schema(self) -> None:
        """Upgrade pre-v0.3 candle stores without guessing their upstream source."""
        expected_lookup = ["source_id", "ticker", "asset_class", "timeframe", "timestamp"]
        expected_ticker_tf = ["source_id", "ticker", "timeframe"]
        with self._engine.begin() as connection:
            columns = {
                row[1] for row in connection.exec_driver_sql("PRAGMA table_info(klines)").fetchall()
            }
            if "source_id" not in columns:
                connection.exec_driver_sql(
                    "ALTER TABLE klines ADD COLUMN source_id VARCHAR "
                    f"NOT NULL DEFAULT '{LEGACY_SOURCE_ID}'"
                )

            for index_name, expected_columns, unique in (
                ("ix_kline_lookup", expected_lookup, True),
                ("ix_kline_ticker_tf", expected_ticker_tf, False),
            ):
                current_columns = [
                    row[2]
                    for row in connection.exec_driver_sql(
                        f"PRAGMA index_info('{index_name}')"
                    ).fetchall()
                ]
                if current_columns and current_columns != expected_columns:
                    connection.exec_driver_sql(f"DROP INDEX IF EXISTS {index_name}")
                unique_sql = "UNIQUE " if unique else ""
                joined = ", ".join(expected_columns)
                connection.exec_driver_sql(
                    f"CREATE {unique_sql}INDEX IF NOT EXISTS {index_name} ON klines ({joined})"
                )

    def _normalize_stored_timestamps(self) -> None:
        """Canonicalize UTC timestamps and merge logically duplicate legacy rows."""
        from datetime import datetime, timezone

        with self._engine.begin() as connection:
            rows = connection.exec_driver_sql(
                "SELECT id, source_id, ticker, asset_class, timeframe, timestamp, "
                "open, high, low, close, volume, amount FROM klines "
                "WHERE timestamp LIKE '%T%' AND timestamp NOT LIKE '%+__:__' "
                "AND timestamp NOT LIKE '%Z'"
            ).fetchall()
            for row in rows:
                parsed = datetime.fromisoformat(str(row[5]))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                canonical = parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat()
                duplicate = connection.exec_driver_sql(
                    "SELECT id FROM klines WHERE source_id=? AND ticker=? AND asset_class=? "
                    "AND timeframe=? AND timestamp=? AND id<>? LIMIT 1",
                    (row[1], row[2], row[3], row[4], canonical, row[0]),
                ).fetchone()
                if duplicate:
                    connection.exec_driver_sql(
                        "UPDATE klines SET open=?, high=?, low=?, close=?, volume=?, amount=? "
                        "WHERE id=?",
                        (row[6], row[7], row[8], row[9], row[10], row[11], duplicate[0]),
                    )
                    connection.exec_driver_sql("DELETE FROM klines WHERE id=?", (row[0],))
                else:
                    connection.exec_driver_sql(
                        "UPDATE klines SET timestamp=? WHERE id=?", (canonical, row[0])
                    )

    def query(
        self,
        ticker: str,
        asset_class: AssetClass,
        timeframe: Timeframe,
        *,
        source_id: str = LEGACY_SOURCE_ID,
        start: str | None = None,
        end: str | None = None,
        limit: int = 500,
    ) -> list[Candle]:
        """Query candles for a ticker. Returns oldest-first."""
        with self._session_factory() as session:
            stmt = (
                select(KlineRow)
                .where(
                    KlineRow.source_id == source_id,
                    KlineRow.ticker == ticker,
                    KlineRow.asset_class == asset_class.value,
                    KlineRow.timeframe == timeframe.value,
                )
                .order_by(KlineRow.timestamp.desc())
                .limit(limit)
            )
            if start:
                stmt = stmt.where(KlineRow.timestamp >= start)
            if end:
                stmt = stmt.where(KlineRow.timestamp <= end)

            rows = session.execute(stmt).scalars().all()
            return [row.to_candle() for row in reversed(rows)]

    def save(
        self,
        ticker: str,
        asset_class: AssetClass,
        timeframe: Timeframe,
        candles: list[Candle],
        *,
        source_id: str = LEGACY_SOURCE_ID,
    ) -> int:
        """Upsert candles. Returns number of rows affected."""
        if not candles:
            return 0

        records = [
            {
                "source_id": source_id,
                "ticker": ticker,
                "asset_class": asset_class.value,
                "timeframe": timeframe.value,
                "timestamp": c.timestamp,
                "open": c.open,
                "high": c.high,
                "low": c.low,
                "close": c.close,
                "volume": c.volume,
                "amount": c.amount,
            }
            for c in candles
        ]

        affected = 0
        with self._session_factory() as session:
            # Keep well below SQLite's build-dependent bound-variable limit.
            for offset in range(0, len(records), 500):
                stmt = sqlite_insert(KlineRow).values(records[offset : offset + 500])
                stmt = stmt.on_conflict_do_update(
                    index_elements=[
                        "source_id",
                        "ticker",
                        "asset_class",
                        "timeframe",
                        "timestamp",
                    ],
                    set_={
                        "open": stmt.excluded.open,
                        "high": stmt.excluded.high,
                        "low": stmt.excluded.low,
                        "close": stmt.excluded.close,
                        "volume": stmt.excluded.volume,
                        "amount": stmt.excluded.amount,
                    },
                )
                affected += session.execute(stmt).rowcount
            session.commit()
            return affected

    def save_raw_response(
        self,
        *,
        provider: str,
        source_mode: str,
        ticker: str,
        asset_class: AssetClass,
        timeframe: Timeframe,
        served_from: str,
        execution_venue: bool,
        request_params: dict[str, Any],
        response_body: Any,
        status_code: int | None = None,
        error: str | None = None,
    ) -> int:
        """Persist raw upstream payloads for debugging source behavior."""
        row = RawUpstreamResponse(
            provider=provider,
            source_mode=source_mode,
            ticker=ticker,
            asset_class=asset_class.value,
            timeframe=timeframe.value,
            served_from=served_from,
            execution_venue=execution_venue,
            request_params=json.dumps(request_params, ensure_ascii=False, sort_keys=True),
            response_body=json.dumps(response_body, ensure_ascii=False, sort_keys=True),
            status_code=status_code,
            error=error,
        )
        with self._session_factory() as session:
            session.add(row)
            session.commit()
            return 1

    def list_tickers(
        self,
        asset_class: AssetClass | None = None,
        *,
        source_id: str | None = None,
    ) -> list[str]:
        """List all tickers with stored data."""
        with self._session_factory() as session:
            stmt = select(KlineRow.ticker).distinct()
            if asset_class:
                stmt = stmt.where(KlineRow.asset_class == asset_class.value)
            if source_id:
                stmt = stmt.where(KlineRow.source_id == source_id)
            return list(session.execute(stmt).scalars().all())

    def count(
        self,
        ticker: str,
        asset_class: AssetClass,
        timeframe: Timeframe,
        *,
        source_id: str = LEGACY_SOURCE_ID,
    ) -> int:
        """Count stored candles for a ticker."""
        from sqlalchemy import func

        with self._session_factory() as session:
            stmt = (
                select(func.count())
                .select_from(KlineRow)
                .where(
                    KlineRow.source_id == source_id,
                    KlineRow.ticker == ticker,
                    KlineRow.asset_class == asset_class.value,
                    KlineRow.timeframe == timeframe.value,
                )
            )
            return session.execute(stmt).scalar_one()

    def source_coverage(self) -> list[dict[str, Any]]:
        """Return source-scoped storage coverage for health and audit surfaces."""
        from sqlalchemy import func

        with self._session_factory() as session:
            rows = session.execute(
                select(
                    KlineRow.source_id,
                    KlineRow.asset_class,
                    KlineRow.ticker,
                    KlineRow.timeframe,
                    func.count().label("count"),
                    func.min(KlineRow.timestamp).label("first_timestamp"),
                    func.max(KlineRow.timestamp).label("latest_timestamp"),
                ).group_by(
                    KlineRow.source_id,
                    KlineRow.asset_class,
                    KlineRow.ticker,
                    KlineRow.timeframe,
                )
            ).all()
            return [
                {
                    "source_id": row.source_id,
                    "asset_class": row.asset_class,
                    "ticker": row.ticker,
                    "timeframe": row.timeframe,
                    "count": row.count,
                    "first_timestamp": row.first_timestamp,
                    "latest_timestamp": row.latest_timestamp,
                }
                for row in rows
            ]

    def save_source_observation(
        self,
        *,
        source_id: str,
        provider: str,
        ticker: str,
        asset_class: AssetClass,
        timeframe: Timeframe,
        success: bool,
        candle_count: int,
        latest_timestamp: str | None,
        latency_ms: float | None,
        quality_flags: list[str],
        error: str | None = None,
        served_from: str = "upstream",
    ) -> int:
        row = SourceObservation(
            source_id=source_id,
            provider=provider,
            ticker=ticker,
            asset_class=asset_class.value,
            timeframe=timeframe.value,
            success=success,
            served_from=served_from,
            candle_count=candle_count,
            latest_timestamp=latest_timestamp,
            latency_ms=latency_ms,
            error=error,
            quality_flags=json.dumps(quality_flags, ensure_ascii=False),
        )
        with self._session_factory() as session:
            session.add(row)
            session.commit()
            return 1

    def latest_source_observations(self) -> list[dict[str, Any]]:
        """Return the latest request result for every source/instrument/timeframe."""
        from sqlalchemy import and_, func

        with self._session_factory() as session:
            latest = (
                select(
                    SourceObservation.source_id,
                    SourceObservation.ticker,
                    SourceObservation.timeframe,
                    func.max(SourceObservation.id).label("latest_id"),
                )
                .group_by(
                    SourceObservation.source_id,
                    SourceObservation.ticker,
                    SourceObservation.timeframe,
                )
                .subquery()
            )
            rows = session.execute(
                select(SourceObservation)
                .join(
                    latest,
                    and_(
                        SourceObservation.source_id == latest.c.source_id,
                        SourceObservation.ticker == latest.c.ticker,
                        SourceObservation.timeframe == latest.c.timeframe,
                        SourceObservation.id == latest.c.latest_id,
                    ),
                )
                .order_by(SourceObservation.source_id, SourceObservation.ticker)
            ).scalars()
            return [
                {
                    "source_id": row.source_id,
                    "provider": row.provider,
                    "asset_class": row.asset_class,
                    "ticker": row.ticker,
                    "timeframe": row.timeframe,
                    "success": row.success,
                    "served_from": row.served_from,
                    "candle_count": row.candle_count,
                    "latest_timestamp": row.latest_timestamp,
                    "latency_ms": row.latency_ms,
                    "error": row.error,
                    "quality_flags": json.loads(row.quality_flags),
                    "observed_at": row.observed_at.isoformat() if row.observed_at else None,
                }
                for row in rows
            ]

    def count_raw_responses(self) -> int:
        """Count captured raw upstream payloads."""
        from sqlalchemy import func

        with self._session_factory() as session:
            stmt = select(func.count()).select_from(RawUpstreamResponse)
            return session.execute(stmt).scalar_one()

    @staticmethod
    def _mvp_key_identity(key: CandleSeriesKey) -> tuple[str, ...]:
        return (
            key.source_id,
            key.instrument_id,
            key.timeframe,
            key.adjustment_basis,
            key.manifest_version,
        )

    @staticmethod
    def _mvp_json(value: Any, *, field_name: str) -> str:
        if not isinstance(value, dict) and not hasattr(value, "items"):
            raise StorageError(f"{field_name} must be JSON object-like")
        try:
            return json.dumps(
                dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
        except (TypeError, ValueError) as exc:
            raise StorageError(f"{field_name} must be JSON serializable") from exc

    @staticmethod
    def _mvp_hash(value: str, *, field_name: str) -> str:
        if not isinstance(value, str) or len(value) != 64:
            raise StorageError(f"{field_name} must be a SHA-256 hex digest")
        lowered = value.lower()
        if any(char not in "0123456789abcdef" for char in lowered):
            raise StorageError(f"{field_name} must be a SHA-256 hex digest")
        return lowered

    @staticmethod
    def _mvp_text(value: Any, *, field_name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise StorageError(f"{field_name} must be a non-empty string")
        return value.strip()

    def _validate_mvp_run(self, write: MvpRunWrite) -> None:
        if not isinstance(write, MvpRunWrite):
            raise StorageError("commit_mvp_run expects MvpRunWrite")
        self._mvp_text(write.run_id, field_name="run_id")
        self._mvp_text(write.manifest_version, field_name="manifest_version")
        self._mvp_hash(write.manifest_hash, field_name="manifest_hash")
        self._mvp_text(write.started_at, field_name="started_at")
        if write.completed_at is not None:
            self._mvp_text(write.completed_at, field_name="completed_at")
        if write.window_start is not None:
            self._mvp_text(write.window_start, field_name="window_start")
        if write.window_end is not None:
            self._mvp_text(write.window_end, field_name="window_end")
        self._mvp_json(write.policy, field_name="policy")
        if write.status not in {"success", "partial", "failed"}:
            raise StorageError("run status must be success, partial, or failed")
        if write.status == "failed":
            if not write.error:
                raise StorageError("failed run requires error")
            if any(
                (
                    write.candles,
                    write.source_observations,
                    write.quality_receipts,
                    write.transform_receipts,
                    write.watermarks,
                )
            ):
                raise StorageError("failed run cannot promote candles or watermarks")

        candle_keys: set[tuple[Any, ...]] = set()
        for candle in write.candles:
            if not isinstance(candle, MvpCandle):
                raise StorageError("candles must contain MvpCandle values")
            if candle.key.manifest_version != write.manifest_version:
                raise StorageError("candle manifest_version does not match run")
            identity = (*self._mvp_key_identity(candle.key), candle.timestamp)
            if identity in candle_keys:
                raise StorageError("duplicate candle identity in one run")
            candle_keys.add(identity)

        for observation in write.source_observations:
            if not isinstance(observation, SourceObservationWrite):
                raise StorageError("source_observations must contain SourceObservationWrite values")
            if observation.run_id != write.run_id:
                raise StorageError("source observation run_id does not match run")
            if not isinstance(observation.success, bool):
                raise StorageError("source observation success must be boolean")
            if observation.key.manifest_version != write.manifest_version:
                raise StorageError("source observation manifest_version does not match run")
            if observation.candle_count < 0:
                raise StorageError("source observation candle_count cannot be negative")
            self._mvp_json(observation.policy, field_name="source observation policy")
            if observation.request_start is not None:
                self._mvp_text(observation.request_start, field_name="request_start")
            if observation.request_end is not None:
                self._mvp_text(observation.request_end, field_name="request_end")
            if observation.response_hash is not None:
                self._mvp_hash(observation.response_hash, field_name="response_hash")
            if observation.latest_timestamp is not None:
                self._mvp_text(observation.latest_timestamp, field_name="latest_timestamp")
            if observation.observed_at is not None:
                self._mvp_text(observation.observed_at, field_name="observed_at")
            self._mvp_text(observation.served_from, field_name="served_from")
            if observation.latency_ms is not None and (
                not isinstance(observation.latency_ms, (int, float)) or observation.latency_ms < 0
            ):
                raise StorageError("source observation latency_ms must be non-negative")

        quality_keys: set[tuple[Any, ...]] = set()
        for quality in write.quality_receipts:
            if not isinstance(quality, QualityReceiptWrite):
                raise StorageError("quality_receipts must contain QualityReceiptWrite values")
            if (
                quality.run_id != write.run_id
                or quality.key.manifest_version != write.manifest_version
            ):
                raise StorageError("quality receipt identity does not match run")
            identity = (*self._mvp_key_identity(quality.key),)
            if identity in quality_keys:
                raise StorageError("duplicate quality receipt identity in one run")
            quality_keys.add(identity)
            if quality.status not in {"pass", "partial", "blocked", "fail"}:
                raise StorageError("quality receipt status is invalid")
            for field_name in ("gaps", "duplicates", "invalid_rows", "blocked_cells"):
                value = getattr(quality, field_name)
                if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                    raise StorageError(f"quality receipt {field_name} must be non-negative integer")
            self._mvp_json(quality.details, field_name="quality details")
            if quality.receipt_hash is not None:
                self._mvp_hash(quality.receipt_hash, field_name="quality receipt_hash")

        transform_keys: set[tuple[Any, ...]] = set()
        for transform in write.transform_receipts:
            if not isinstance(transform, TransformReceiptWrite):
                raise StorageError("transform_receipts must contain TransformReceiptWrite values")
            if (
                transform.run_id != write.run_id
                or transform.manifest_version != write.manifest_version
            ):
                raise StorageError("transform receipt identity does not match run")
            if transform.output_timeframe not in {
                "15m",
                "1h",
                "4h",
                "1d",
                "1w",
            } or transform.input_timeframe not in {
                "15m",
                "1h",
                "4h",
                "1d",
                "1w",
            }:
                raise StorageError("transform receipt timeframe is invalid")
            if transform.output_timeframe == transform.input_timeframe:
                raise StorageError("transform receipt must change timeframe")
            identity = (
                transform.instrument_id,
                transform.source_id,
                transform.output_timeframe,
                transform.input_timeframe,
            )
            if identity in transform_keys:
                raise StorageError("duplicate transform receipt identity in one run")
            transform_keys.add(identity)
            for field_name in ("input_start", "input_end", "aggregation_rule_version"):
                self._mvp_text(getattr(transform, field_name), field_name=field_name)
            self._mvp_hash(transform.input_hash, field_name="input_hash")
            self._mvp_hash(transform.output_hash, field_name="output_hash")
            if transform.bucket_anchor is not None:
                self._mvp_text(transform.bucket_anchor, field_name="bucket_anchor")
            if transform.partial_bucket_policy is not None:
                self._mvp_text(transform.partial_bucket_policy, field_name="partial_bucket_policy")
            if (
                not isinstance(transform.partial_bucket_count, int)
                or isinstance(transform.partial_bucket_count, bool)
                or transform.partial_bucket_count < 0
            ):
                raise StorageError("partial_bucket_count must be a non-negative integer")

        derived_identities = {
            (candle.key.instrument_id, candle.key.source_id, candle.key.timeframe)
            for candle in write.candles
            if candle.is_derived
        }
        receipt_identities = {
            (instrument_id, source_id, output_timeframe)
            for instrument_id, source_id, output_timeframe, _input_timeframe in transform_keys
        }
        if derived_identities - receipt_identities:
            raise StorageError("derived candle requires a matching transform receipt")

        watermark_keys: set[tuple[Any, ...]] = set()
        for watermark in write.watermarks:
            if not isinstance(watermark, WatermarkWrite):
                raise StorageError("watermarks must contain WatermarkWrite values")
            if (
                watermark.run_id != write.run_id
                or watermark.key.manifest_version != write.manifest_version
            ):
                raise StorageError("watermark identity does not match run")
            identity = self._mvp_key_identity(watermark.key)
            if identity in watermark_keys:
                raise StorageError("duplicate watermark identity in one run")
            watermark_keys.add(identity)
            self._mvp_text(watermark.last_closed_timestamp, field_name="last_closed_timestamp")
            if watermark.cursor is not None:
                self._mvp_text(watermark.cursor, field_name="cursor")

        for entitlement in write.entitlement_receipts:
            if not isinstance(entitlement, EntitlementReceiptWrite):
                raise StorageError(
                    "entitlement_receipts must contain EntitlementReceiptWrite values"
                )
            for field_name in ("receipt_id", "source_id", "status", "evidence_ref", "receipt_hash"):
                self._mvp_text(getattr(entitlement, field_name), field_name=field_name)
            self._mvp_hash(entitlement.receipt_hash, field_name="receipt_hash")
            self._mvp_json(entitlement.allowed_history, field_name="allowed_history")
            if not isinstance(entitlement.timeframe_permissions, Sequence) or isinstance(
                entitlement.timeframe_permissions, (str, bytes)
            ):
                raise StorageError("timeframe_permissions must be a sequence")
            if any(
                item not in {"15m", "1h", "4h", "1d", "1w"}
                for item in entitlement.timeframe_permissions
            ):
                raise StorageError("timeframe_permissions contains unsupported timeframe")
            if not all(
                isinstance(value, bool)
                for value in (
                    entitlement.persistence_allowed,
                    entitlement.derived_allowed,
                    entitlement.non_display_allowed,
                )
            ):
                raise StorageError("entitlement permissions must be boolean")

        for backup in write.backup_receipts:
            if not isinstance(backup, BackupReceiptWrite):
                raise StorageError("backup_receipts must contain BackupReceiptWrite values")
            for field_name in ("backup_id", "run_id", "destination", "status", "checksum"):
                self._mvp_text(getattr(backup, field_name), field_name=field_name)
            if backup.run_id != write.run_id:
                raise StorageError("backup receipt run_id does not match run")
            self._mvp_hash(backup.checksum, field_name="checksum")
            if (
                not isinstance(backup.size_bytes, int)
                or isinstance(backup.size_bytes, bool)
                or backup.size_bytes < 0
            ):
                raise StorageError("backup size_bytes must be a non-negative integer")
            if not isinstance(backup.restore_verified, bool):
                raise StorageError("backup restore_verified must be boolean")
            self._mvp_json(backup.policy, field_name="backup policy")

    @staticmethod
    def _mvp_now() -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    @staticmethod
    def _mvp_run_receipt(row: MvpRunRow) -> MvpRunReceipt:
        return MvpRunReceipt(
            run_id=row.run_id,
            status=row.status,
            manifest_version=row.manifest_version,
            manifest_hash=row.manifest_hash,
            candle_count=row.candle_count,
            observation_count=row.observation_count,
            quality_count=row.quality_count,
            transform_count=row.transform_count,
            watermark_count=row.watermark_count,
            committed_at=row.committed_at or "",
        )

    def commit_mvp_run(self, write: MvpRunWrite) -> MvpRunReceipt:
        """Atomically persist a complete MVP run and all of its receipts."""

        self._validate_mvp_run(write)
        committed_at = self._mvp_now()
        receipt_hash = hashlib.sha256(
            json.dumps(
                {
                    "run_id": write.run_id,
                    "manifest_version": write.manifest_version,
                    "manifest_hash": write.manifest_hash,
                    "status": write.status,
                    "candle_count": len(write.candles),
                    "observation_count": len(write.source_observations),
                    "quality_count": len(write.quality_receipts),
                    "transform_count": len(write.transform_receipts),
                    "watermark_count": len(write.watermarks),
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

        with self._session_factory() as session:
            existing = session.get(MvpRunRow, write.run_id)
            if existing is not None:
                session.rollback()
                if (
                    existing.manifest_version != write.manifest_version
                    or existing.manifest_hash != write.manifest_hash
                ):
                    raise StorageError("run_id already belongs to a different manifest")
                if existing.status not in {"success", "partial"}:
                    raise StorageError("run_id already has a failed attempt")
                return self._mvp_run_receipt(existing)
            session.rollback()

            try:
                with session.begin():
                    run_row = MvpRunRow(
                        run_id=write.run_id,
                        manifest_version=write.manifest_version,
                        manifest_hash=write.manifest_hash.lower(),
                        status=write.status,
                        started_at=write.started_at,
                        completed_at=write.completed_at or committed_at,
                        window_start=write.window_start,
                        window_end=write.window_end,
                        policy_json=self._mvp_json(write.policy, field_name="policy"),
                        receipt_hash=receipt_hash,
                        error=write.error,
                        candle_count=len(write.candles),
                        observation_count=len(write.source_observations),
                        quality_count=len(write.quality_receipts),
                        transform_count=len(write.transform_receipts),
                        watermark_count=len(write.watermarks),
                        committed_at=committed_at,
                    )
                    session.add(run_row)

                    for entitlement in write.entitlement_receipts:
                        session.add(
                            MvpEntitlementReceiptRow(
                                receipt_id=entitlement.receipt_id,
                                source_id=entitlement.source_id,
                                status=entitlement.status,
                                allowed_history_json=self._mvp_json(
                                    entitlement.allowed_history, field_name="allowed_history"
                                ),
                                timeframe_permissions_json=json.dumps(
                                    list(entitlement.timeframe_permissions),
                                    ensure_ascii=False,
                                    sort_keys=True,
                                ),
                                persistence_allowed=entitlement.persistence_allowed,
                                derived_allowed=entitlement.derived_allowed,
                                non_display_allowed=entitlement.non_display_allowed,
                                valid_from=entitlement.valid_from,
                                valid_to=entitlement.valid_to,
                                evidence_ref=entitlement.evidence_ref,
                                receipt_hash=entitlement.receipt_hash.lower(),
                            )
                        )

                    transform_rows: dict[tuple[str, str, str], MvpTransformReceiptRow] = {}
                    for transform in write.transform_receipts:
                        row = MvpTransformReceiptRow(
                            run_id=transform.run_id,
                            manifest_version=transform.manifest_version,
                            instrument_id=transform.instrument_id,
                            source_id=transform.source_id,
                            output_timeframe=transform.output_timeframe,
                            input_timeframe=transform.input_timeframe,
                            aggregation_rule_version=transform.aggregation_rule_version,
                            input_start=transform.input_start,
                            input_end=transform.input_end,
                            input_hash=transform.input_hash.lower(),
                            output_hash=transform.output_hash.lower(),
                            bucket_anchor=transform.bucket_anchor,
                            partial_bucket_policy=transform.partial_bucket_policy,
                            partial_bucket_count=transform.partial_bucket_count,
                        )
                        session.add(row)
                        transform_rows[
                            (
                                transform.instrument_id,
                                transform.source_id,
                                transform.output_timeframe,
                            )
                        ] = row
                    session.flush()

                    for candle in write.candles:
                        transform_row = transform_rows.get(
                            (
                                candle.key.instrument_id,
                                candle.key.source_id,
                                candle.key.timeframe,
                            )
                        )
                        transform_receipt_id = candle.transform_receipt_id
                        if candle.is_derived:
                            if transform_row is None:
                                raise StorageError(
                                    "derived candle requires a matching transform receipt"
                                )
                            transform_receipt_id = transform_row.id
                        values = {
                            "instrument_id": candle.key.instrument_id,
                            "display_symbol": candle.key.display_symbol,
                            "provider_symbol": candle.key.provider_symbol,
                            "source_id": candle.key.source_id,
                            "asset_class": candle.key.asset_class,
                            "timeframe": candle.key.timeframe,
                            "adjustment_basis": candle.key.adjustment_basis,
                            "manifest_version": candle.key.manifest_version,
                            "timestamp": candle.timestamp,
                            "open": candle.open,
                            "high": candle.high,
                            "low": candle.low,
                            "close": candle.close,
                            "volume": candle.volume,
                            "amount": candle.amount,
                            "volume_semantics": candle.volume_semantics,
                            "is_derived": candle.is_derived,
                            "transform_receipt_id": transform_receipt_id,
                        }
                        stmt = sqlite_insert(MvpCandleRow).values(values)
                        stmt = stmt.on_conflict_do_update(
                            index_elements=[
                                "source_id",
                                "instrument_id",
                                "timeframe",
                                "adjustment_basis",
                                "manifest_version",
                                "timestamp",
                            ],
                            set_={
                                "display_symbol": stmt.excluded.display_symbol,
                                "provider_symbol": stmt.excluded.provider_symbol,
                                "asset_class": stmt.excluded.asset_class,
                                "open": stmt.excluded.open,
                                "high": stmt.excluded.high,
                                "low": stmt.excluded.low,
                                "close": stmt.excluded.close,
                                "volume": stmt.excluded.volume,
                                "amount": stmt.excluded.amount,
                                "volume_semantics": stmt.excluded.volume_semantics,
                                "is_derived": stmt.excluded.is_derived,
                                "transform_receipt_id": stmt.excluded.transform_receipt_id,
                            },
                        )
                        session.execute(stmt)

                    if callable(self._mvp_commit_failpoint):
                        self._mvp_commit_failpoint("after_candles")

                    for observation in write.source_observations:
                        session.add(
                            MvpSourceObservationRow(
                                run_id=observation.run_id,
                                manifest_version=observation.key.manifest_version,
                                instrument_id=observation.key.instrument_id,
                                display_symbol=observation.key.display_symbol,
                                provider_symbol=observation.key.provider_symbol,
                                source_id=observation.key.source_id,
                                asset_class=observation.key.asset_class,
                                timeframe=observation.key.timeframe,
                                success=observation.success,
                                served_from=observation.served_from,
                                request_start=observation.request_start,
                                request_end=observation.request_end,
                                response_hash=observation.response_hash.lower()
                                if observation.response_hash
                                else None,
                                policy_json=self._mvp_json(
                                    observation.policy, field_name="source observation policy"
                                ),
                                candle_count=observation.candle_count,
                                latest_timestamp=observation.latest_timestamp,
                                latency_ms=observation.latency_ms,
                                error=observation.error,
                                observed_at=observation.observed_at,
                            )
                        )
                    for quality in write.quality_receipts:
                        session.add(
                            MvpQualityReceiptRow(
                                run_id=quality.run_id,
                                manifest_version=quality.key.manifest_version,
                                instrument_id=quality.key.instrument_id,
                                source_id=quality.key.source_id,
                                timeframe=quality.key.timeframe,
                                status=quality.status,
                                gaps=quality.gaps,
                                duplicates=quality.duplicates,
                                invalid_rows=quality.invalid_rows,
                                blocked_cells=quality.blocked_cells,
                                details_json=self._mvp_json(
                                    quality.details, field_name="quality details"
                                ),
                                receipt_hash=quality.receipt_hash.lower()
                                if quality.receipt_hash
                                else None,
                            )
                        )
                    for watermark in write.watermarks:
                        existing_watermark = session.execute(
                            select(MvpWatermarkRow).where(
                                MvpWatermarkRow.source_id == watermark.key.source_id,
                                MvpWatermarkRow.instrument_id == watermark.key.instrument_id,
                                MvpWatermarkRow.timeframe == watermark.key.timeframe,
                                MvpWatermarkRow.adjustment_basis == watermark.key.adjustment_basis,
                                MvpWatermarkRow.manifest_version == watermark.key.manifest_version,
                            )
                        ).scalar_one_or_none()
                        if (
                            existing_watermark is not None
                            and watermark.last_closed_timestamp
                            < existing_watermark.last_closed_timestamp
                        ):
                            raise StorageError("watermark cannot move backwards")
                        if existing_watermark is None:
                            session.add(
                                MvpWatermarkRow(
                                    instrument_id=watermark.key.instrument_id,
                                    display_symbol=watermark.key.display_symbol,
                                    provider_symbol=watermark.key.provider_symbol,
                                    source_id=watermark.key.source_id,
                                    asset_class=watermark.key.asset_class,
                                    timeframe=watermark.key.timeframe,
                                    adjustment_basis=watermark.key.adjustment_basis,
                                    manifest_version=watermark.key.manifest_version,
                                    last_closed_timestamp=watermark.last_closed_timestamp,
                                    cursor=watermark.cursor,
                                    run_id=watermark.run_id,
                                )
                            )
                        else:
                            existing_watermark.display_symbol = watermark.key.display_symbol
                            existing_watermark.provider_symbol = watermark.key.provider_symbol
                            existing_watermark.asset_class = watermark.key.asset_class
                            existing_watermark.last_closed_timestamp = (
                                watermark.last_closed_timestamp
                            )
                            existing_watermark.cursor = watermark.cursor
                            existing_watermark.run_id = watermark.run_id

                    for backup in write.backup_receipts:
                        session.add(
                            MvpBackupReceiptRow(
                                backup_id=backup.backup_id,
                                run_id=backup.run_id,
                                destination=backup.destination,
                                status=backup.status,
                                checksum=backup.checksum.lower(),
                                size_bytes=backup.size_bytes,
                                restore_verified=backup.restore_verified,
                                policy_json=self._mvp_json(
                                    backup.policy, field_name="backup policy"
                                ),
                            )
                        )
                    if callable(self._mvp_commit_failpoint):
                        self._mvp_commit_failpoint("before_commit")
            except StorageError:
                raise
            except Exception as exc:
                raise StorageError(f"MVP run transaction rolled back: {exc}") from exc

            return MvpRunReceipt(
                run_id=write.run_id,
                status=write.status,
                manifest_version=write.manifest_version,
                manifest_hash=write.manifest_hash.lower(),
                candle_count=len(write.candles),
                observation_count=len(write.source_observations),
                quality_count=len(write.quality_receipts),
                transform_count=len(write.transform_receipts),
                watermark_count=len(write.watermarks),
                committed_at=committed_at,
            )

    def query_mvp_candles(
        self,
        key: CandleSeriesKey,
        *,
        start: str | None = None,
        end: str | None = None,
        limit: int = 500,
    ) -> list[MvpCandle]:
        """Read a source-aware MVP series without consulting the legacy table."""

        if limit < 1:
            return []
        with self._session_factory() as session:
            stmt = (
                select(MvpCandleRow)
                .where(
                    MvpCandleRow.source_id == key.source_id,
                    MvpCandleRow.instrument_id == key.instrument_id,
                    MvpCandleRow.timeframe == key.timeframe,
                    MvpCandleRow.adjustment_basis == key.adjustment_basis,
                    MvpCandleRow.manifest_version == key.manifest_version,
                )
                .order_by(MvpCandleRow.timestamp.desc())
                .limit(limit)
            )
            if start is not None:
                stmt = stmt.where(MvpCandleRow.timestamp >= start)
            if end is not None:
                stmt = stmt.where(MvpCandleRow.timestamp <= end)
            rows = list(session.execute(stmt).scalars())
        return [
            MvpCandle(
                key=key,
                timestamp=row.timestamp,
                open=row.open,
                high=row.high,
                low=row.low,
                close=row.close,
                volume=row.volume,
                amount=row.amount,
                volume_semantics=row.volume_semantics,
                is_derived=row.is_derived,
                transform_receipt_id=row.transform_receipt_id,
            )
            for row in reversed(rows)
        ]

    def get_mvp_watermark(self, key: CandleSeriesKey) -> WatermarkState | None:
        """Return the last closed-bar watermark for an exact series key."""

        with self._session_factory() as session:
            row = session.execute(
                select(MvpWatermarkRow).where(
                    MvpWatermarkRow.source_id == key.source_id,
                    MvpWatermarkRow.instrument_id == key.instrument_id,
                    MvpWatermarkRow.timeframe == key.timeframe,
                    MvpWatermarkRow.adjustment_basis == key.adjustment_basis,
                    MvpWatermarkRow.manifest_version == key.manifest_version,
                )
            ).scalar_one_or_none()
        if row is None:
            return None
        return WatermarkState(
            key=key,
            last_closed_timestamp=row.last_closed_timestamp,
            cursor=row.cursor,
            run_id=row.run_id,
        )

    def mvp_storage_health(self) -> dict[str, Any]:
        """Return counts for the MVP serving and receipt tables."""

        tables = {
            "candles": MvpCandleRow,
            "runs": MvpRunRow,
            "watermarks": MvpWatermarkRow,
            "source_observations": MvpSourceObservationRow,
            "quality_receipts": MvpQualityReceiptRow,
            "transform_receipts": MvpTransformReceiptRow,
            "entitlement_receipts": MvpEntitlementReceiptRow,
            "backup_receipts": MvpBackupReceiptRow,
        }
        with self._session_factory() as session:
            counts = {
                name: int(session.execute(select(func.count()).select_from(model)).scalar_one())
                for name, model in tables.items()
            }
        return {"status": "ok", **counts}

    def mvp_duplicate_key_count(self) -> int:
        """Count duplicate canonical candle identities, if a legacy import slipped through."""

        with self._engine.connect() as connection:
            value = connection.exec_driver_sql(
                "SELECT COUNT(*) FROM ("
                "SELECT source_id, instrument_id, timeframe, adjustment_basis, "
                "manifest_version, timestamp FROM mvp_candles "
                "GROUP BY source_id, instrument_id, timeframe, adjustment_basis, "
                "manifest_version, timestamp HAVING COUNT(*) > 1)"
            ).scalar_one()
        return int(value)

    def mvp_watermark_count_for_runs(self, run_ids: set[str]) -> int:
        """Count watermark rows attributed to the supplied failed run IDs."""

        if not run_ids:
            return 0
        with self._session_factory() as session:
            return int(
                session.execute(
                    select(func.count())
                    .select_from(MvpWatermarkRow)
                    .where(MvpWatermarkRow.run_id.in_(sorted(run_ids)))
                ).scalar_one()
            )

    def latest_mvp_run(self) -> dict[str, Any] | None:
        """Return the newest persisted MVP run receipt summary."""

        with self._session_factory() as session:
            row = session.execute(
                select(MvpRunRow)
                .order_by(MvpRunRow.created_at.desc(), MvpRunRow.run_id.desc())
                .limit(1)
            ).scalar_one_or_none()
        if row is None:
            return None
        return {
            "run_id": row.run_id,
            "status": row.status,
            "manifest_version": row.manifest_version,
            "manifest_hash": row.manifest_hash,
            "started_at": row.started_at,
            "completed_at": row.completed_at,
            "window_start": row.window_start,
            "window_end": row.window_end,
            "candle_count": row.candle_count,
            "observation_count": row.observation_count,
            "quality_count": row.quality_count,
            "transform_count": row.transform_count,
            "watermark_count": row.watermark_count,
            "receipt_hash": row.receipt_hash,
            "error": row.error,
        }

    def latest_mvp_runs(self, *, limit: int = 6) -> list[dict[str, Any]]:
        """Return recent MVP run receipts for the health matrix timeline."""

        bounded_limit = max(1, min(int(limit), 100))
        with self._session_factory() as session:
            rows = session.execute(
                select(MvpRunRow)
                .order_by(MvpRunRow.started_at.desc(), MvpRunRow.run_id.desc())
                .limit(bounded_limit)
            ).scalars()
            return [
                {
                    "run_id": row.run_id,
                    "status": row.status,
                    "manifest_version": row.manifest_version,
                    "manifest_hash": row.manifest_hash,
                    "started_at": row.started_at,
                    "completed_at": row.completed_at,
                    "window_start": row.window_start,
                    "window_end": row.window_end,
                    "candle_count": row.candle_count,
                    "observation_count": row.observation_count,
                    "quality_count": row.quality_count,
                    "transform_count": row.transform_count,
                    "watermark_count": row.watermark_count,
                    "receipt_hash": row.receipt_hash,
                    "error": row.error,
                }
                for row in rows
            ]

    def latest_mvp_source_observations(self) -> list[dict[str, Any]]:
        """Return the latest source observation for every exact MVP cell."""

        with self._session_factory() as session:
            rows = session.execute(
                select(MvpSourceObservationRow).order_by(MvpSourceObservationRow.id.desc())
            ).scalars()
            seen: set[tuple[str, str, str, str]] = set()
            result: list[dict[str, Any]] = []
            for row in rows:
                identity = (row.source_id, row.instrument_id, row.timeframe, row.manifest_version)
                if identity in seen:
                    continue
                seen.add(identity)
                result.append(
                    {
                        "run_id": row.run_id,
                        "manifest_version": row.manifest_version,
                        "instrument_id": row.instrument_id,
                        "display_symbol": row.display_symbol,
                        "provider_symbol": row.provider_symbol,
                        "source_id": row.source_id,
                        "asset_class": row.asset_class,
                        "timeframe": row.timeframe,
                        "success": row.success,
                        "served_from": row.served_from,
                        "request_start": row.request_start,
                        "request_end": row.request_end,
                        "response_hash": row.response_hash,
                        "candle_count": row.candle_count,
                        "latest_timestamp": row.latest_timestamp,
                        "latency_ms": row.latency_ms,
                        "error": row.error,
                        "observed_at": row.observed_at,
                        "policy": json.loads(row.policy_json or "{}"),
                    }
                )
            return result

    def latest_mvp_quality_receipts(self) -> list[dict[str, Any]]:
        """Return the latest quality receipt for every exact MVP cell."""

        with self._session_factory() as session:
            rows = session.execute(
                select(MvpQualityReceiptRow).order_by(MvpQualityReceiptRow.id.desc())
            ).scalars()
            seen: set[tuple[str, str, str, str]] = set()
            result: list[dict[str, Any]] = []
            for row in rows:
                identity = (row.source_id, row.instrument_id, row.timeframe, row.manifest_version)
                if identity in seen:
                    continue
                seen.add(identity)
                result.append(
                    {
                        "run_id": row.run_id,
                        "manifest_version": row.manifest_version,
                        "instrument_id": row.instrument_id,
                        "source_id": row.source_id,
                        "timeframe": row.timeframe,
                        "status": row.status,
                        "gaps": row.gaps,
                        "duplicates": row.duplicates,
                        "invalid_rows": row.invalid_rows,
                        "blocked_cells": row.blocked_cells,
                        "details": json.loads(row.details_json or "{}"),
                        "receipt_hash": row.receipt_hash,
                    }
                )
            return result

    def latest_mvp_entitlement_receipts(self) -> list[dict[str, Any]]:
        """Return the newest entitlement receipt for each source."""

        with self._session_factory() as session:
            rows = session.execute(
                select(MvpEntitlementReceiptRow).order_by(
                    MvpEntitlementReceiptRow.created_at.desc()
                )
            ).scalars()
            seen: set[str] = set()
            result: list[dict[str, Any]] = []
            for row in rows:
                if row.source_id in seen:
                    continue
                seen.add(row.source_id)
                result.append(
                    {
                        "receipt_id": row.receipt_id,
                        "source_id": row.source_id,
                        "status": row.status,
                        "allowed_history": json.loads(row.allowed_history_json or "{}"),
                        "timeframe_permissions": json.loads(row.timeframe_permissions_json or "[]"),
                        "persistence_allowed": row.persistence_allowed,
                        "derived_allowed": row.derived_allowed,
                        "non_display_allowed": row.non_display_allowed,
                        "valid_from": row.valid_from,
                        "valid_to": row.valid_to,
                        "evidence_ref": row.evidence_ref,
                        "receipt_hash": row.receipt_hash,
                    }
                )
            return result

    def latest_mvp_transform_receipts(self) -> list[dict[str, Any]]:
        """Return the latest transform receipt for every derived cell."""

        with self._session_factory() as session:
            rows = session.execute(
                select(MvpTransformReceiptRow).order_by(MvpTransformReceiptRow.id.desc())
            ).scalars()
            seen: set[tuple[str, str, str, str]] = set()
            result: list[dict[str, Any]] = []
            for row in rows:
                identity = (
                    row.source_id,
                    row.instrument_id,
                    row.output_timeframe,
                    row.manifest_version,
                )
                if identity in seen:
                    continue
                seen.add(identity)
                result.append(
                    {
                        "id": row.id,
                        "receipt_id": row.id,
                        "run_id": row.run_id,
                        "manifest_version": row.manifest_version,
                        "instrument_id": row.instrument_id,
                        "source_id": row.source_id,
                        "output_timeframe": row.output_timeframe,
                        "input_timeframe": row.input_timeframe,
                        "aggregation_rule_version": row.aggregation_rule_version,
                        "input_start": row.input_start,
                        "input_end": row.input_end,
                        "input_hash": row.input_hash,
                        "output_hash": row.output_hash,
                        "bucket_anchor": row.bucket_anchor,
                        "partial_bucket_policy": row.partial_bucket_policy,
                        "partial_bucket_count": row.partial_bucket_count,
                    }
                )
            return result

    def latest_mvp_watermarks(self) -> list[dict[str, Any]]:
        """Return the latest watermark for every exact MVP cell."""

        with self._session_factory() as session:
            rows = session.execute(select(MvpWatermarkRow)).scalars()
            return [
                {
                    "instrument_id": row.instrument_id,
                    "display_symbol": row.display_symbol,
                    "provider_symbol": row.provider_symbol,
                    "source_id": row.source_id,
                    "asset_class": row.asset_class,
                    "timeframe": row.timeframe,
                    "adjustment_basis": row.adjustment_basis,
                    "manifest_version": row.manifest_version,
                    "last_closed_timestamp": row.last_closed_timestamp,
                    "cursor": row.cursor,
                    "run_id": row.run_id,
                }
                for row in rows
            ]

    def mvp_latest_closed_bars(self) -> list[dict[str, Any]]:
        """Return latest timestamp/row count for each source-aware MVP series."""

        latest = (
            select(
                MvpCandleRow.source_id,
                MvpCandleRow.instrument_id,
                MvpCandleRow.timeframe,
                MvpCandleRow.adjustment_basis,
                MvpCandleRow.manifest_version,
                func.max(MvpCandleRow.timestamp).label("latest_timestamp"),
                func.count().label("row_count"),
            )
            .group_by(
                MvpCandleRow.source_id,
                MvpCandleRow.instrument_id,
                MvpCandleRow.timeframe,
                MvpCandleRow.adjustment_basis,
                MvpCandleRow.manifest_version,
            )
            .subquery()
        )
        with self._session_factory() as session:
            rows = session.execute(select(latest)).all()
        return [
            {
                "source_id": row.source_id,
                "instrument_id": row.instrument_id,
                "timeframe": row.timeframe,
                "adjustment_basis": row.adjustment_basis,
                "manifest_version": row.manifest_version,
                "latest_timestamp": row.latest_timestamp,
                "row_count": row.row_count,
            }
            for row in rows
        ]

    def mvp_quality_summary(self) -> dict[str, Any]:
        """Summarize persisted quality outcomes for the health surface."""

        with self._session_factory() as session:
            rows = session.execute(
                select(
                    MvpQualityReceiptRow.status,
                    func.count().label("count"),
                    func.coalesce(func.sum(MvpQualityReceiptRow.gaps), 0).label("gaps"),
                    func.coalesce(func.sum(MvpQualityReceiptRow.duplicates), 0).label("duplicates"),
                    func.coalesce(func.sum(MvpQualityReceiptRow.blocked_cells), 0).label(
                        "blocked_cells"
                    ),
                ).group_by(MvpQualityReceiptRow.status)
            ).all()
        return {
            "by_status": {
                row.status: {
                    "count": int(row.count),
                    "gaps": int(row.gaps),
                    "duplicates": int(row.duplicates),
                    "blocked_cells": int(row.blocked_cells),
                }
                for row in rows
            }
        }

    def latest_mvp_backup(self) -> dict[str, Any] | None:
        """Return the newest backup receipt without exposing database paths."""

        with self._session_factory() as session:
            row = session.execute(
                select(MvpBackupReceiptRow)
                .order_by(
                    MvpBackupReceiptRow.created_at.desc(), MvpBackupReceiptRow.backup_id.desc()
                )
                .limit(1)
            ).scalar_one_or_none()
        if row is None:
            return None
        return {
            "backup_id": row.backup_id,
            "run_id": row.run_id,
            "status": row.status,
            "checksum": row.checksum,
            "size_bytes": row.size_bytes,
            "restore_verified": row.restore_verified,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }

    def storage_health(self) -> dict[str, Any]:
        """Return an owner-side integrity receipt without exposing the database path."""
        with self._engine.connect() as connection:
            integrity = str(connection.exec_driver_sql("PRAGMA integrity_check").scalar_one())
            candle_rows = int(
                connection.exec_driver_sql("SELECT COUNT(*) FROM klines").scalar_one()
            )
            source_rows = int(
                connection.exec_driver_sql(
                    "SELECT COUNT(DISTINCT source_id) FROM klines"
                ).scalar_one()
            )
        return {
            "status": "ok" if integrity == "ok" else "error",
            "integrity": integrity,
            "candle_rows": candle_rows,
            "source_count": source_rows,
        }


class KlineReadOnlyStore(KlineStore):
    """Open an existing SQLite database without schema, WAL, or migration writes."""

    def __init__(self, db_path: str) -> None:
        path = Path(db_path).expanduser().resolve()
        if not path.is_file():
            raise StorageError(f"read-only database does not exist: {path}")
        encoded = quote(str(path), safe="/")
        self._engine = create_engine(
            f"sqlite:///file:{encoded}?mode=ro&uri=true",
            echo=False,
            connect_args={"uri": True},
        )
        event.listen(self._engine, "connect", self._enable_query_only)
        self._mvp_commit_failpoint = None
        self._session_factory = sessionmaker(bind=self._engine)

    @staticmethod
    def _enable_query_only(dbapi_conn, _connection_record) -> None:
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA query_only=ON")
        cursor.close()

    @staticmethod
    def _write_blocked(operation: str) -> None:
        raise StorageError(f"read-only store forbids {operation}")

    def save(self, *_args: Any, **_kwargs: Any) -> int:
        self._write_blocked("save")

    def save_raw_response(self, **_kwargs: Any) -> int:
        self._write_blocked("save_raw_response")

    def save_source_observation(self, **_kwargs: Any) -> int:
        self._write_blocked("save_source_observation")

    def commit_mvp_run(self, _write: MvpRunWrite) -> MvpRunReceipt:
        self._write_blocked("commit_mvp_run")
