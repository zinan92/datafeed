from __future__ import annotations

from pathlib import Path
from dataclasses import replace

import pytest
import kline.mvp_manifest as manifest_module

from kline.mvp_manifest import (
    ALLOWED_TIMEFRAMES,
    ManifestError,
    activate_manifest,
    build_selection_receipt,
    load_manifest,
    manifest_digest,
    replace_candidate,
    validate_manifest,
)


MANIFEST_PATH = Path(__file__).parents[1] / "configs" / "mvp_manifest.json"


class _ReadySource:
    def __init__(self, asset_class: str) -> None:
        self.asset_class = asset_class

    def supports_timeframe(self, _provider_symbol: str, _timeframe: object) -> bool:
        return True


def _ready_manifest(monkeypatch: pytest.MonkeyPatch):
    manifest = load_manifest(MANIFEST_PATH)
    payload = manifest.to_dict()
    source_registry = {
        item["source_id"]: _ReadySource(item["asset_class"]) for item in payload["instruments"]
    }
    for item in payload["instruments"]:
        item["source_status"] = "configured"
        item["not_applicable_timeframes"] = list(
            dict.fromkeys(item["not_applicable_timeframes"] + item["blocked_timeframes"])
        )
        item["blocked_timeframes"] = []
        missing = [
            timeframe
            for timeframe in ALLOWED_TIMEFRAMES
            if timeframe not in item["required_timeframes"] + item["not_applicable_timeframes"]
        ]
        item["not_applicable_timeframes"].extend(missing)
    payload["selection_policy"]["status"] = "ready"
    monkeypatch.setattr(manifest_module, "_source_registry", lambda: source_registry)
    return validate_manifest(payload)


def _run_receipt(manifest, *, selection_snapshot_hash: str = "f" * 64) -> dict[str, object]:
    return {
        "run_id": "run-20260901-0001",
        "status": "success",
        "manifest_version": manifest.version,
        "manifest_hash": manifest_digest(manifest),
        "completed_at": "2026-09-01T00:00:00Z",
        "coverage": {
            "instrument_count": len(manifest.instruments),
            "required_cells": sum(len(item.required_timeframes) for item in manifest.instruments)
            or 1,
            "persisted_rows": len(manifest.instruments),
            "closed_bars_only": True,
            "selection_snapshot_hash": selection_snapshot_hash,
        },
        "quality": {
            "status": "pass",
            "gaps": 0,
            "duplicates": 0,
            "invalid_rows": 0,
            "blocked_cells": 0,
        },
        "storage": {
            "atomic_commit": True,
            "watermark_advanced": True,
            "receipts_persisted": True,
        },
        "sources": [
            {
                "source_id": source_id,
                "status": "success",
                "instrument_count": sum(
                    item.source_status == "configured" and item.source_id == source_id
                    for item in manifest.instruments
                ),
            }
            for source_id in sorted(
                {
                    item.source_id
                    for item in manifest.instruments
                    if item.source_status == "configured"
                }
            )
        ],
    }


def test_mvp_manifest_has_exact_counts_and_required_fields() -> None:
    manifest = load_manifest(MANIFEST_PATH)

    assert manifest.version == "mvp_universe_v1"
    assert manifest.counts == {"a_share": 100, "us_stock": 100, "cross_market": 16}
    assert len(manifest.instruments) == 216
    assert all(item.instrument_id for item in manifest.instruments)
    assert all(item.provider_symbol for item in manifest.instruments)
    assert all(item.source_id for item in manifest.instruments)
    assert all(
        item.required_timeframes or item.not_applicable_timeframes or item.blocked_timeframes
        for item in manifest.instruments
    )
    assert all(item.aggregation_rule_version for item in manifest.instruments)
    assert all(item.source_status for item in manifest.instruments)


def test_mvp_manifest_contains_user_anchors_and_actual_index_identities() -> None:
    manifest = load_manifest(MANIFEST_PATH)
    by_symbol = {item.display_symbol: item for item in manifest.instruments}
    by_id = {item.instrument_id: item for item in manifest.instruments}

    assert by_symbol["688825"].display_name == "长鑫科技"
    assert by_symbol["688836"].display_name == "宇树科技"
    assert by_symbol["601696"].display_name == "中银证券"
    assert by_symbol["300308"].display_name == "中际旭创"
    assert by_symbol["300394"].display_name == "天孚通信"
    assert by_symbol["300502"].display_name == "新易盛"
    assert by_symbol["688525"].display_name == "佰维存储"
    assert by_id["US.INDEX.SPX"].provider_symbol == "SPX"
    assert by_id["US.INDEX.NDX"].provider_symbol == "NDX"
    assert by_id["US.INDEX.SPX"].security_type == "index"
    assert by_id["US.INDEX.NDX"].security_type == "index"
    assert by_symbol["DXY"].provider_symbol == "UUP"


def test_mvp_manifest_keeps_aliases_and_security_identity_explicit() -> None:
    manifest = load_manifest(MANIFEST_PATH)
    by_symbol = {item.display_symbol: item for item in manifest.instruments}

    assert "FB" in by_symbol["META"].ticker_aliases
    assert by_symbol["META"].ticker_alias_validity["FB"] == {
        "valid_from": None,
        "valid_to": "2022-06-08",
    }
    assert by_symbol["GOOGL"].share_class == "A"
    assert by_symbol["BRK.B"].share_class == "B"
    assert by_symbol["TSM"].security_type == "adr"
    assert by_symbol["TSM"].adr_ratio == "1:5"
    assert by_symbol["PLTR"].venue_valid_from == "2024-11-26"


def test_mvp_manifest_excludes_treasury_and_30m() -> None:
    manifest = load_manifest(MANIFEST_PATH)

    assert manifest.legacy_exclusions == {
        "treasury_symbols": ["DGS2", "DGS10", "T10Y2Y"],
        "timeframes": ["30m"],
    }
    assert manifest.legacy_namespace == {
        "name": "legacy",
        "read_only": True,
        "write_enabled": False,
    }
    assert all(
        item.display_symbol not in {"DGS2", "DGS10", "T10Y2Y"} for item in manifest.instruments
    )
    assert all("30m" not in item.required_timeframes for item in manifest.instruments)
    assert all("30m" not in item.not_applicable_timeframes for item in manifest.instruments)


def test_mvp_manifest_reserves_are_disjoint_and_pre_screened() -> None:
    manifest = load_manifest(MANIFEST_PATH)
    active_symbols = {item.display_symbol for item in manifest.instruments}

    assert len(manifest.reserves["a_share"]) >= 20
    assert len(manifest.reserves["us_stock"]) >= 20
    assert not active_symbols.intersection(manifest.reserves["a_share"])
    assert not active_symbols.intersection(manifest.reserves["us_stock"])


def test_manifest_digest_is_stable_and_validation_rejects_banned_timeframe() -> None:
    manifest = load_manifest(MANIFEST_PATH)

    assert manifest_digest(manifest) == manifest_digest(load_manifest(MANIFEST_PATH))
    detached = manifest.to_dict()
    detached["instruments"][0]["metadata"]["mutated"] = True
    detached["selection_policy"]["mutated"] = True
    assert manifest_digest(manifest) == manifest_digest(load_manifest(MANIFEST_PATH))

    payload = manifest.to_dict()
    configured = next(
        item for item in payload["instruments"] if item["source_status"] == "configured"
    )
    configured["required_timeframes"].append("30m")
    with pytest.raises(ManifestError, match="30m"):
        validate_manifest(payload)


def test_manifest_rejects_treasury_provider_symbols_and_aliases() -> None:
    manifest = load_manifest(MANIFEST_PATH)

    for field_name, value in (("provider_symbol", "DGS2"), ("ticker_aliases", ["T10Y2Y"])):
        payload = manifest.to_dict()
        payload["instruments"][0][field_name] = value
        with pytest.raises(ManifestError, match="Treasury"):
            validate_manifest(payload)


def test_programmatic_manifest_is_revalidated() -> None:
    manifest = load_manifest(MANIFEST_PATH)
    invalid_first = replace(manifest.instruments[0], asset_class="index")
    invalid_manifest = replace(manifest, instruments=(invalid_first, *manifest.instruments[1:]))

    with pytest.raises(ManifestError, match="asset_class mismatch"):
        validate_manifest(invalid_manifest)


def test_manifest_rejects_provider_source_symbol_collisions() -> None:
    manifest = load_manifest(MANIFEST_PATH)
    payload = manifest.to_dict()
    payload["instruments"][1]["provider_symbol"] = payload["instruments"][0]["provider_symbol"]

    with pytest.raises(ManifestError, match="provider source/symbol/asset_class"):
        validate_manifest(payload)


def test_selection_receipt_and_activation_bind_hash_and_effective_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _ready_manifest(monkeypatch)
    selected = [item.display_symbol for item in manifest.instruments if item.universe == "a_share"]
    selection = build_selection_receipt(
        manifest,
        universe="a_share",
        snapshot_url="https://example.test/snapshot.json",
        snapshot_hash="a" * 64,
        window="20 trading days",
        thresholds={"median_amount": 300000000},
        selected=selected,
        rejected=[],
        replacements=[],
        reason="initial candidate gate",
    )
    active, activation = activate_manifest(
        manifest,
        effective_at="2026-09-01T00:00:00Z",
        selection_receipt=selection,
        run_receipt=_run_receipt(manifest, selection_snapshot_hash="a" * 64),
    )

    assert active.effective_at == "2026-09-01T00:00:00Z"
    assert active.selection_policy["status"] == "active"
    assert activation == {
        "manifest_version": "mvp_universe_v1",
        "manifest_hash": manifest_digest(active),
        "effective_at": "2026-09-01T00:00:00Z",
    }
    receipt = build_selection_receipt(
        active,
        universe="a_share",
        snapshot_url="https://example.test/snapshot.json",
        snapshot_hash="a" * 64,
        window="20 trading days",
        thresholds={"median_amount": 300000000},
        selected=["300308"],
        rejected=[],
        replacements=[],
        reason="initial candidate gate",
    )
    assert receipt["manifest_version"] == active.version
    assert receipt["manifest_hash"] == manifest_digest(active)
    assert receipt["snapshot_hash"] == "a" * 64


def test_activation_requires_success_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = load_manifest(MANIFEST_PATH)

    with pytest.raises(ManifestError, match="status=ready"):
        activate_manifest(
            manifest,
            effective_at="2026-09-01T00:00:00Z",
            run_receipt={"status": "success"},
        )

    ready = _ready_manifest(monkeypatch)
    selection = build_selection_receipt(
        ready,
        universe="a_share",
        snapshot_url="https://example.test/snapshot.json",
        snapshot_hash="b" * 64,
        window="20 trading days",
        thresholds={},
        selected=[item.display_symbol for item in ready.instruments if item.universe == "a_share"],
        rejected=[],
        replacements=[],
        reason="completed liquidity gate",
    )
    with pytest.raises(ManifestError, match="missing fields"):
        activate_manifest(
            ready,
            effective_at="2026-09-01T00:00:00Z",
            selection_receipt=selection,
            run_receipt={"status": "success"},
        )
    with pytest.raises(ManifestError, match="validated selection_receipt"):
        activate_manifest(
            ready,
            effective_at="2026-09-01T00:00:00Z",
            run_receipt=_run_receipt(ready, selection_snapshot_hash="b" * 64),
        )
    tampered = {**selection, "manifest_hash": "c" * 64}
    with pytest.raises(ManifestError, match="manifest_hash"):
        activate_manifest(
            ready,
            effective_at="2026-09-01T00:00:00Z",
            selection_receipt=tampered,
            run_receipt=_run_receipt(ready, selection_snapshot_hash="b" * 64),
        )
    failed_selection = {**selection, "status": "failed"}
    with pytest.raises(ManifestError, match="status=success"):
        activate_manifest(
            ready,
            effective_at="2026-09-01T00:00:00Z",
            selection_receipt=failed_selection,
            run_receipt=_run_receipt(ready, selection_snapshot_hash="b" * 64),
        )
    with pytest.raises(ManifestError, match="not bound"):
        activate_manifest(
            ready,
            effective_at="2026-09-01T00:00:00Z",
            selection_receipt=selection,
            run_receipt=_run_receipt(ready, selection_snapshot_hash="c" * 64),
        )


def test_replace_candidate_versions_manifest_and_retains_failed_member() -> None:
    manifest = load_manifest(MANIFEST_PATH)
    replacement = replace(
        manifest.instruments[0],
        instrument_id="CN.RESERVE.601988",
        display_symbol="601988",
        display_name="601988",
        provider_symbol="601988.SH",
        issuer_id="601988",
        venue="CN exchange pending",
    )

    selection = build_selection_receipt(
        manifest,
        universe="a_share",
        snapshot_url="https://example.test/snapshot.json",
        snapshot_hash="d" * 64,
        window="20 trading days",
        thresholds={},
        selected=["601988"],
        rejected=["300308"],
        replacements=[],
        reason="liquidity replacement",
    )
    updated, receipt = replace_candidate(
        manifest,
        universe="a_share",
        failed_symbol="300308",
        replacement=replacement,
        effective_at="2026-09-01T00:00:00Z",
        selection_receipt=selection,
    )

    assert updated.version == "mvp_universe_v1.1"
    assert updated.effective_at == "2026-09-01T00:00:00Z"
    assert any(item.display_symbol == "601988" for item in updated.instruments)
    assert "300308" in updated.reserves["a_share"]
    failed_record = next(
        record
        for record in updated.reserve_records["a_share"]
        if record["display_symbol"] == "300308"
    )
    assert failed_record["status"] == "quarantined"
    assert failed_record["pre_screened"] is False
    assert receipt["replacements"] == [
        {"failed": "300308", "replacement": "601988", "reason": "explicit"}
    ]
    assert receipt["manifest_hash"] == manifest_digest(updated)


def test_replace_candidate_requires_next_reserve_identity_and_order() -> None:
    manifest = load_manifest(MANIFEST_PATH)
    replacement = replace(
        manifest.instruments[0],
        instrument_id="CN.RESERVE.601398",
        display_symbol="601398",
        display_name="601398",
        provider_symbol="601398.SH",
    )
    selection = build_selection_receipt(
        manifest,
        universe="a_share",
        snapshot_url="https://example.test/snapshot.json",
        snapshot_hash="e" * 64,
        window="20 trading days",
        thresholds={},
        selected=["601398"],
        rejected=["300308"],
        replacements=[],
        reason="liquidity replacement",
    )

    with pytest.raises(ManifestError, match="next eligible"):
        replace_candidate(
            manifest,
            universe="a_share",
            failed_symbol="300308",
            replacement=replacement,
            effective_at="2026-09-01T00:00:00Z",
            selection_receipt=selection,
        )


def test_manifest_rejects_malformed_open_ended_dates_and_source_mismatch() -> None:
    manifest = load_manifest(MANIFEST_PATH)

    payload = manifest.to_dict()
    payload["instruments"][0]["venue_valid_from"] = "not-a-date"
    with pytest.raises(ManifestError, match="venue_valid_from"):
        validate_manifest(payload)

    payload = manifest.to_dict()
    payload["instruments"][0]["ticker_aliases"] = ["foo", "FOO"]
    with pytest.raises(ManifestError, match="ticker_aliases"):
        validate_manifest(payload)

    payload = manifest.to_dict()
    payload["instruments"][0]["source_id"] = "tencent_kline"
    payload["instruments"][0]["source_status"] = "configured"
    with pytest.raises(ManifestError, match="serves index"):
        validate_manifest(payload)

    payload = manifest.to_dict()
    payload["instruments"][0]["required_timeframes"] = ["1d"]
    with pytest.raises(ManifestError, match="cannot declare required"):
        validate_manifest(payload)

    payload = manifest.to_dict()
    payload["instruments"][0]["source_status"] = "configured"
    payload["instruments"][0]["required_timeframes"] = ["15m"]
    payload["instruments"][0]["blocked_timeframes"] = []
    payload["instruments"][0]["not_applicable_timeframes"] = ["4h", "1d", "1w"]
    with pytest.raises(ManifestError, match="does not support"):
        validate_manifest(payload)


def test_manifest_rejects_casefolded_cross_instrument_identity_collision() -> None:
    manifest = load_manifest(MANIFEST_PATH)
    payload = manifest.to_dict()
    payload["instruments"][1]["ticker_aliases"] = [
        payload["instruments"][0]["display_symbol"].lower()
    ]
    with pytest.raises(ManifestError, match="collides"):
        validate_manifest(payload)


def test_manifest_reuses_active_identity_rules_for_reserves() -> None:
    manifest = load_manifest(MANIFEST_PATH)

    payload = manifest.to_dict()
    reserve = payload["reserve_records"]["a_share"][0]
    reserve["identity"]["security_type"] = "etf"
    with pytest.raises(ManifestError, match="identity violates instrument contract"):
        validate_manifest(payload)

    payload = manifest.to_dict()
    reserve = payload["reserve_records"]["a_share"][0]
    reserve["provider_symbol"] = "DGS2"
    reserve["identity"]["provider_symbol"] = "DGS2"
    with pytest.raises(ManifestError, match="identity violates instrument contract"):
        validate_manifest(payload)
