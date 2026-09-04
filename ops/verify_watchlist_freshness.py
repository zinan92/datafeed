"""Reconcile one Watchlist run receipt with its upstream daily observations."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from kline.session_freshness import assess_daily_freshness
from kline.store import KlineReadOnlyStore
from kline.time_utils import parse_utc_timestamp
from kline.watchlist_manifest import WatchlistManifest, load_watchlist_manifest


def reconcile_watchlist_freshness(
    manifest: WatchlistManifest,
    store: Any,
    receipt: Mapping[str, Any],
) -> dict[str, Any]:
    """Return an instrument-by-instrument comparison for one completed run."""

    observed_at = parse_utc_timestamp(str(receipt.get("observed_at") or ""))
    if observed_at is None:
        raise ValueError("Watchlist receipt observed_at is invalid")
    if receipt.get("manifest_hash") != manifest.validated_digest():
        raise ValueError("Watchlist receipt manifest_hash does not match the manifest")
    statuses = receipt.get("instrument_statuses")
    reports = receipt.get("reports")
    if not isinstance(statuses, Mapping) or not isinstance(reports, list):
        raise ValueError("Watchlist receipt is missing instrument statuses or batch reports")
    run_ids = {
        str(report.get("run_id"))
        for report in reports
        if isinstance(report, Mapping) and report.get("run_id")
    }
    observations = {
        str(row["instrument_id"]): row
        for row in store.latest_mvp_source_observations()
        if row.get("manifest_version") == manifest.version
        and row.get("timeframe") == "1d"
        and row.get("run_id") in run_ids
    }
    instruments = {item.instrument_id: item for item in manifest.instruments}
    if set(statuses) != set(instruments):
        raise ValueError("Watchlist receipt instrument denominator does not match the manifest")

    rows: list[dict[str, Any]] = []
    expected_counts: Counter[str] = Counter()
    reported_counts: Counter[str] = Counter()
    for instrument_id in sorted(instruments):
        instrument = instruments[instrument_id]
        observation = observations.get(instrument_id)
        reported_status = str(statuses[instrument_id].get("status") or "missing_status")
        reported_counts[reported_status] += 1
        if observation is None:
            expected_status = "missing_observation"
            freshness = assess_daily_freshness(instrument, None, now=observed_at)
        else:
            freshness = assess_daily_freshness(
                instrument,
                str(observation.get("latest_timestamp") or ""),
                now=observed_at,
            )
            if not observation.get("success"):
                expected_status = "fail"
            elif freshness.stale is True:
                expected_status = "stale"
            elif freshness.stale is False:
                expected_status = "ready"
            else:
                expected_status = "unresolved"
        expected_counts[expected_status] += 1
        rows.append(
            {
                "instrument_id": instrument_id,
                "source_id": instrument.source_id,
                "daily_timestamp_convention": freshness.convention,
                "upstream_latest_timestamp": observation.get("latest_timestamp")
                if observation is not None
                else None,
                "upstream_observation_success": bool(observation.get("success"))
                if observation is not None
                else None,
                "observed_session": freshness.observed_session.isoformat()
                if freshness.observed_session is not None
                else None,
                "expected_session": freshness.expected_session.isoformat()
                if freshness.expected_session is not None
                else None,
                "reported_status": reported_status,
                "expected_status": expected_status,
                "matches": reported_status == expected_status,
            }
        )
    mismatches = [row for row in rows if not row["matches"]]
    return {
        "status": "pass" if not mismatches else "fail",
        "observed_at": observed_at.isoformat(),
        "manifest_version": manifest.version,
        "manifest_hash": manifest.validated_digest(),
        "instrument_count": len(rows),
        "reported_status_counts": dict(reported_counts),
        "expected_status_counts": dict(expected_counts),
        "mismatch_count": len(mismatches),
        "rows": rows,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Reconcile Watchlist daily freshness")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--db", required=True)
    parser.add_argument("--receipt", required=True)
    parser.add_argument("--output", default=None)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    manifest = load_watchlist_manifest(args.manifest)
    receipt = json.loads(Path(args.receipt).read_text(encoding="utf-8"))
    report = reconcile_watchlist_freshness(
        manifest,
        KlineReadOnlyStore(args.db),
        receipt,
    )
    rendered = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    print(rendered, end="")
    if args.output:
        Path(args.output).write_text(rendered, encoding="utf-8")
    return 0 if report["status"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
