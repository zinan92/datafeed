"""SQLite storage — save and query candles."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import create_engine, event, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import sessionmaker

from kline.models import AssetClass, Base, Candle, KlineRow, RawUpstreamResponse, Timeframe


class KlineStore:
    """Thin wrapper around SQLite for candle CRUD."""

    def __init__(self, db_path: str) -> None:
        self._engine = create_engine(f"sqlite:///{db_path}", echo=False)
        # Enable WAL mode for concurrent reads
        event.listen(self._engine, "connect", self._enable_wal)
        Base.metadata.create_all(self._engine)
        self._session_factory = sessionmaker(bind=self._engine)

    @staticmethod
    def _enable_wal(dbapi_conn, _connection_record) -> None:
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

    def query(
        self,
        ticker: str,
        asset_class: AssetClass,
        timeframe: Timeframe,
        *,
        start: str | None = None,
        end: str | None = None,
        limit: int = 500,
    ) -> list[Candle]:
        """Query candles for a ticker. Returns oldest-first."""
        with self._session_factory() as session:
            stmt = (
                select(KlineRow)
                .where(
                    KlineRow.ticker == ticker,
                    KlineRow.asset_class == asset_class.value,
                    KlineRow.timeframe == timeframe.value,
                )
                .order_by(KlineRow.timestamp.asc())
                .limit(limit)
            )
            if start:
                stmt = stmt.where(KlineRow.timestamp >= start)
            if end:
                stmt = stmt.where(KlineRow.timestamp <= end)

            rows = session.execute(stmt).scalars().all()
            return [row.to_candle() for row in rows]

    def save(
        self,
        ticker: str,
        asset_class: AssetClass,
        timeframe: Timeframe,
        candles: list[Candle],
    ) -> int:
        """Upsert candles. Returns number of rows affected."""
        if not candles:
            return 0

        records = [
            {
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

        with self._session_factory() as session:
            stmt = sqlite_insert(KlineRow).values(records)
            stmt = stmt.on_conflict_do_update(
                index_elements=["ticker", "asset_class", "timeframe", "timestamp"],
                set_={
                    "open": stmt.excluded.open,
                    "high": stmt.excluded.high,
                    "low": stmt.excluded.low,
                    "close": stmt.excluded.close,
                    "volume": stmt.excluded.volume,
                    "amount": stmt.excluded.amount,
                },
            )
            result = session.execute(stmt)
            session.commit()
            return result.rowcount

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

    def list_tickers(self, asset_class: AssetClass | None = None) -> list[str]:
        """List all tickers with stored data."""
        with self._session_factory() as session:
            stmt = select(KlineRow.ticker).distinct()
            if asset_class:
                stmt = stmt.where(KlineRow.asset_class == asset_class.value)
            return list(session.execute(stmt).scalars().all())

    def count(self, ticker: str, asset_class: AssetClass, timeframe: Timeframe) -> int:
        """Count stored candles for a ticker."""
        from sqlalchemy import func

        with self._session_factory() as session:
            stmt = (
                select(func.count())
                .select_from(KlineRow)
                .where(
                    KlineRow.ticker == ticker,
                    KlineRow.asset_class == asset_class.value,
                    KlineRow.timeframe == timeframe.value,
                )
            )
            return session.execute(stmt).scalar_one()

    def count_raw_responses(self) -> int:
        """Count captured raw upstream payloads."""
        from sqlalchemy import func

        with self._session_factory() as session:
            stmt = select(func.count()).select_from(RawUpstreamResponse)
            return session.execute(stmt).scalar_one()
