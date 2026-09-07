"""Idempotently merge post-cutover rows from the MVP observer database.

The source database wins on an exact candle identity conflict.  Only
``mvp_candles`` is merged: the other MVP tables contain run-local receipt IDs
and are not safe to copy between independently evolving databases.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Sequence


IDENTITY_COLUMNS = (
    "source_id",
    "instrument_id",
    "timeframe",
    "adjustment_basis",
    "manifest_version",
    "timestamp",
)
DATA_COLUMNS = (
    "display_symbol",
    "provider_symbol",
    "asset_class",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "volume_semantics",
    "is_derived",
    "transform_receipt_id",
    "created_at",
    "updated_at",
)
ALL_COLUMNS = IDENTITY_COLUMNS + DATA_COLUMNS


def _resolved(path: str | Path) -> Path:
    value = Path(path).expanduser().resolve()
    if not value.is_file():
        raise ValueError(f"database does not exist: {value}")
    return value


def _validate_schema(connection: sqlite3.Connection, label: str) -> None:
    rows = connection.execute("PRAGMA table_info(mvp_candles)").fetchall()
    columns = {row[1] for row in rows}
    missing = set(ALL_COLUMNS) - columns
    if missing:
        raise ValueError(f"{label} mvp_candles schema missing: {sorted(missing)}")


def merge_mvp_databases(
    *, source_path: str | Path, target_path: str | Path, since: str
) -> dict[str, Any]:
    source = _resolved(source_path)
    target = _resolved(target_path)
    if source == target:
        raise ValueError("source and target databases must be different")

    source_connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        target_connection = sqlite3.connect(target)
        try:
            source_connection.row_factory = sqlite3.Row
            target_connection.row_factory = sqlite3.Row
            _validate_schema(source_connection, "source")
            _validate_schema(target_connection, "target")
            target_connection.execute("PRAGMA foreign_keys=ON")
            target_connection.execute("ATTACH DATABASE ? AS source_db", (str(source),))
            target_connection.execute("BEGIN IMMEDIATE")
            before = target_connection.execute("SELECT count(*) FROM mvp_candles").fetchone()[0]
            source_rows = target_connection.execute(
                "SELECT * FROM source_db.mvp_candles WHERE timestamp >= ?", (since,)
            ).fetchall()
            inserted = replaced = unchanged = 0
            identity_where = " AND ".join(f"{column} = ?" for column in IDENTITY_COLUMNS)
            insert_columns = ", ".join(ALL_COLUMNS)
            placeholders = ", ".join("?" for _ in ALL_COLUMNS)
            update_set = ", ".join(f"{column} = ?" for column in DATA_COLUMNS)
            for row in source_rows:
                identity_values = tuple(row[column] for column in IDENTITY_COLUMNS)
                existing = target_connection.execute(
                    f"SELECT {', '.join(DATA_COLUMNS)} FROM mvp_candles "
                    f"WHERE {identity_where}",
                    identity_values,
                ).fetchone()
                values = tuple(row[column] for column in ALL_COLUMNS)
                if existing is None:
                    target_connection.execute(
                        f"INSERT INTO mvp_candles ({insert_columns}) VALUES ({placeholders})",
                        values,
                    )
                    inserted += 1
                elif any(existing[column] != row[column] for column in DATA_COLUMNS):
                    target_connection.execute(
                        f"UPDATE mvp_candles SET {update_set} WHERE {identity_where}",
                        tuple(row[column] for column in DATA_COLUMNS) + identity_values,
                    )
                    replaced += 1
                else:
                    unchanged += 1
            target_connection.commit()
            after = target_connection.execute("SELECT count(*) FROM mvp_candles").fetchone()[0]
            receipt = {
                "status": "success",
                "source_database": str(source),
                "target_database": str(target),
                "since": since,
                "source_rows_considered": len(source_rows),
                "merged_rows": inserted + replaced,
                "inserted_rows": inserted,
                "replaced_rows": replaced,
                "conflict_rows": replaced + unchanged,
                "unchanged_conflict_rows": unchanged,
                "target_mvp_candles_before": before,
                "target_mvp_candles_after": after,
                "source_wins_conflicts": True,
                "generated_at": datetime.now(timezone.utc).isoformat(),
            }
            return receipt
        except Exception:
            target_connection.rollback()
            raise
        finally:
            target_connection.close()
    finally:
        source_connection.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Merge MVP candle rows into the canonical database")
    parser.add_argument("--source", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--since", default="2026-09-02T00:00:00+00:00")
    parser.add_argument("--receipt", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    receipt = merge_mvp_databases(source_path=args.source, target_path=args.target, since=args.since)
    rendered = json.dumps(receipt, ensure_ascii=False, sort_keys=True)
    print(rendered)
    if args.receipt:
        args.receipt.expanduser().resolve().write_text(rendered + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
