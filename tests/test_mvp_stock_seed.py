from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kline.free_source_profile import apply_free_source_profile
from kline.mvp_manifest import load_manifest
from ops.mvp_stock_seed import (
    _batch_report,
    _batches,
    _classify_attempt,
    remaining_stock_ids,
    stock_cycle_wait_seconds,
    stock_instrument_ids,
    validate_seed_target,
)


MANIFEST_PATH = Path(__file__).parents[1] / "configs" / "mvp_manifest.json"


def test_stock_cycle_wait_is_anchored_to_start_across_runtime_variation() -> None:
    started = datetime(2026, 9, 2, 1, 30, tzinfo=timezone.utc)

    assert stock_cycle_wait_seconds(
        cycle_started=started,
        now=started + timedelta(minutes=29),
        interval_seconds=4 * 60 * 60,
    ) == 3 * 60 * 60 + 31 * 60
    assert stock_cycle_wait_seconds(
        cycle_started=started,
        now=started + timedelta(hours=3, minutes=58),
        interval_seconds=4 * 60 * 60,
    ) == 2 * 60
    assert stock_cycle_wait_seconds(
        cycle_started=started,
        now=started + timedelta(hours=4, minutes=5),
        interval_seconds=4 * 60 * 60,
    ) == 0


def test_stock_seed_selects_the_100_plus_100_manifest_universes() -> None:
    manifest = apply_free_source_profile(load_manifest(MANIFEST_PATH))
    selected = stock_instrument_ids(manifest)
    assert {universe: len(ids) for universe, ids in selected.items()} == {
        "a_share": 100,
        "us_stock": 100,
    }
    assert all(
        item_id.startswith(("CN.A.", "US.EQ.")) for ids in selected.values() for item_id in ids
    )


def test_remaining_stock_ids_skip_only_complete_free_series() -> None:
    manifest = apply_free_source_profile(load_manifest(MANIFEST_PATH))
    a_share = next(item for item in manifest.instruments if item.instrument_id == "CN.A.600519")
    us_stock = next(item for item in manifest.instruments if item.instrument_id == "US.EQ.AAPL")

    class FakeStore:
        def mvp_latest_closed_bars(self) -> list[dict[str, str]]:
            return [
                {
                    "source_id": item.source_id,
                    "instrument_id": item.instrument_id,
                    "timeframe": timeframe,
                    "adjustment_basis": item.adjustment_basis,
                    "manifest_version": manifest.version,
                }
                for item in (a_share, us_stock)
                for timeframe in item.required_timeframes
            ]

    remaining = remaining_stock_ids(manifest, FakeStore())
    assert len(remaining["a_share"]) == 99
    assert len(remaining["us_stock"]) == 99
    assert "CN.A.600519" not in remaining["a_share"]
    assert "US.EQ.AAPL" not in remaining["us_stock"]


def test_remaining_stock_ids_can_resume_each_phase_independently() -> None:
    manifest = apply_free_source_profile(load_manifest(MANIFEST_PATH))
    a_share = next(item for item in manifest.instruments if item.instrument_id == "CN.A.600519")
    us_stock = next(item for item in manifest.instruments if item.instrument_id == "US.EQ.AAPL")

    class FakeStore:
        def mvp_latest_closed_bars(self) -> list[dict[str, str]]:
            return [
                {
                    "source_id": item.source_id,
                    "instrument_id": item.instrument_id,
                    "timeframe": timeframe,
                    "adjustment_basis": item.adjustment_basis,
                    "manifest_version": manifest.version,
                }
                for item in (a_share, us_stock)
                for timeframe in ("1d", "1w")
            ]

    coarse = remaining_stock_ids(manifest, FakeStore(), required_timeframes=("1d", "1w"))
    intraday = remaining_stock_ids(manifest, FakeStore(), required_timeframes=("15m", "1h", "4h"))
    assert len(coarse["a_share"]) == 99
    assert len(coarse["us_stock"]) == 99
    assert len(intraday["a_share"]) == 100
    assert len(intraday["us_stock"]) == 100


def test_batches_are_deterministic_and_rate_errors_are_classified() -> None:
    assert _batches(("a", "b", "c", "d", "e"), 2) == [
        ("a", "b"),
        ("c", "d"),
        ("e",),
    ]
    assert (
        _classify_attempt({"status": "provider_error", "error": "HTTP 429 rate limit"})
        == "rate_limit"
    )
    assert (
        _classify_attempt({"status": "provider_error", "error": "HTTP 403 forbidden"})
        == "forbidden"
    )
    assert _classify_attempt({"status": "provider_error", "error": "request timeout"}) == "timeout"
    assert (
        _classify_attempt({"status": "unavailable", "error": "No data returned"})
        == "empty_response"
    )
    assert (
        _classify_attempt({"status": "error", "error": "Tencent returned no minute rows"})
        == "empty_response"
    )
    assert (
        _classify_attempt({"status": "error", "error": "Sina returned no daily rows"})
        == "empty_response"
    )
    assert _classify_attempt({"status": "error", "http_status": 503}) == "server_error"


def test_stock_seed_refuses_non_observer_database(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="refuses non-observer database"):
        validate_seed_target(tmp_path / "unsafe.db")


def test_stock_seed_refuses_non_canonical_lock() -> None:
    from ops.mvp_stock_seed import SAFE_OBSERVER_DB

    with pytest.raises(ValueError, match="canonical observer lock"):
        validate_seed_target(SAFE_OBSERVER_DB, SAFE_OBSERVER_DB.with_name("alternate.lock"))


def test_batch_report_calculates_attempt_p95_and_server_errors() -> None:
    class Receipt:
        run_id = "run-metrics"
        status = "partial"
        requested_cells = (object(),)
        row_counts = {"promoted_candles": 1}
        quality = {"pass": 1}
        source_attempts = (
            {
                "provider_symbol": "A",
                "timeframe": "1d",
                "latency_ms": 100,
                "provider_attempts": (
                    {"source": "tencent", "status": "error", "http_status": 503, "latency_ms": 50},
                    {
                        "source": "tonghuashun",
                        "status": "success",
                        "http_status": 200,
                        "latency_ms": 100,
                    },
                ),
            },
        )

    report = _batch_report(
        Receipt(), phase="coarse", universe="a_share", batch_index=1, batch_size=1
    )
    assert report["attempt_count"] == 2
    assert report["p95_latency_ms"] == 100.0
    assert report["error_counts"] == {"server_error": 1}
    assert report["error_samples"][0]["http_status"] == 503
