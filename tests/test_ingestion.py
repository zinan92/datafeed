from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from kline.free_source_profile import apply_free_source_profile
from kline.ingestion import IngestionError, IngestionOrchestrator, IngestionPlan
from kline.models import AssetClass, Candle, Timeframe, TimeframeTransform
from kline.mvp_manifest import load_manifest
from kline.ports import FetchReceipt, ProviderBackedMarketDataAdapter
from kline.providers.base import ProviderError
from kline.providers.free_ashare import AShareFreeProvider
from kline.provenance import source_manifest
from kline.storage import CandleSeriesKey
from kline.store import KlineStore


MANIFEST_PATH = Path(__file__).parents[1] / "configs" / "mvp_manifest.json"


def test_ingestion_watermark_overlap_supports_one_hour() -> None:
    assert IngestionOrchestrator._interval("1h") == timedelta(hours=1)


@pytest.mark.asyncio
async def test_run_once_marks_cn_a_opening_forming_bars_partial_inside_buffer(
    tmp_path: Path,
) -> None:
    manifest = apply_free_source_profile(load_manifest(MANIFEST_PATH))
    store = KlineStore(str(tmp_path / "opening-buffer.db"))

    class OpeningAdapter:
        async def fetch_candles_with_receipt(
            self,
            _ticker: str,
            _timeframe: Timeframe,
            *,
            start: str | None,
            end: str,
            limit: int,
        ) -> FetchReceipt:
            del start, end, limit
            return FetchReceipt(
                candles=[
                    Candle(
                        timestamp="2026-09-02T01:30:00+00:00",
                        open=100,
                        high=102,
                        low=99,
                        close=101,
                        volume=10,
                    )
                ],
                timeframe_transform=None,
                source_identity={"selected_source": "opening-test"},
                raw_response={"row_count": 1},
            )

    receipt = await IngestionOrchestrator(
        store, adapter_resolver=lambda _instrument: OpeningAdapter()
    ).run_once(
        IngestionPlan(
            manifest=manifest,
            run_id="run-opening-buffer",
            now=datetime(2026, 9, 2, 1, 35, tzinfo=timezone.utc),
            instrument_ids=("CN.A.600519",),
            timeframes=("15m", "1h"),
            market_open_buffer_minutes=10,
        )
    )

    assert [cell.status for cell in receipt.requested_cells] == ["partial", "partial"]
    assert receipt.quality["partial"] == 2
    assert receipt.quality["fail"] == 0
    assert receipt.row_counts["promoted_candles"] == 0


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
async def test_run_once_accepts_coarse_first_phase_selection(tmp_path: Path) -> None:
    manifest = load_manifest(MANIFEST_PATH)
    store = KlineStore(str(tmp_path / "phase-selection.db"))
    adapter = FakeCryptoAdapter()

    receipt = await IngestionOrchestrator(
        store, adapter_resolver=lambda _instrument: adapter
    ).run_once(
        IngestionPlan(
            manifest=manifest,
            run_id="run-coarse-phase",
            now=datetime(2026, 9, 1, 12, tzinfo=timezone.utc),
            fetch_limit=10,
            instrument_ids=("CRYPTO.PERP.BTC",),
            timeframes=("1d", "1w"),
            request_interval_seconds=0.001,
        )
    )

    assert [cell.timeframe for cell in receipt.requested_cells] == ["1d", "1w"]
    assert [cell.status for cell in receipt.requested_cells] == ["ready", "ready"]
    assert receipt.row_counts["promoted_candles"] == 2


@pytest.mark.asyncio
async def test_run_once_preserves_retry_attempts_and_latency_metrics(tmp_path: Path) -> None:
    manifest = load_manifest(MANIFEST_PATH)
    store = KlineStore(str(tmp_path / "attempt-metrics.db"))

    class RetryingAdapter:
        def __init__(self) -> None:
            self.calls = 0
            self.last_attempts: list[dict[str, object]] = []

        async def fetch_candles_with_receipt(
            self,
            _ticker: str,
            _timeframe: Timeframe,
            *,
            start: str | None,
            end: str,
            limit: int,
        ) -> FetchReceipt:
            del start, end, limit
            self.calls += 1
            if self.calls == 1:
                self.last_attempts = [
                    {"source": "fake", "status": "error", "http_status": 429, "latency_ms": 1.0}
                ]
                raise ProviderError("HTTP 429 rate limit")
            self.last_attempts = [
                {"source": "fake", "status": "success", "http_status": 200, "latency_ms": 2.0}
            ]
            return FetchReceipt(
                candles=[
                    Candle(
                        timestamp="2026-08-31T00:00:00+00:00",
                        open=100,
                        high=102,
                        low=99,
                        close=101,
                        volume=10,
                    )
                ],
                timeframe_transform=None,
                source_identity={"selected_source": "fake"},
                raw_response={"http_status": 200, "row_count": 1},
                attempts=tuple(self.last_attempts),
            )

    adapter = RetryingAdapter()
    receipt = await IngestionOrchestrator(
        store, adapter_resolver=lambda _instrument: adapter
    ).run_once(
        IngestionPlan(
            manifest=manifest,
            run_id="run-attempt-metrics",
            now=datetime(2026, 9, 1, 12, tzinfo=timezone.utc),
            instrument_ids=("CRYPTO.PERP.BTC",),
            timeframes=("1d",),
            max_retries=1,
            retry_backoff_seconds=0.001,
        )
    )

    assert receipt.status == "success"
    attempt = receipt.source_attempts[0]
    assert attempt["latency_ms"] >= 0
    assert [item["retry_number"] for item in attempt["provider_attempts"]] == [1, 2]
    observation = store.latest_mvp_source_observations()[0]
    assert observation["latency_ms"] >= 0
    assert len(observation["policy"]["provider_attempts"]) == 2


@pytest.mark.asyncio
async def test_run_once_persists_failed_provider_attempts(tmp_path: Path) -> None:
    manifest = apply_free_source_profile(load_manifest(MANIFEST_PATH))
    store = KlineStore(str(tmp_path / "failed-attempt-metrics.db"))

    class AlwaysFailAdapter:
        def __init__(self) -> None:
            self.calls = 0
            self.last_attempts: list[dict[str, object]] = []

        async def fetch_candles_with_receipt(
            self,
            _ticker: str,
            _timeframe: Timeframe,
            *,
            start: str | None,
            end: str,
            limit: int,
        ) -> FetchReceipt:
            del start, end, limit
            self.calls += 1
            self.last_attempts = [
                {
                    "source": "fake",
                    "status": "error",
                    "http_status": 503,
                    "latency_ms": float(self.calls),
                }
            ]
            raise ProviderError("HTTP 503 service unavailable")

    adapter = AlwaysFailAdapter()
    receipt = await IngestionOrchestrator(
        store, adapter_resolver=lambda _instrument: adapter
    ).run_once(
        IngestionPlan(
            manifest=manifest,
            run_id="run-failed-attempt-metrics",
            now=datetime(2026, 9, 1, 12, tzinfo=timezone.utc),
            instrument_ids=("CN.A.600519",),
            timeframes=("1d",),
            max_retries=1,
            retry_backoff_seconds=0.001,
        )
    )

    assert receipt.status == "partial"
    assert len(receipt.source_attempts[0]["provider_attempts"]) == 2
    observation = store.latest_mvp_source_observations()[0]
    assert observation["success"] is False
    assert len(observation["policy"]["provider_attempts"]) == 2


@pytest.mark.asyncio
async def test_run_once_persists_failed_attempts_through_provider_adapter(
    tmp_path: Path,
) -> None:
    manifest = apply_free_source_profile(load_manifest(MANIFEST_PATH))
    store = KlineStore(str(tmp_path / "wrapped-failed-attempts.db"))

    def handler(request):
        return httpx.Response(503, request=request)

    provider = AShareFreeProvider(transport=httpx.MockTransport(handler))
    adapter = ProviderBackedMarketDataAdapter(
        source_manifest("tencent_stock_free", AssetClass.A_SHARE),
        provider,
    )
    receipt = await IngestionOrchestrator(
        store, adapter_resolver=lambda _instrument: adapter
    ).run_once(
        IngestionPlan(
            manifest=manifest,
            run_id="run-wrapped-failed-attempts",
            now=datetime(2026, 9, 1, 12, tzinfo=timezone.utc),
            instrument_ids=("CN.A.600519",),
            timeframes=("1h",),
            max_retries=0,
        )
    )

    assert receipt.status == "partial"
    assert [item["source"] for item in receipt.source_attempts[0]["provider_attempts"]] == [
        "tencent",
        "tonghuashun",
    ]
    observation = store.latest_mvp_source_observations()[0]
    assert len(observation["policy"]["provider_attempts"]) == 2


@pytest.mark.asyncio
async def test_run_once_does_not_retry_terminal_empty_response(tmp_path: Path) -> None:
    manifest = apply_free_source_profile(load_manifest(MANIFEST_PATH))
    store = KlineStore(str(tmp_path / "terminal-empty.db"))

    class EmptyAdapter:
        def __init__(self) -> None:
            self.calls = 0
            self.last_attempts: list[dict[str, object]] = []

        async def fetch_candles_with_receipt(self, *_args, **_kwargs) -> FetchReceipt:
            self.calls += 1
            self.last_attempts = [
                {"source": "fake", "status": "error", "http_status": 200, "latency_ms": 1.0}
            ]
            error = ProviderError("Tencent returned no minute rows")
            error.code = "empty_response"
            raise error

    adapter = EmptyAdapter()
    receipt = await IngestionOrchestrator(
        store, adapter_resolver=lambda _instrument: adapter
    ).run_once(
        IngestionPlan(
            manifest=manifest,
            run_id="run-terminal-empty",
            now=datetime(2026, 9, 1, 12, tzinfo=timezone.utc),
            instrument_ids=("CN.A.600519",),
            timeframes=("15m",),
            max_retries=2,
            retry_backoff_seconds=0.001,
        )
    )

    assert receipt.status == "partial"
    assert adapter.calls == 1
    assert receipt.source_attempts[0]["provider_attempts"][0]["retry_number"] == 1


@pytest.mark.asyncio
async def test_run_once_suppresses_watermark_regression_without_failing_transaction(
    tmp_path: Path,
) -> None:
    manifest = load_manifest(MANIFEST_PATH)
    store = KlineStore(str(tmp_path / "watermark-regression.db"))
    key = "CRYPTO.PERP.BTC"
    watermark_key = CandleSeriesKey(
        instrument_id=key,
        display_symbol="BTC",
        provider_symbol="BTC",
        source_id="hyperliquid_perpetual_public",
        asset_class="crypto",
        timeframe="1d",
        adjustment_basis="raw_unadjusted",
        manifest_version=manifest.version,
    )
    first = await IngestionOrchestrator(
        store, adapter_resolver=lambda _instrument: FakeCryptoAdapter()
    ).run_once(
        IngestionPlan(
            manifest=manifest,
            run_id="run-watermark-forward",
            now=datetime(2026, 9, 1, 12, tzinfo=timezone.utc),
            instrument_ids=(key,),
            timeframes=("1d",),
        )
    )
    assert first.status == "success"

    class OlderAdapter:
        async def fetch_candles_with_receipt(self, *_args, **_kwargs) -> FetchReceipt:
            return FetchReceipt(
                candles=[
                    Candle(
                        timestamp="2026-08-30T00:00:00+00:00",
                        open=100,
                        high=102,
                        low=99,
                        close=101,
                        volume=10,
                    )
                ],
                timeframe_transform=None,
                source_identity={"provider_symbol": "BTC"},
                raw_response={"row_count": 1},
                attempts=({"source": "fake", "status": "success", "latency_ms": 1.0},),
            )

    second = await IngestionOrchestrator(
        store, adapter_resolver=lambda _instrument: OlderAdapter()
    ).run_once(
        IngestionPlan(
            manifest=manifest,
            run_id="run-watermark-backward",
            now=datetime(2026, 9, 1, 12, tzinfo=timezone.utc),
            instrument_ids=(key,),
            timeframes=("1d",),
        )
    )
    assert second.status == "partial"
    assert second.row_counts["watermarks"] == 0
    assert second.row_counts["watermarks_skipped"] == 1
    assert second.source_attempts[0]["watermark_regression_suppressed"] is True
    assert (
        store.get_mvp_watermark(watermark_key).last_closed_timestamp == "2026-08-31T00:00:00+00:00"
    )


@pytest.mark.asyncio
async def test_run_once_bounds_hanging_provider_cell(tmp_path: Path) -> None:
    manifest = load_manifest(MANIFEST_PATH)
    store = KlineStore(str(tmp_path / "provider-timeout.db"))

    class HangingAdapter:
        calls = 0

        async def fetch_candles_with_receipt(self, *_args, **_kwargs) -> FetchReceipt:
            self.calls += 1
            await asyncio.sleep(10)
            raise AssertionError("timeout should cancel the provider call")

    adapter = HangingAdapter()
    receipt = await IngestionOrchestrator(
        store, adapter_resolver=lambda _instrument: adapter
    ).run_once(
        IngestionPlan(
            manifest=manifest,
            run_id="run-provider-timeout",
            now=datetime(2026, 9, 1, 12, tzinfo=timezone.utc),
            instrument_ids=("CRYPTO.PERP.BTC",),
            timeframes=("1d",),
            max_retries=2,
            provider_timeout_seconds=0.01,
        )
    )

    assert receipt.status == "partial"
    assert adapter.calls == 1
    assert receipt.source_attempts[0]["status"] == "timeout"
    assert receipt.source_attempts[0]["latency_ms"] >= 0


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
    receipt = await IngestionOrchestrator(
        store, adapter_resolver=lambda _instrument: adapter
    ).run_once(
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
