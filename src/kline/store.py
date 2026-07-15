"""SQLite storage — save and query candles."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import create_engine, event, select
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
)

LEGACY_SOURCE_ID = "legacy_unknown"


class KlineStore:
    """Thin wrapper around SQLite for candle CRUD."""

    def __init__(self, db_path: str) -> None:
        self._engine = create_engine(f"sqlite:///{db_path}", echo=False)
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
                    f"CREATE {unique_sql}INDEX IF NOT EXISTS {index_name} "
                    f"ON klines ({joined})"
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

    def storage_health(self) -> dict[str, Any]:
        """Return an owner-side integrity receipt without exposing the database path."""
        with self._engine.connect() as connection:
            integrity = str(connection.exec_driver_sql("PRAGMA integrity_check").scalar_one())
            candle_rows = int(connection.exec_driver_sql("SELECT COUNT(*) FROM klines").scalar_one())
            source_rows = int(
                connection.exec_driver_sql("SELECT COUNT(DISTINCT source_id) FROM klines").scalar_one()
            )
        return {
            "status": "ok" if integrity == "ok" else "error",
            "integrity": integrity,
            "candle_rows": candle_rows,
            "source_count": source_rows,
        }
