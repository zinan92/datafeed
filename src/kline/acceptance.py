"""Evidence-gated 30-day MVP acceptance receipt."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Any, Mapping

from kline.mvp_manifest import MvpManifest, manifest_digest


ACCEPTANCE_SCHEMA_VERSION = "mvp-acceptance-v1"
REAL_EVIDENCE_KIND = "real_authorized"


@dataclass(frozen=True)
class AcceptanceResult:
    status: str
    generated_at: str
    manifest_version: str
    manifest_hash: str
    window_start: str | None
    window_end: str | None
    evidence_kind: str
    criteria: Mapping[str, Mapping[str, Any]]
    blockers: tuple[Mapping[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": ACCEPTANCE_SCHEMA_VERSION,
            "status": self.status,
            "generated_at": self.generated_at,
            "manifest_version": self.manifest_version,
            "manifest_hash": self.manifest_hash,
            "window": {"start": self.window_start, "end": self.window_end},
            "evidence_kind": self.evidence_kind,
            "criteria": {key: dict(value) for key, value in self.criteria.items()},
            "blockers": [dict(blocker) for blocker in self.blockers],
        }


def evaluate_acceptance(
    manifest: MvpManifest,
    *,
    health: Mapping[str, Any] | None = None,
    restore: Mapping[str, Any] | None = None,
    evidence_kind: str = "not_supplied",
    now: datetime | None = None,
) -> AcceptanceResult:
    """Return ``verified`` only when every real-data gate has evidence."""

    current = now or datetime.now(timezone.utc)
    generated_at = _iso(current)
    digest = manifest_digest(manifest)
    blockers: list[dict[str, Any]] = []
    criteria: dict[str, dict[str, Any]] = {}
    status = manifest.selection_policy.get("status")
    criteria["active_manifest"] = {
        "status": "pass" if status == "active" else "blocked",
        "observed": status,
    }
    if status != "active":
        blockers.append(
            {
                "code": "manifest_not_active",
                "detail": "manifest must be activated by a real successful run",
            }
        )
    criteria["entitlement"] = {
        "status": "pass"
        if not any(item.source_status == "blocked_for_entitlement" for item in manifest.instruments)
        else "blocked",
        "blocked_instruments": sum(
            item.source_status == "blocked_for_entitlement" for item in manifest.instruments
        ),
    }
    if criteria["entitlement"]["blocked_instruments"]:
        blockers.append(
            {
                "code": "entitlement_blocked",
                "detail": "one or more manifest sources lack valid persistence entitlement",
            }
        )
    criteria["evidence_kind"] = {
        "status": "pass" if evidence_kind == REAL_EVIDENCE_KIND else "blocked",
        "observed": evidence_kind,
    }
    if evidence_kind != REAL_EVIDENCE_KIND:
        blockers.append(
            {
                "code": "real_evidence_required",
                "detail": "mock/parser/HTTP-200 evidence cannot verify the MVP",
            }
        )

    window_start = _parse_optional(manifest.effective_at)
    window_end = window_start + timedelta(days=30) if window_start else None
    criteria["thirty_day_window"] = {
        "status": "pass" if window_end and window_end <= current else "blocked",
        "start": _iso(window_start) if window_start else None,
        "end": _iso(window_end) if window_end else None,
    }
    if window_end is None:
        blockers.append(
            {"code": "window_not_started", "detail": "active manifest effective_at is missing"}
        )
    elif window_end > current:
        blockers.append(
            {"code": "window_not_elapsed", "detail": "30 calendar days have not elapsed"}
        )

    health_status = health.get("status") if health else None
    last_run = health.get("last_run") if health else None
    health_pass = (
        health_status == "ready"
        and isinstance(last_run, Mapping)
        and last_run.get("status") == "success"
    )
    criteria["real_coverage"] = {
        "status": "pass" if health_pass else "blocked",
        "health_status": health_status,
    }
    if not health_pass:
        blockers.append(
            {
                "code": "coverage_not_verified",
                "detail": "health must show a successful real-data run",
            }
        )

    restore_pass = bool(
        restore and restore.get("status") == "verified" and restore.get("manifest_hash") == digest
    )
    criteria["restore"] = {
        "status": "pass" if restore_pass else "blocked",
        "observed": restore.get("status") if restore else None,
    }
    if not restore_pass:
        blockers.append(
            {
                "code": "restore_not_verified",
                "detail": "clean NAS restore with matching manifest hash is required",
            }
        )

    if health and health.get("row_counts", {}).get("candles", 0) <= 0:
        blockers.append(
            {
                "code": "no_serving_rows",
                "detail": "health row counts contain no persisted MVP candles",
            }
        )
    criteria["membership_freeze"] = {
        "status": "pass"
        if status == "active"
        and manifest.selection_policy.get("freeze_days_after_first_success") == 30
        else "blocked"
    }
    if criteria["membership_freeze"]["status"] != "pass":
        blockers.append(
            {
                "code": "freeze_policy_mismatch",
                "detail": "active manifest must freeze membership for 30 days",
            }
        )

    return AcceptanceResult(
        status="verified" if not blockers else "blocked",
        generated_at=generated_at,
        manifest_version=manifest.version,
        manifest_hash=digest,
        window_start=_iso(window_start) if window_start else None,
        window_end=_iso(window_end) if window_end else None,
        evidence_kind=evidence_kind,
        criteria=criteria,
        blockers=tuple(blockers),
    )


def render_markdown(result: AcceptanceResult) -> str:
    """Render a human-readable receipt without hiding blocker state."""

    lines = [
        "# Market Data Database MVP Acceptance",
        "",
        f"- Status: **{result.status}**",
        f"- Generated: `{result.generated_at}`",
        f"- Manifest: `{result.manifest_version}`",
        f"- Manifest hash: `{result.manifest_hash}`",
        f"- Evidence kind: `{result.evidence_kind}`",
        f"- Window: `{result.window_start or 'not started'}` → `{result.window_end or 'not started'}`",
        "",
        "## Criteria",
        "",
    ]
    for name, criterion in result.criteria.items():
        lines.append(
            f"- `{name}`: **{criterion.get('status', 'unknown')}** — {json.dumps(dict(criterion), ensure_ascii=False, sort_keys=True)}"
        )
    lines.extend(["", "## Blockers", ""])
    if result.blockers:
        lines.extend(f"- `{item.get('code')}`: {item.get('detail')}" for item in result.blockers)
    else:
        lines.append("- none")
    lines.append("")
    return "\n".join(lines)


def write_artifacts(
    result: AcceptanceResult, *, json_path: str | Path, markdown_path: str | Path
) -> None:
    """Write explicit JSON + Markdown receipts to caller-selected paths."""

    json_target = Path(json_path)
    markdown_target = Path(markdown_path)
    json_target.parent.mkdir(parents=True, exist_ok=True)
    markdown_target.parent.mkdir(parents=True, exist_ok=True)
    json_target.write_text(
        json.dumps(result.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    markdown_target.write_text(render_markdown(result), encoding="utf-8")


def _parse_optional(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()
