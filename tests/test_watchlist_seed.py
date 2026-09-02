from datetime import datetime, timezone
from pathlib import Path

import pytest

from kline.models import Candle, Timeframe, TimeframeTransform
from kline.ports import FetchReceipt
from kline.store import KlineStore
from kline.watchlist_manifest import load_watchlist_manifest
from ops.mvp_stock_seed import SAFE_OBSERVER_DB
from ops.watchlist_seed import (
    MARKET_DATA_DB,
    WATCHLIST_LOCK,
    execute_watchlist_batches,
    validate_watchlist_target,
)


MANIFEST_PATH = Path(__file__).parents[1] / "configs" / "watchlist_manifest.json"


class _DailyAdapter:
    async def fetch_candles_with_receipt(
        self,
        ticker: str,
        timeframe: Timeframe,
        **_kwargs,
    ) -> FetchReceipt:
        assert timeframe == Timeframe.DAY
        return FetchReceipt(
            candles=[
                Candle(
                    timestamp="2026-09-01",
                    open=100,
                    high=102,
                    low=99,
                    close=101,
                    volume=1000,
                )
            ],
            timeframe_transform=TimeframeTransform(
                raw_timeframe=Timeframe.DAY,
                timeframe_origin="native",
                aggregation={"kind": "none", "rule": "native_passthrough"},
            ),
            source_identity={"provider_symbol": ticker},
            raw_response={"http_status": 200, "row_count": 1},
            attempts=(
                {
                    "source": "fixture",
                    "status": "success",
                    "http_status": 200,
                    "latency_ms": 1.0,
                },
            ),
        )


def test_watchlist_target_is_exact_and_rejects_screening_database() -> None:
    assert validate_watchlist_target(MARKET_DATA_DB, WATCHLIST_LOCK) == (
        MARKET_DATA_DB,
        WATCHLIST_LOCK,
    )
    with pytest.raises(ValueError, match="persistent Market Data Database"):
        validate_watchlist_target(SAFE_OBSERVER_DB, WATCHLIST_LOCK)
    with pytest.raises(ValueError, match="dedicated Watchlist lock"):
        validate_watchlist_target(MARKET_DATA_DB, SAFE_OBSERVER_DB.with_name("mvp-worker.lock"))


@pytest.mark.asyncio
async def test_watchlist_runner_persists_all_daily_members_without_screening_profile(
    tmp_path: Path,
) -> None:
    manifest = load_watchlist_manifest(MANIFEST_PATH)
    store = KlineStore(str(tmp_path / "watchlist.db"))
    adapter = _DailyAdapter()

    report = await execute_watchlist_batches(
        manifest=manifest,
        store=store,
        lock_path=tmp_path / "watchlist.lock",
        adapter_resolver=lambda _instrument: adapter,
        now=datetime(2026, 9, 2, 12, tzinfo=timezone.utc),
        batch_size=10,
        request_interval_seconds=0,
    )

    assert report["status"] == "success"
    assert report["instrument_count"] == 42
    assert report["persisted_instrument_count"] == 42
    assert report["remaining_after"] == []
    assert report["timeframes"] == ["1d"]
    assert report["batch_count"] == 5
    persisted = store.mvp_latest_closed_bars()
    assert {row["instrument_id"] for row in persisted} == {
        item.instrument_id for item in manifest.instruments
    }
    assert {row["manifest_version"] for row in persisted} == {manifest.version}
    assert {row["timeframe"] for row in persisted} == {"1d"}
