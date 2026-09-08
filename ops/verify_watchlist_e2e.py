"""Read-only verification of a Watchlist registry delta through the 8100 API."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from kline.watchlist_manifest import validate_watchlist_manifest
from kline.watchlist_registry import REGISTRY_REPOSITORY
from ops.sync_watchlist_registry import build_snapshot_from_bytes, _compile_manifest


REGISTRY_FILE = "watchlist.yaml"
DEFAULT_BASE_URL = "http://127.0.0.1:8100"
RECEIPT_SCHEMA = "watchlist-e2e-verification-v1"


def _get_json(url: str, *, timeout: float) -> tuple[int, Any]:
    request = Request(url, headers={"Accept": "application/json"}, method="GET")
    try:
        with urlopen(request, timeout=timeout) as response:
            return int(getattr(response, "status", 200)), json.loads(response.read().decode())
    except HTTPError as error:
        try:
            payload = json.loads(error.read().decode())
        except (ValueError, UnicodeDecodeError):
            payload = {"error": type(error).__name__, "detail": str(error)}
        return int(error.code), payload
    except (URLError, TimeoutError, OSError, ValueError) as error:
        return 0, {"error": type(error).__name__, "detail": str(error)}


def _fetch_registry_revision(sha: str, *, timeout: float) -> tuple[str, bytes]:
    """Fetch exactly the requested immutable revision without a mutable ref lookup."""
    commit = sha.strip().lower()
    if len(commit) != 40 or any(char not in "0123456789abcdef" for char in commit):
        raise ValueError("registry SHA must be a 40-character lowercase SHA")
    url = f"https://raw.githubusercontent.com/{REGISTRY_REPOSITORY}/{commit}/{REGISTRY_FILE}"
    request = Request(url, headers={"User-Agent": "datafeed-watchlist-e2e"}, method="GET")
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except (HTTPError, URLError, TimeoutError, OSError) as error:
        raise RuntimeError(f"registry fetch failed for {commit}: {error}") from error
    if not raw.strip():
        raise RuntimeError(f"registry file is empty at {commit}")
    return commit, raw


def _manifest_for_sha(sha: str, *, timeout: float) -> Any:
    commit, raw = _fetch_registry_revision(sha, timeout=timeout)
    snapshot = build_snapshot_from_bytes(raw, commit=commit)
    return validate_watchlist_manifest(_compile_manifest(snapshot))


def _instrument_map(manifest: Any) -> dict[str, Any]:
    return {str(item.instrument_id): item for item in manifest.instruments}


def _diff(before: Any, after: Any) -> tuple[list[Any], list[Any]]:
    previous = _instrument_map(before)
    current = _instrument_map(after)
    return (
        [previous[key] for key in sorted(previous.keys() - current.keys())],
        [current[key] for key in sorted(current.keys() - previous.keys())],
    )


def _candle_probe(
    base_url: str, instrument: Any, *, timeout: float, limit: int
) -> dict[str, Any]:
    query = urlencode(
        {
            "timeframe": "1d",
            "limit": max(1, limit),
            "source": instrument.source_id,
            "cache_policy": "require",
            "quality": "strict",
            "fallback_policy": "none",
        }
    )
    url = (
        f"{base_url.rstrip('/')}/api/candles/"
        f"{quote(str(instrument.asset_class), safe='')}/{quote(str(instrument.provider_symbol), safe='')}?{query}"
    )
    status, payload = _get_json(url, timeout=timeout)
    candles = payload.get("candles") if isinstance(payload, Mapping) else None
    issues: list[str] = []
    if status != 200:
        issues.append("http_error")
    if not isinstance(candles, list) or not candles:
        issues.append("no_1d_bars")
    if isinstance(payload, Mapping):
        if payload.get("timeframe") != "1d":
            issues.append("timeframe_mismatch")
        if payload.get("provider_symbol") not in {None, instrument.provider_symbol}:
            issues.append("provider_symbol_mismatch")
        if payload.get("is_synthetic") is True:
            issues.append("synthetic_bar")
        if payload.get("reject_reason"):
            issues.append("rejected")
    return {
        "status": "pass" if not issues else "fail",
        "http_status": status,
        "url": url,
        "instrument_id": instrument.instrument_id,
        "provider_symbol": instrument.provider_symbol,
        "candle_count": len(candles) if isinstance(candles, list) else 0,
        "issues": issues,
    }


def _matrix_probe(base_url: str, instrument: Any, *, timeout: float) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}/api/health/combined-matrix"
    status, payload = _get_json(url, timeout=timeout)
    cell = None
    if isinstance(payload, Mapping):
        cell = next(
            (
                candidate
                for candidate in payload.get("cells", [])
                if isinstance(candidate, Mapping)
                and candidate.get("dataset") == "watchlist"
                and candidate.get("instrument_id") == instrument.instrument_id
                and candidate.get("timeframe") == "1d"
            ),
            None,
        )
    issues = []
    if status != 200:
        issues.append("http_error")
    if cell is None:
        issues.append("cell_missing")
    elif cell.get("status") not in {"ready", "ready_unverified"}:
        issues.append(f"cell_status:{cell.get('status')}")
    return {
        "status": "pass" if not issues else "fail",
        "http_status": status,
        "url": url,
        "instrument_id": instrument.instrument_id,
        "cell_status": cell.get("status") if cell else None,
        "cell": dict(cell) if cell else None,
        "issues": issues,
    }


def verify(
    before_sha: str,
    after_sha: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = 30.0,
    limit: int = 3,
) -> dict[str, Any]:
    before = _manifest_for_sha(before_sha, timeout=timeout)
    after = _manifest_for_sha(after_sha, timeout=timeout)
    removed, added = _diff(before, after)
    rows: list[dict[str, Any]] = []
    for action, instruments in (("added", added), ("removed", removed)):
        for instrument in instruments:
            candles = _candle_probe(base_url, instrument, timeout=timeout, limit=limit)
            matrix = _matrix_probe(base_url, instrument, timeout=timeout)
            if action == "removed":
                # A retained historical bar is not a failure; the acceptance gate is cell absence.
                matrix["status"] = "pass" if matrix["cell_status"] is None and matrix["http_status"] == 200 else "fail"
                matrix["issues"] = [] if matrix["status"] == "pass" else matrix["issues"]
            rows.append(
                {
                    "change": action,
                    "instrument_id": instrument.instrument_id,
                    "display_symbol": instrument.display_symbol,
                    "candles": candles,
                    "combined_matrix": matrix,
                    "status": (
                        "pass"
                        if matrix["status"] == "pass"
                        and (action == "removed" or candles["status"] == "pass")
                        else "fail"
                    ),
                }
            )
    overall = "pass" if all(row["status"] == "pass" for row in rows) else "fail"
    return {
        "schema_version": RECEIPT_SCHEMA,
        "observed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "status": overall,
        "base_url": base_url,
        "before_registry_sha": before_sha,
        "after_registry_sha": after_sha,
        "removed_count": len(removed),
        "added_count": len(added),
        "instrument_delta_count": len(rows),
        "rows": rows,
        "real_daily_verification": "owner_pending",
        "legacy_manifest_audit": {
            "path": "configs/watchlist_manifest.json",
            "status": "confirmed_legacy_src_reference",
            "references": ["src/kline/market_query.py:323"],
            "not_changed": True,
            "reason": "#174 migrated daily seed to configs/watchlist_registry_manifest.json; market_query still loads the retained legacy path",
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", required=True, help="previous 40-character registry SHA")
    parser.add_argument("--after", required=True, help="new 40-character registry SHA")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--output", default=None)
    args = parser.parse_args(argv)
    try:
        report = verify(
            args.before,
            args.after,
            base_url=args.base_url,
            timeout=args.timeout,
            limit=args.limit,
        )
    except Exception as error:
        report = {
            "schema_version": RECEIPT_SCHEMA,
            "observed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "status": "blocked",
            "before_registry_sha": args.before,
            "after_registry_sha": args.after,
            "blocker": f"{type(error).__name__}: {error}",
            "real_daily_verification": "owner_pending",
            "legacy_manifest_audit": {
                "path": "configs/watchlist_manifest.json",
                "status": "not_reached",
                "not_changed": True,
            },
        }
    rendered = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    print(rendered, end="")
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
    return 0 if report["status"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
