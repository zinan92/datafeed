from __future__ import annotations

import json
from pathlib import Path

import pytest

from kline.models import AssetClass
from kline.watchlist_registry import (
    APPROVED_WATCHLIST_COMMIT,
    RegistryError,
    compile_watchlist_manifest,
    load_registry_snapshot,
)
from kline.watchlist_manifest import validate_watchlist_manifest


ROOT = Path(__file__).parents[1]
SNAPSHOT = ROOT / "configs" / "watchlist_registry.snapshot.json"
COMPILED = ROOT / "configs" / "watchlist_registry_manifest.json"


def test_pinned_registry_compiles_to_107_unique_daily_instruments() -> None:
    snapshot = load_registry_snapshot(SNAPSHOT)
    payload = compile_watchlist_manifest(snapshot)
    manifest = validate_watchlist_manifest(payload)

    assert snapshot.upstream_commit == APPROVED_WATCHLIST_COMMIT
    assert len(manifest.instruments) == 107
    assert sum(item.asset_class == "a_share" for item in manifest.instruments) == 38
    assert sum(
        item.metadata.get("registry_market") == "US"
        and item.metadata.get("registry_target_type") == "company"
        for item in manifest.instruments
    ) == 48
    assert sum(item.asset_class == "hk_stock" for item in manifest.instruments) == 4
    assert sum(
        item.metadata.get("registry_market") == "KR"
        and item.metadata.get("registry_target_type") == "company"
        for item in manifest.instruments
    ) == 1
    assert {item.display_symbol for item in manifest.instruments} >= {
        "SPX", "NDX", "DXY", "SCHD", "VIX", "BTC", "ETH", "HYPE",
        "sh000001", "sh000688", "sh000015", "^N225", "^KS11", "CL=F", "GC=F", "SI=F",
    }
    assert all(item.required_timeframes == ("1d",) for item in manifest.instruments)


def test_compiled_artifact_is_byte_stable_and_records_registry_provenance() -> None:
    snapshot = load_registry_snapshot(SNAPSHOT)
    first = compile_watchlist_manifest(snapshot)
    second = compile_watchlist_manifest(load_registry_snapshot(SNAPSHOT))

    assert json.dumps(first, ensure_ascii=False, sort_keys=True, separators=(",", ":")) == json.dumps(
        second, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    artifact = json.loads(COMPILED.read_text(encoding="utf-8"))
    assert artifact == first
    assert artifact["registry"]["repository"] == "zinan92/watchlist"
    assert artifact["registry"]["commit"] == APPROVED_WATCHLIST_COMMIT
    assert len(artifact["registry"]["source_sha256"]) == 64


def test_registry_excludes_unlisted_and_theme_targets_and_deduplicates_memberships() -> None:
    snapshot = load_registry_snapshot(SNAPSHOT)
    payload = compile_watchlist_manifest(snapshot)
    ids = {item["display_symbol"] for item in payload["instruments"]}

    assert "federal-reserve" not in ids
    assert "OpenAI" not in ids
    assert "longxin" not in ids
    mrvl = next(item for item in payload["instruments"] if item["display_symbol"] == "MRVL")
    assert len(mrvl["metadata"]["registry_memberships"]) > 1
    assert len(ids) == 107


def test_registry_rejects_unapproved_commit_and_invalid_company_code(tmp_path: Path) -> None:
    snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    snapshot["upstream"]["commit"] = "0" * 40
    path = tmp_path / "wrong-commit.json"
    path.write_text(json.dumps(snapshot), encoding="utf-8")
    with pytest.raises(RegistryError, match="approved pinned commit"):
        load_registry_snapshot(path)

    valid = load_registry_snapshot(SNAPSHOT)
    raw = valid.to_dict()
    raw["targets"].append(
        {
            "id": "BAD",
            "type": "company",
            "market": "CN",
            "ticker": "not-a-code",
            "listed": True,
            "reason": "test",
            "memberships": [],
        }
    )
    with pytest.raises(RegistryError, match="CN ticker"):
        compile_watchlist_manifest(raw)


def test_hong_kong_asset_class_is_a_truthful_source_identity() -> None:
    snapshot = load_registry_snapshot(SNAPSHOT)
    payload = compile_watchlist_manifest(snapshot)
    hong_kong = [item for item in payload["instruments"] if item["asset_class"] == AssetClass.HK_STOCK.value]
    assert {(item["display_symbol"], item["provider_symbol"]) for item in hong_kong} == {
        ("00100", "0100.HK"),
        ("02513", "2513.HK"),
        ("00700", "0700.HK"),
        ("09988", "9988.HK"),
    }
    assert all(item["source_id"] == "yahoo_finance_hk" for item in hong_kong)
