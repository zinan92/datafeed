from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from kline.acceptance import evaluate_acceptance, render_markdown, write_artifacts
from kline.mvp_manifest import load_manifest


MANIFEST_PATH = Path(__file__).parents[1] / "configs" / "mvp_manifest.json"


def test_acceptance_remains_blocked_without_real_entitlement_and_30_day_evidence(
    tmp_path: Path,
) -> None:
    manifest = load_manifest(MANIFEST_PATH)
    result = evaluate_acceptance(
        manifest,
        evidence_kind="synthetic_test",
        now=datetime(2026, 9, 30, tzinfo=timezone.utc),
    )

    assert result.status == "blocked"
    codes = {item["code"] for item in result.blockers}
    assert {
        "manifest_not_active",
        "entitlement_blocked",
        "real_evidence_required",
        "window_not_started",
        "coverage_not_verified",
        "restore_not_verified",
    }.issubset(codes)
    markdown = render_markdown(result)
    assert "Status: **blocked**" in markdown
    assert "real_evidence_required" in markdown

    json_path = tmp_path / "acceptance.json"
    markdown_path = tmp_path / "acceptance.md"
    write_artifacts(result, json_path=json_path, markdown_path=markdown_path)
    assert json_path.exists() and markdown_path.exists()
    assert '"status": "blocked"' in json_path.read_text(encoding="utf-8")


def test_acceptance_does_not_treat_verified_restore_without_matching_manifest_as_success() -> None:
    manifest = load_manifest(MANIFEST_PATH)
    result = evaluate_acceptance(
        manifest,
        health={"status": "ready", "last_run": {"status": "success"}, "row_counts": {"candles": 1}},
        restore={"status": "verified", "manifest_hash": "0" * 64},
        evidence_kind="real_authorized",
        now=datetime(2026, 9, 30, tzinfo=timezone.utc),
    )
    assert result.status == "blocked"
    assert any(item["code"] == "manifest_not_active" for item in result.blockers)
    assert any(item["code"] == "restore_not_verified" for item in result.blockers)
