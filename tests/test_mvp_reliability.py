from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kline.mvp_manifest import load_manifest
from kline.store import KlineStore
from ops.mvp_reliability import (
    DEMO_INSTRUMENT_IDS,
    _silence_gate,
    _run_times,
    audit_reliability,
    demo_manifest,
    run_demo_once,
)


MANIFEST_PATH = Path(__file__).parents[1] / "configs" / "mvp_manifest.json"


def test_demo_manifest_is_exactly_three_plus_three() -> None:
    manifest = demo_manifest(load_manifest(MANIFEST_PATH))
    assert tuple(item.instrument_id for item in manifest.instruments) == DEMO_INSTRUMENT_IDS
    assert manifest.counts == {"a_share": 3, "us_stock": 3, "cross_market": 0}


@pytest.mark.asyncio
async def test_run_demo_once_writes_a_terminal_receipt_without_network_calls(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "reliability.db"
    result = await run_demo_once(
        db_path=db_path,
        manifest_path=MANIFEST_PATH,
        now=datetime(2026, 9, 1, 12, tzinfo=timezone.utc),
        adapter_resolver=lambda _instrument: None,
    )
    assert result["status"] == "partial"
    assert result["run_id"]
    store = KlineStore(str(db_path))
    run = store.latest_mvp_run()
    assert run is not None
    assert run["status"] == "partial"
    assert store.mvp_storage_health()["runs"] == 1
    assert store.mvp_duplicate_key_count() == 0
    assert store.mvp_watermark_count_for_runs({"missing-run"}) == 0
    assert result["health"]["scope"]["name"] == "demo_3x3"


def test_audit_stays_blocked_before_seven_days(tmp_path: Path) -> None:
    store = KlineStore(str(tmp_path / "audit.db"))
    end = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
    report = audit_reliability(
        load_manifest(MANIFEST_PATH),
        store,
        window_start=end - timedelta(days=1),
        window_end=end,
    )
    assert report["status"] == "blocked"
    assert "terminal_receipt" in report["gates"]
    assert report["gates"]["seven_calendar_days"]["status"] == "blocked"


def test_silence_gate_uses_receipt_completion_time() -> None:
    gate = _silence_gate(
        [
            {
                "started_at": "2026-09-01T00:00:00+00:00",
                "completed_at": "2026-09-01T00:01:00+00:00",
            },
            {
                "started_at": "2026-09-01T04:00:00+00:00",
                "completed_at": "2026-09-01T13:01:00+00:00",
            },
        ],
        start=datetime(2026, 9, 1, 0, tzinfo=timezone.utc),
        end=datetime(2026, 9, 1, 14, tzinfo=timezone.utc),
    )
    assert gate["status"] == "failed"
    assert gate["max_silent_hours"] > 8


def test_run_times_are_newest_first_for_the_timeline() -> None:
    class FakeStore:
        def latest_mvp_runs(self, *, limit: int):
            del limit
            return [
                {"run_id": "old", "started_at": "2026-09-01T01:00:00+00:00"},
                {"run_id": "new", "started_at": "2026-09-01T05:00:00+00:00"},
            ]

    rows = _run_times(
        FakeStore(),
        datetime(2026, 9, 1, 0, tzinfo=timezone.utc),
        datetime(2026, 9, 1, 6, tzinfo=timezone.utc),
    )
    assert [row["run_id"] for row in rows] == ["new", "old"]
