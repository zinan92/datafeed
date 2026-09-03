from __future__ import annotations

from pathlib import Path

import pytest

from ops.consumer_cutover_backfill import (
    CONSUMER_DAILY_INSTRUMENT_IDS,
    MARKET_DATA_DB,
    WATCHLIST_LOCK,
    run_consumer_cutover_backfill,
)


def test_consumer_cutover_backfill_targets_only_approved_watchlist_daily_series() -> None:
    assert len(CONSUMER_DAILY_INSTRUMENT_IDS) == 10
    assert len(set(CONSUMER_DAILY_INSTRUMENT_IDS)) == 10
    assert all(value.startswith("WATCH.CROSS.") for value in CONSUMER_DAILY_INSTRUMENT_IDS)
    assert "WATCH.CROSS.VIX" not in CONSUMER_DAILY_INSTRUMENT_IDS
    assert not any("TREASURY" in value for value in CONSUMER_DAILY_INSTRUMENT_IDS)


@pytest.mark.asyncio
async def test_consumer_cutover_backfill_rejects_noncanonical_database(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="persistent Market Data Database"):
        await run_consumer_cutover_backfill(
            db_path=tmp_path / "wrong.db",
            lock_path=WATCHLIST_LOCK,
        )


@pytest.mark.asyncio
async def test_consumer_cutover_backfill_rejects_short_history_request() -> None:
    with pytest.raises(ValueError, match="1008-row request"):
        await run_consumer_cutover_backfill(
            db_path=MARKET_DATA_DB,
            lock_path=WATCHLIST_LOCK,
            fetch_limit=1007,
        )
