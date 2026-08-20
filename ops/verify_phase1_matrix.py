#!/usr/bin/env python3
"""Read-only HTTP verification for the canonical Phase 1 39-cell matrix.

The script only performs GET requests and writes no database or cache files.
Run it against a disposable/canonical runtime when a strict no-persistence
receipt is required; it does not restart services or alter launchd/.env.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

try:
    from .phase1_matrix import PHASE1_MATRIX_VERSION, PHASE1_POLICIES, expected_provider_symbol, required_cells
except ImportError:  # Direct ``python ops/verify_phase1_matrix.py`` execution.
    from phase1_matrix import PHASE1_MATRIX_VERSION, PHASE1_POLICIES, expected_provider_symbol, required_cells


def _get_json(url: str, *, timeout: float) -> tuple[int, Any]:
    request = Request(url, headers={"Accept": "application/json"}, method="GET")
    try:
        with urlopen(request, timeout=timeout) as response:
            return int(getattr(response, "status", 200)), json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        try:
            body = json.loads(error.read().decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            body = {"error": type(error).__name__, "detail": str(error)}
        return int(error.code), body
    except (URLError, TimeoutError, OSError, ValueError) as error:
        return 0, {"error": type(error).__name__, "detail": str(error)}


def _detail_mapping(payload: Any) -> Mapping[str, Any]:
    detail = payload.get("detail") if isinstance(payload, Mapping) else payload
    return detail if isinstance(detail, Mapping) else payload if isinstance(payload, Mapping) else {}


def _error_detail(payload: Any) -> tuple[str, str, Mapping[str, Any]]:
    detail = _detail_mapping(payload)
    reason = str(detail.get("reject_reason") or detail.get("error") or "http_error")
    message = str(detail.get("detail") or detail.get("error") or reason)
    return reason, message, detail


def _cell_result(cell: Mapping[str, Any], status: int, payload: Any) -> dict[str, Any]:
    if status == 200 and isinstance(payload, Mapping):
        candles = payload.get("candles")
        reject_reason = payload.get("reject_reason") or (None if isinstance(candles, list) and candles else "empty_data")
        issues: list[str] = []
        expected = {
            "timeframe": cell["timeframe"],
            "requested_source": cell["source"],
            "selected_source": cell["source"],
            "cache_policy": cell["cache_policy"],
            "quality_policy": cell["quality_policy"],
            "fallback_policy": cell["fallback_policy"],
            "served_from": "upstream",
            "is_synthetic": False,
        }
        for field, value in expected.items():
            if payload.get(field) != value:
                issues.append(f"{field}_mismatch")
        if payload.get("attempted_sources") != [cell["source"]]:
            issues.append("attempted_sources_mismatch")
        expected_symbol = expected_provider_symbol(cell["asset_key"])
        if payload.get("provider_symbol") != expected_symbol:
            issues.append("provider_symbol_mismatch")
        if not isinstance(payload.get("source_identity"), Mapping) or not payload["source_identity"]:
            issues.append("source_identity_missing")
        if cell["timeframe"] == "4h":
            if payload.get("raw_timeframe") not in {"1h", "4h"}:
                issues.append("raw_timeframe_missing")
            if payload.get("timeframe_origin") not in {"native", "aggregated"}:
                issues.append("timeframe_origin_missing")
            if not isinstance(payload.get("aggregation"), Mapping) or not payload["aggregation"]:
                issues.append("aggregation_missing")
        if issues and isinstance(candles, list) and candles:
            reject_reason = f"contract_mismatch:{issues[0]}"
        cell_status = "ready" if isinstance(candles, list) and candles and not reject_reason and not issues else "blocked"
        return {
            **cell,
            "status": cell_status,
            "http_status": status,
            "count": len(candles) if isinstance(candles, list) else 0,
            "provider": payload.get("provider"),
            "provider_symbol": payload.get("provider_symbol"),
            "selected_source": payload.get("selected_source"),
            "requested_source": payload.get("requested_source"),
            "raw_timeframe": payload.get("raw_timeframe"),
            "timeframe_origin": payload.get("timeframe_origin"),
            "source_identity": payload.get("source_identity") or {},
            "reject_reason": reject_reason,
            "access_issues": [*payload.get("access_issues", []), *issues],
            "attempted_sources": payload.get("attempted_sources") or [],
            "served_from": payload.get("served_from"),
            "cache_policy": payload.get("cache_policy"),
            "quality_policy": payload.get("quality_policy"),
            "fallback_policy": payload.get("fallback_policy"),
        }
    reason, message, detail = _error_detail(payload)
    return {
        **cell,
        "status": "unavailable" if status in {0, 404} else "blocked",
        "http_status": status,
        "count": 0,
        "provider": None,
        "provider_symbol": detail.get("provider_symbol") or cell["ticker"],
        "selected_source": detail.get("selected_source"),
        "requested_source": detail.get("requested_source") or cell["source"],
        "raw_timeframe": detail.get("raw_timeframe"),
        "timeframe_origin": detail.get("timeframe_origin"),
        "source_identity": {
            **(dict(detail.get("source_identity")) if isinstance(detail.get("source_identity"), Mapping) else {}),
            "requested_source": cell["source"],
            "requested_ticker": cell["ticker"],
        },
        "reject_reason": reason,
        "access_issues": [message],
        "attempted_sources": detail.get("attempted_sources") or [cell["source"]],
        "served_from": detail.get("served_from"),
        "quality_policy": detail.get("quality_policy") or cell["quality_policy"],
        "fallback_policy": detail.get("fallback_policy") or cell["fallback_policy"],
        "cache_policy": detail.get("cache_policy") or cell["cache_policy"],
    }


def _health_contract_issues(status: int, health: Any, db_path: str | None = None) -> list[str]:
    if status != 200 or not isinstance(health, Mapping):
        return ["health_unavailable"]
    issues: list[str] = []
    runtime = health.get("runtime")
    if not isinstance(runtime, Mapping):
        issues.append("health_runtime_missing")
    else:
        for field in ("build_sha", "runtime_root", "module_root", "registry_version", "database_path"):
            if not isinstance(runtime.get(field), str) or not runtime[field].strip():
                issues.append(f"health_runtime_{field}_missing")
        if runtime.get("registry_version") != "weekly-macro-phase1-source-registry-v1":
            issues.append("health_registry_version_mismatch")
        if runtime.get("build_sha") == "unknown":
            issues.append("health_build_sha_unknown")
        if db_path and isinstance(runtime.get("database_path"), str):
            if Path(runtime["database_path"]).resolve() != Path(db_path).resolve():
                issues.append("health_database_path_mismatch")
    sources = ((health.get("providers") or {}).get("sources") if isinstance(health.get("providers"), Mapping) else None)
    if not isinstance(sources, Mapping):
        return [*issues, "health_sources_missing"]
    for source in sorted({cell["source"] for cell in required_cells()}):
        entry = sources.get(source)
        if not isinstance(entry, Mapping):
            issues.append(f"health_source_missing:{source}")
        elif entry.get("configured") is not True:
            issues.append(f"health_source_not_configured:{source}")
        elif entry.get("availability_basis") not in {"not_live_probed", "live_probe"}:
            issues.append(f"health_source_availability_basis_invalid:{source}")
        elif type(entry.get("available")) is not bool:
            issues.append(f"health_source_available_invalid:{source}")
        elif entry.get("availability_basis") == "not_live_probed" and entry.get("available") is not False:
            issues.append(f"health_source_not_live_status_invalid:{source}")
    return issues


def _database_receipt(path: str | None) -> dict[str, Any] | None:
    if not path:
        return None
    database = Path(path)
    if not database.exists():
        return {"exists": False, "verified": False}
    try:
        uri = f"file:{database.resolve()}?mode=ro"
        with sqlite3.connect(uri, uri=True) as connection:
            counts = {}
            for table in ("klines", "source_observations", "raw_upstream_responses"):
                try:
                    counts[table] = int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                except sqlite3.Error:
                    counts[table] = None
        verified = all(isinstance(counts.get(table), int) for table in ("klines", "source_observations", "raw_upstream_responses"))
        return {"exists": True, "verified": verified, "counts": counts}
    except sqlite3.Error as error:
        return {"exists": True, "verified": False, "error": type(error).__name__}


def verify(
    base_url: str,
    *,
    timeout: float = 30.0,
    limit: int = 3,
    db_path: str | None = None,
) -> dict[str, Any]:
    base = base_url.rstrip("/")
    database_before = _database_receipt(db_path)
    health_status, health = _get_json(f"{base}/api/health", timeout=timeout)
    results: list[dict[str, Any]] = []
    health_issues = _health_contract_issues(health_status, health, db_path)
    for cell in required_cells():
        if health_issues:
            result = _cell_result(
                cell,
                503,
                {
                    "error": "health_contract_invalid",
                    "detail": {
                        "reject_reason": ";".join(health_issues),
                        "source_identity": {"health_registry": "invalid"},
                    },
                },
            )
            results.append(result)
            continue
        query = {
            "timeframe": cell["timeframe"],
            "source": cell["source"],
            "cache_policy": PHASE1_POLICIES["cache_policy"],
            "quality": PHASE1_POLICIES["quality"],
            "fallback_policy": PHASE1_POLICIES["fallback_policy"],
            "limit": str(limit),
        }
        path = f"/api/candles/{cell['asset_class']}/{cell['ticker']}?{urlencode(query)}"
        status, payload = _get_json(f"{base}{path}", timeout=timeout)
        results.append(_cell_result(cell, status, payload))

    counts = {
        state: sum(1 for result in results if result["status"] == state)
        for state in ("ready", "unavailable", "blocked")
    }
    database_after = _database_receipt(db_path)
    return {
        "schema_version": "phase1-matrix-receipt-v1",
        "matrix_version": PHASE1_MATRIX_VERSION,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "base_url": base,
        "health_http_status": health_status,
        "health_contract_issues": health_issues,
        "runtime": health.get("runtime") if isinstance(health, Mapping) else None,
        "health": health,
        "counts": counts,
        "cells": results,
        "read_only_tool": True,
        "database_before": database_before,
        "database_after": database_after,
        "database_unchanged": (
            database_before == database_after
            if (
                database_before is not None
                and database_after is not None
                and database_before.get("verified") is True
                and database_after.get("verified") is True
            )
            else None
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--db-path", required=True)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--limit", type=int, default=3)
    args = parser.parse_args()
    receipt = verify(args.base_url, timeout=args.timeout, limit=args.limit, db_path=args.db_path)
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    total_cells = sum(receipt["counts"].values())
    has_ready = receipt["counts"]["ready"] > 0
    matrix_valid = len(receipt["cells"]) == 39 and total_cells == 39 and has_ready
    return 0 if not receipt["health_contract_issues"] and receipt["database_unchanged"] is True and matrix_valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
