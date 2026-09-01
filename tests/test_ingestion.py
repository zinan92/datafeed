from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kline.free_source_profile import apply_free_source_profile
from kline.ingestion import IngestionError, IngestionOrchestrator, IngestionPlan
from kline.models import Candle, Timeframe, TimeframeTransform
from kline.mvp_manifest import load_manifest
from kline.ports import FetchReceipt
from kline.providers.base import ProviderError
from kline.storage import CandleSeriesKey
from kline.store import KlineStore


MANIFEST_PATH = Path(__file__).parents[1] / "configs" / "mvp_manifest.json"


def test_ingestion_watermark_overlap_supports_one_hour() -> None:
    assert IngestionOrchestrator._interval("1h") == timedelta(hours=1)


class FakeCryptoAdapter:
    async def fetch_candles_with_receipt(
        self,
        ticker: str,
        timeframe: Timeframe,
        *,
        start: str | None,
        end: str | None,
        limit: int,
    ) -> FetchReceipt:
        timestamps = {
            Timeframe.MIN_15: "2026-08-31T00:00:00+00:00",
            Timeframe.HOUR_4: "2026-08-31T00:00:00+00:00",
            Timeframe.DAY: "2026-08-31T00:00:00+00:00",
            Timeframe.WEEK: "2026-08-28T00:00:00+00:00",
        }
        candle = Candle(
            timestamp=timestamps[timeframe],
            open=100.0,
            high=102.0,
            low=99.0,
            close=101.0,
            volume=10.0,
        )
        transform = None
        if timeframe == Timeframe.HOUR_4:
            transform = TimeframeTransform(
                raw_timeframe=Timeframe.MIN_15,
                timeframe_origin="aggregated",
                aggregation={
                    "rule": "utc_fixed_4h_v1",
                    "bucket_anchor": "00:00",
                    "partial_bucket_policy": "drop_and_record",
                    "partial_bucket_count": 0,
                },
            )
        elif timeframe == Timeframe.WEEK:
            transform = TimeframeTransform(
                raw_timeframe=Timeframe.DAY,
                timeframe_origin="aggregated",
                aggregation={
                    "rule": "completed_local_calendar_week_v1",
                    "bucket_anchor": "local_week",
                    "partial_bucket_policy": "defer_until_closed",
                    "partial_bucket_count": 0,
                },
            )
        return FetchReceipt(
            candles=[candle],
            timeframe_transform=transform,
            source_identity={"provider_symbol": ticker},
            raw_response={"row_count": 1, "request": {"start": start, "end": end}},
        )


@pytest.mark.asyncio
async def test_run_once_promotes_ready_crypto_cells_and_keeps_blocked_cells_explicit(
    tmp_path: Path,
) -> None:
    manifest = load_manifest(MANIFEST_PATH)
    store = KlineStore(str(tmp_path / "ingestion.db"))
    adapter = FakeCryptoAdapter()

    def resolve(instrument):
        return adapter if instrument.instrument_id.startswith("CRYPTO.") else None

    plan = IngestionPlan(
        manifest=manifest,
        run_id="run-ingestion-1",
        now=datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc),
        history_start="2026-08-01T00:00:00+00:00",
        fetch_limit=10,
    )
    receipt = await IngestionOrchestrator(store, adapter_resolver=resolve).run_once(plan)

    assert receipt.status == "partial"
    assert receipt.manifest_hash
    assert receipt.row_counts["promoted_candles"] == 12
    assert receipt.row_counts["watermarks"] == 12
    assert any(cell["status"] == "blocked_for_entitlement" for cell in receipt.blocked_cells)
    assert any(cell.status == "not_applicable" for cell in receipt.requested_cells)
    key = CandleSeriesKey(
        instrument_id="CRYPTO.PERP.BTC",
        display_symbol="BTC",
        provider_symbol="BTC",
        source_id="hyperliquid_perpetual_public",
        asset_class="crypto",
        timeframe="4h",
        adjustment_basis="raw_unadjusted",
        manifest_version=manifest.version,
    )
    assert len(store.query_mvp_candles(key)) == 1
    assert store.get_mvp_watermark(key) is not None
    assert store.mvp_storage_health()["transform_receipts"] == 6
    observations = store.latest_mvp_source_observations()
    assert any(
        item["policy"].get("source_identity", {}).get("provider_symbol") == "BTC"
        for item in observations
    )


@pytest.mark.asyncio
async def test_run_once_uses_watermark_overlap_and_is_idempotent(tmp_path: Path) -> None:
    manifest = load_manifest(MANIFEST_PATH)
    store = KlineStore(str(tmp_path / "idempotent.db"))
    adapter = FakeCryptoAdapter()

    def resolve(instrument):
        return adapter if instrument.instrument_id == "CRYPTO.PERP.BTC" else None

    orchestrator = IngestionOrchestrator(store, adapter_resolver=resolve)
    first = await orchestrator.run_once(
        IngestionPlan(
            manifest=manifest,
            run_id="run-overlap-1",
            now=datetime(2026, 9, 1, 12, tzinfo=timezone.utc),
            history_start="2026-08-01T00:00:00+00:00",
            fetch_limit=10,
        )
    )
    second = await orchestrator.run_once(
        IngestionPlan(
            manifest=manifest,
            run_id="run-overlap-2",
            now=datetime(2026, 9, 1, 12, tzinfo=timezone.utc),
            history_start="2026-08-01T00:00:00+00:00",
            overlap_bars=2,
            fetch_limit=10,
        )
    )
    assert first.storage_receipt.candle_count == second.storage_receipt.candle_count == 4
    assert second.row_counts["promoted_candles"] == 4
    assert store.mvp_storage_health()["candles"] == 4


@pytest.mark.asyncio
async def test_run_once_atomic_failure_leaves_rerun_point(tmp_path: Path) -> None:
    manifest = load_manifest(MANIFEST_PATH)
    store = KlineStore(str(tmp_path / "failure.db"))
    adapter = FakeCryptoAdapter()

    def resolve(instrument):
        return adapter if instrument.instrument_id == "CRYPTO.PERP.BTC" else None

    store._mvp_commit_failpoint = lambda stage: (
        (_ for _ in ()).throw(RuntimeError("forced storage interruption"))
        if stage == "after_candles"
        else None
    )
    with pytest.raises(IngestionError, match="atomic storage promotion failed"):
        await IngestionOrchestrator(store, adapter_resolver=resolve).run_once(
            IngestionPlan(
                manifest=manifest,
                run_id="run-failure",
                now=datetime(2026, 9, 1, 12, tzinfo=timezone.utc),
                fetch_limit=10,
            )
        )
    assert store.mvp_storage_health()["runs"] == 0
    assert store.mvp_storage_health()["candles"] == 0


@pytest.mark.asyncio
async def test_run_once_rate_budget_blocks_remaining_required_cells(tmp_path: Path) -> None:
    manifest = load_manifest(MANIFEST_PATH)
    store = KlineStore(str(tmp_path / "budget.db"))
    adapter = FakeCryptoAdapter()

    def resolve(instrument):
        return adapter if instrument.instrument_id == "CRYPTO.PERP.BTC" else None

    receipt = await IngestionOrchestrator(store, adapter_resolver=resolve).run_once(
        IngestionPlan(
            manifest=manifest,
            run_id="run-budget",
            now=datetime(2026, 9, 1, 12, tzinfo=timezone.utc),
            rate_budget=1,
            fetch_limit=10,
        )
    )
    assert receipt.status == "partial"
    assert any(cell.status == "blocked_rate_budget" for cell in receipt.requested_cells)


@pytest.mark.asyncio
async def test_intraday_source_failure_still_persists_daily_and_weekly_data(
    tmp_path: Path,
) -> None:
    manifest = apply_free_source_profile(load_manifest(MANIFEST_PATH))
    store = KlineStore(str(tmp_path / "daily-weekly-fallback.db"))

    class IntradayUnavailableAdapter:
        def __init__(self) -> None:
            self.calls: list[Timeframe] = []

        async def fetch_candles_with_receipt(
            self,
            _ticker: str,
            timeframe: Timeframe,
            *,
            start: str | None,
            end: str,
            limit: int,
        ) -> FetchReceipt:
            del start, end, limit
            self.calls.append(timeframe)
            if timeframe in {Timeframe.MIN_15, Timeframe.HOUR_1, Timeframe.HOUR_4}:
                raise ProviderError("intraday endpoint unavailable")
            timestamp = (
                "2026-08-31T00:00:00+00:00"
                if timeframe == Timeframe.DAY
                else "2026-08-28T00:00:00+00:00"
            )
            transform = (
                TimeframeTransform(
                    raw_timeframe=Timeframe.DAY,
                    timeframe_origin="aggregated",
                    aggregation={
                        "rule": "completed_local_calendar_week_v1",
                        "bucket_anchor": "local_week",
                        "partial_bucket_policy": "defer_until_closed",
                        "partial_bucket_count": 0,
                    },
                )
                if timeframe == Timeframe.WEEK
                else None
            )
            return FetchReceipt(
                candles=[
                    Candle(
                        timestamp=timestamp,
                        open=100,
                        high=102,
                        low=99,
                        close=101,
                        volume=10,
                    )
                ],
                timeframe_transform=transform,
                source_identity={"selected_source": "daily-weekly-fallback-test"},
                raw_response={"row_count": 1},
            )

    adapter = IntradayUnavailableAdapter()
    receipt = await IngestionOrchestrator(store, adapter_resolver=lambda _instrument: adapter).run_once(
        IngestionPlan(
            manifest=manifest,
            run_id="run-daily-weekly-fallback",
            now=datetime(2026, 9, 1, 12, tzinfo=timezone.utc),
            fetch_limit=10,
            instrument_ids=("CN.A.600519",),
        )
    )

    statuses = {cell.timeframe: cell.status for cell in receipt.requested_cells}
    assert receipt.status == "partial"
    assert statuses["15m"] == "provider_error"
    assert statuses["1h"] == "provider_error"
    assert statuses["4h"] == "provider_error"
    assert statuses["1d"] == "ready"
    assert statuses["1w"] == "ready"
    assert adapter.calls[:2] == [Timeframe.DAY, Timeframe.WEEK]
    latest = store.mvp_latest_closed_bars()
    assert {row["timeframe"] for row in latest} == {"1d", "1w"}
