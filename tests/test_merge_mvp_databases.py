from pathlib import Path
import sqlite3

from ops.merge_mvp_databases import ALL_COLUMNS, merge_mvp_databases


def _database(path: Path, rows: list[tuple]) -> None:
    columns = ", ".join(f"{column} TEXT" for column in ALL_COLUMNS)
    with sqlite3.connect(path) as connection:
        connection.execute(f"CREATE TABLE mvp_candles ({columns})")
        connection.execute(
            "CREATE UNIQUE INDEX uq_mvp_candle_identity ON mvp_candles "
            "(source_id, instrument_id, timeframe, adjustment_basis, manifest_version, timestamp)"
        )
        connection.executemany(
            f"INSERT INTO mvp_candles ({', '.join(ALL_COLUMNS)}) VALUES ({', '.join('?' for _ in ALL_COLUMNS)})",
            rows,
        )


def _row(timestamp: str, close: str) -> tuple:
    values = {column: "v" for column in ALL_COLUMNS}
    values.update(
        source_id="source", instrument_id="WATCH.X", timeframe="1d",
        adjustment_basis="raw", manifest_version="v1", timestamp=timestamp,
        close=close,
    )
    return tuple(values[column] for column in ALL_COLUMNS)


def test_merge_is_idempotent_and_source_wins_conflicts(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    target = tmp_path / "target.db"
    _database(source, [_row("2026-09-02T00:00:00+00:00", "source"), _row("2026-09-03T00:00:00+00:00", "new")])
    _database(target, [_row("2026-09-02T00:00:00+00:00", "target")])

    first = merge_mvp_databases(source_path=source, target_path=target, since="2026-09-02T00:00:00+00:00")
    second = merge_mvp_databases(source_path=source, target_path=target, since="2026-09-02T00:00:00+00:00")

    assert first["merged_rows"] == 2
    assert first["conflict_rows"] == 1
    assert second["merged_rows"] == 0
    assert second["conflict_rows"] == 2
    with sqlite3.connect(target) as connection:
        assert connection.execute("SELECT count(*) FROM mvp_candles").fetchone()[0] == 2
        assert connection.execute("SELECT close FROM mvp_candles WHERE timestamp LIKE '2026-09-02%'").fetchone()[0] == "source"


def test_merge_excludes_rows_before_cutoff(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    target = tmp_path / "target.db"
    _database(source, [_row("2026-09-01T00:00:00+00:00", "old")])
    _database(target, [])
    receipt = merge_mvp_databases(source_path=source, target_path=target, since="2026-09-02T00:00:00+00:00")
    assert receipt["source_rows_considered"] == 0
    assert receipt["target_mvp_candles_after"] == 0
