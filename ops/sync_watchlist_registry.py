"""Materialize a pinned Park Exposure Registry YAML into offline artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from kline.watchlist_registry import (
    APPROVED_WATCHLIST_COMMIT,
    REGISTRY_REPOSITORY,
    REGISTRY_SNAPSHOT_SCHEMA,
    compile_watchlist_manifest,
)
from kline.watchlist_manifest import validate_watchlist_manifest


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def build_snapshot(source_path: str | Path, *, commit: str = APPROVED_WATCHLIST_COMMIT) -> dict[str, Any]:
    source = Path(source_path)
    raw_bytes = source.read_bytes()
    payload = yaml.safe_load(raw_bytes)
    if not isinstance(payload, Mapping):
        raise ValueError("registry YAML must be an object")
    if commit != APPROVED_WATCHLIST_COMMIT:
        raise ValueError("source commit is not the approved pinned commit")
    assets = payload.get("assets")
    sectors = payload.get("sectors")
    if not isinstance(assets, list) or not isinstance(sectors, list):
        raise ValueError("registry YAML requires assets and sectors")

    normalized_assets: list[dict[str, str]] = []
    for index, item in enumerate(assets):
        if not isinstance(item, Mapping):
            raise ValueError(f"assets[{index}] must be an object")
        normalized_assets.append(
            {
                "id": _text(item.get("id"), f"assets[{index}].id"),
                "kind": _text(item.get("kind"), f"assets[{index}].kind"),
                "market": _text(item.get("market"), f"assets[{index}].market"),
                "name": _text(item.get("name"), f"assets[{index}].name"),
            }
        )

    targets: dict[str, dict[str, Any]] = {}
    for sector_index, sector in enumerate(sectors):
        if not isinstance(sector, Mapping):
            raise ValueError(f"sectors[{sector_index}] must be an object")
        sector_id = _text(sector.get("id"), f"sectors[{sector_index}].id")
        sector_name = _text(sector.get("name"), f"sectors[{sector_index}].name")
        macro_id = _text(sector.get("macro"), f"sectors[{sector_index}].macro")
        sector_targets = sector.get("targets")
        if not isinstance(sector_targets, list):
            raise ValueError(f"sectors[{sector_index}].targets must be a list")
        for target_index, target in enumerate(sector_targets):
            if not isinstance(target, Mapping):
                raise ValueError(f"sector target {sector_index}/{target_index} must be an object")
            target_id = _text(target.get("id"), f"target {sector_index}/{target_index}.id")
            key = target_id.casefold()
            target_type = _text(target.get("type"), f"target {target_id}.type")
            listed = target.get("listed")
            if target_type == "asset" and listed is None:
                listed = False
            base = {
                "id": target_id,
                "type": target_type,
                "market": target.get("market"),
                "name": str(target.get("name") or target_id).strip(),
                "listed": listed,
                "reason": _text(target.get("reason"), f"target {target_id}.reason"),
                "ticker": target.get("ticker"),
                "reasons": [_text(target.get("reason"), f"target {target_id}.reason")],
            }
            if base["market"] is not None:
                base["market"] = _text(base["market"], f"target {target_id}.market")
            if base["ticker"] is not None:
                base["ticker"] = _text(base["ticker"], f"target {target_id}.ticker")
            if not isinstance(base["listed"], bool):
                raise ValueError(f"target {target_id}.listed must be boolean")
            existing = targets.get(key)
            if existing is None:
                existing = {**base, "memberships": []}
                targets[key] = existing
            else:
                for field in ("type", "market", "name", "listed", "ticker"):
                    if existing[field] != base[field]:
                        raise ValueError(f"duplicate target {target_id} has conflicting {field}")
                if base["reason"] not in existing["reasons"]:
                    existing["reasons"].append(base["reason"])
            existing["memberships"].append(
                {"sector_id": sector_id, "sector_name": sector_name, "macro_id": macro_id}
            )

    return {
        "schema_version": REGISTRY_SNAPSHOT_SCHEMA,
        "upstream": {
            "repository": REGISTRY_REPOSITORY,
            "commit": commit,
            "source_sha256": hashlib.sha256(raw_bytes).hexdigest(),
            "version": payload.get("version"),
            "updated": _text(payload.get("updated"), "updated"),
        },
        "assets": normalized_assets,
        "targets": list(targets.values()),
    }


def _write_json(path: str | Path, payload: Mapping[str, Any]) -> None:
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Materialize a pinned Watchlist registry snapshot")
    parser.add_argument("--source", required=True, help="Pinned watchlist.yaml input")
    parser.add_argument("--commit", default=APPROVED_WATCHLIST_COMMIT)
    parser.add_argument("--snapshot-output", required=True)
    parser.add_argument("--manifest-output", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    snapshot_payload = build_snapshot(args.source, commit=args.commit)
    snapshot_path = Path(args.snapshot_output).expanduser().resolve()
    _write_json(snapshot_path, snapshot_payload)
    from kline.watchlist_registry import load_registry_snapshot

    manifest = compile_watchlist_manifest(load_registry_snapshot(snapshot_path))
    validate_watchlist_manifest(manifest)
    _write_json(args.manifest_output, manifest)
    print(
        json.dumps(
            {
                "snapshot": str(snapshot_path),
                "manifest": str(Path(args.manifest_output).expanduser().resolve()),
                "asset_count": len(manifest["instruments"]),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
