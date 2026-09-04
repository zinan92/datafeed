from datetime import datetime, timezone
from pathlib import Path

from kline.watchlist_manifest import load_watchlist_manifest
from ops.verify_watchlist_freshness import reconcile_watchlist_freshness


MANIFEST_PATH = Path(__file__).parents[1] / "configs" / "watchlist_registry_manifest.json"


class _ObservationStore:
    def __init__(self, observations):
        self._observations = observations

    def latest_mvp_source_observations(self):
        return list(self._observations)


def _receipt(manifest, statuses):
    return {
        "observed_at": datetime(2026, 9, 4, 0, 15, tzinfo=timezone.utc).isoformat(),
        "manifest_hash": manifest.validated_digest(),
        "instrument_statuses": {
            instrument_id: {"status": status}
            for instrument_id, status in statuses.items()
        },
        "reports": [{"run_id": "watchlist-verification-001"}],
    }


def _observation(instrument, timestamp, *, success=True):
    return {
        "run_id": "watchlist-verification-001",
        "manifest_version": "watchlist_universe_v1",
        "instrument_id": instrument.instrument_id,
        "timeframe": "1d",
        "latest_timestamp": timestamp,
        "success": success,
    }


def test_reconciliation_compares_each_reported_status_with_upstream_session_freshness() -> None:
    full_manifest = load_watchlist_manifest(MANIFEST_PATH)
    selected = {
        item.instrument_id: item
        for item in full_manifest.instruments
        if item.instrument_id
        in {"WATCH.CN.A.600900", "WATCH.CROSS.SHCOMP", "WATCH.CROSS.STAR50"}
    }
    manifest = type(full_manifest)(
        version=full_manifest.version,
        selection_as_of=full_manifest.selection_as_of,
        effective_at=full_manifest.effective_at,
        membership_policy=full_manifest.membership_policy,
        excluded_symbols=full_manifest.excluded_symbols,
        instruments=tuple(selected.values()),
        registry=full_manifest.registry,
    )
    store = _ObservationStore(
        [
            _observation(selected["WATCH.CN.A.600900"], "2026-09-02T16:00:00+00:00"),
            _observation(selected["WATCH.CROSS.SHCOMP"], "2026-09-03T00:00:00+00:00"),
            _observation(selected["WATCH.CROSS.STAR50"], "2026-09-02T00:00:00+00:00"),
        ]
    )

    report = reconcile_watchlist_freshness(
        manifest,
        store,
        _receipt(
            manifest,
            {
                "WATCH.CN.A.600900": "ready",
                "WATCH.CROSS.SHCOMP": "ready",
                "WATCH.CROSS.STAR50": "stale",
            },
        ),
    )

    assert report["status"] == "pass"
    assert report["instrument_count"] == 3
    assert report["reported_status_counts"] == {"ready": 2, "stale": 1}
    assert report["expected_status_counts"] == {"ready": 2, "stale": 1}
    assert report["mismatch_count"] == 0
    assert all(row["matches"] for row in report["rows"])
