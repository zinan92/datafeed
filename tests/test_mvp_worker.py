from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kline.models import Timeframe
from kline.mvp_manifest import load_manifest, manifest_digest
from kline.mvp_worker import (
    MAX_INTERVAL_SECONDS,
    MvpWorker,
    TargetGuardResult,
    WorkerError,
    _SingleRunLock,
    build_mvp_health,
    build_mvp_serving_status,
    next_due_at,
)
from kline.store import KlineStore


MANIFEST_PATH = Path(__file__).parents[1] / "configs" / "mvp_manifest.json"


def test_interval_is_capped_at_four_hours_and_overrun_does_not_drift(tmp_path: Path) -> None:
    now = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
    assert (
        next_due_at(
            last_started=now - timedelta(hours=5), now=now, interval_seconds=MAX_INTERVAL_SECONDS
        )
        == now
    )
    assert next_due_at(
        last_started=now - timedelta(hours=1), now=now, interval_seconds=MAX_INTERVAL_SECONDS
    ) == now + timedelta(hours=3)
    with pytest.raises(WorkerError, match="no greater than four hours"):
        MvpWorker(
            load_manifest(MANIFEST_PATH),
            KlineStore(str(tmp_path / "interval.db")),
            interval_seconds=MAX_INTERVAL_SECONDS + 1,
        )


def test_single_run_lock_reports_contention(tmp_path: Path) -> None:
    path = tmp_path / "worker.lock"
    first = _SingleRunLock(path)
    first.__enter__()
    try:
        with pytest.raises(WorkerError, match="already held"):
            _SingleRunLock(path).__enter__()
    finally:
        first.__exit__(None, None, None)


@pytest.mark.asyncio
async def test_worker_target_guard_blocks_without_calling_orchestrator(tmp_path: Path) -> None:
    class NeverOrchestrator:
        called = False

        async def run_once(self, _plan):
            self.called = True
            raise AssertionError("orchestrator must not run when target is blocked")

    orchestrator = NeverOrchestrator()
    worker = MvpWorker(
        load_manifest(MANIFEST_PATH),
        KlineStore(str(tmp_path / "guard.db")),
        orchestrator=orchestrator,
        target_guard=lambda: TargetGuardResult("blocked", "/Volumes/Phone SSD", "mount missing"),
        clock=lambda: datetime(2026, 8, 31, 5, tzinfo=timezone.utc),
    )
    result = await worker.run_once()
    assert result.status == "blocked_target"
    assert result.reason == "mount missing"
    assert orchestrator.called is False
    assert worker.health()["worker"]["status"] == "blocked_target"


@pytest.mark.asyncio
async def test_worker_runs_orchestrator_and_serving_reports_stable_cells(tmp_path: Path) -> None:
    from tests.test_ingestion import FakeCryptoAdapter

    manifest = load_manifest(MANIFEST_PATH)
    store = KlineStore(str(tmp_path / "worker.db"))
    adapter = FakeCryptoAdapter()
    worker = MvpWorker(
        manifest,
        store,
        adapter_resolver=lambda instrument: (
            adapter if instrument.instrument_id.startswith("CRYPTO.") else None
        ),
        interval_seconds=3600,
        lock_path=tmp_path / "worker.lock",
        clock=lambda: datetime(2026, 8, 31, 5, tzinfo=timezone.utc),
    )

    result = await worker.run_once()
    assert result.status == "partial"
    assert result.receipt is not None
    health = worker.health()
    assert health["status"] == "partial"
    assert health["manifest_hash"] == manifest_digest(manifest)
    assert health["worker"]["interval_seconds"] == 3600
    assert health["raw_retention"]["status"] == "receipt_only"
    assert health["row_counts"]["candles"] == 9

    serving = worker.serving()
    btc_4h = next(
        cell
        for cell in serving["cells"]
        if cell["instrument_id"] == "CRYPTO.PERP.BTC"
        and cell["timeframe"] == Timeframe.HOUR_4.value
    )
    assert btc_4h["status"] == "ready"
    spx = next(
        cell
        for cell in serving["cells"]
        if cell["instrument_id"] == "US.INDEX.SPX" and cell["timeframe"] == "1d"
    )
    assert spx["status"] == "blocked"

    restarted = MvpWorker(
        manifest,
        store,
        adapter_resolver=lambda instrument: (
            adapter if instrument.instrument_id.startswith("CRYPTO.") else None
        ),
        interval_seconds=3600,
        lock_path=tmp_path / "worker.lock",
        clock=lambda: datetime(2026, 8, 31, 5, tzinfo=timezone.utc),
    )
    assert restarted.health()["worker"]["last_run_id"] == result.run_id


def test_health_and_serving_are_explicit_before_any_run(tmp_path: Path) -> None:
    manifest = load_manifest(MANIFEST_PATH)
    store = KlineStore(str(tmp_path / "empty.db"))
    health = build_mvp_health(manifest, store, now=datetime(2026, 9, 1, tzinfo=timezone.utc))
    serving = build_mvp_serving_status(manifest, store)

    assert health["status"] == "blocked"
    assert health["last_run"] is None
    assert health["backup"] is None
    assert serving["manifest_hash"] == manifest_digest(manifest)
    assert all("instrument_id" in cell and "source_id" in cell for cell in serving["cells"])
