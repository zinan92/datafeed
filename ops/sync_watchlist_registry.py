"""Materialize a pinned Park Exposure Registry YAML into offline artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import tempfile
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

import yaml
import kline.watchlist_registry as registry_module

from kline.watchlist_registry import (
    APPROVED_WATCHLIST_COMMIT,
    REGISTRY_REPOSITORY,
    REGISTRY_SNAPSHOT_SCHEMA,
    compile_watchlist_manifest,
)
from kline.watchlist_manifest import validate_watchlist_manifest


DEFAULT_REF = "main"
DEFAULT_SNAPSHOT_OUTPUT = Path("configs/watchlist_registry.snapshot.json")
DEFAULT_MANIFEST_OUTPUT = Path("configs/watchlist_registry_manifest.json")
DEFAULT_RECEIPT = Path("~/park-data/market/watchlist-registry-receipt.json")
REGISTRY_FILE = "watchlist.yaml"
RECEIPT_SCHEMA = "watchlist-registry-receipt-v1"
GITHUB_API = "https://api.github.com"
GITHUB_RAW = "https://raw.githubusercontent.com"


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
    if not isinstance(commit, str) or len(commit) != 40 or any(
        character not in "0123456789abcdef" for character in commit
    ):
        raise ValueError("source commit must be a 40-character lowercase SHA")
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
    temporary = target.with_name(f".{target.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(target)


def _fetch_json(url: str) -> Mapping[str, Any]:
    request = Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "datafeed-watchlist-sync"})
    try:
        with urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as error:
        raise RuntimeError(f"registry ref lookup failed: {error}") from error
    if not isinstance(payload, Mapping):
        raise RuntimeError("registry ref lookup returned a non-object response")
    return payload


def fetch_registry(ref: str) -> tuple[str, bytes]:
    """Fetch exactly one upstream registry revision; never fall back to a local copy."""
    requested_ref = _text(ref, "ref")
    ref_url = f"{GITHUB_API}/repos/{REGISTRY_REPOSITORY}/commits/{quote(requested_ref, safe='')}"
    metadata = _fetch_json(ref_url)
    commit = metadata.get("sha")
    if not isinstance(commit, str) or len(commit) != 40:
        raise RuntimeError("registry ref lookup did not return a 40-character commit SHA")
    source_url = f"{GITHUB_RAW}/{REGISTRY_REPOSITORY}/{commit}/{REGISTRY_FILE}"
    request = Request(source_url, headers={"User-Agent": "datafeed-watchlist-sync"})
    try:
        with urlopen(request, timeout=30) as response:
            raw_bytes = response.read()
    except (HTTPError, URLError, TimeoutError) as error:
        raise RuntimeError(f"registry fetch failed for {commit}: {error}") from error
    if not raw_bytes.strip():
        raise RuntimeError(f"registry file is empty at {commit}")
    return commit, raw_bytes


def build_snapshot_from_bytes(raw_bytes: bytes, *, commit: str) -> dict[str, Any]:
    payload = yaml.safe_load(raw_bytes)
    if not isinstance(payload, Mapping):
        raise ValueError("registry YAML must be an object")
    # Keep the established compiler as the single normalization contract while allowing
    # the runtime sync to consume bytes fetched from an immutable upstream revision.
    with tempfile.TemporaryDirectory(prefix="datafeed-watchlist-") as directory:
        temporary = Path(directory) / "watchlist.yaml"
        temporary.write_bytes(raw_bytes)
        return build_snapshot(temporary, commit=commit)


def _compile_manifest(snapshot_payload: Mapping[str, Any]) -> dict[str, Any]:
    """Use the existing compiler while accepting the fetched immutable revision."""
    commit = snapshot_payload["upstream"]["commit"]
    original_commit = registry_module.APPROVED_WATCHLIST_COMMIT
    registry_module.APPROVED_WATCHLIST_COMMIT = commit
    try:
        return compile_watchlist_manifest(snapshot_payload)
    finally:
        registry_module.APPROVED_WATCHLIST_COMMIT = original_commit


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Materialize a pinned Watchlist registry snapshot")
    parser.add_argument("--ref", default=DEFAULT_REF, help="watchlist branch, tag, or commit")
    parser.add_argument("--source", help="Local YAML fixture/source (for offline verification)")
    parser.add_argument("--commit", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--snapshot-output", default=str(DEFAULT_SNAPSHOT_OUTPUT))
    parser.add_argument("--manifest-output", default=str(DEFAULT_MANIFEST_OUTPUT))
    parser.add_argument("--receipt", default=str(DEFAULT_RECEIPT))
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    receipt_path = Path(args.receipt).expanduser().resolve()
    observed_at = datetime.now(timezone.utc).isoformat()
    try:
        if args.source:
            commit = args.commit or APPROVED_WATCHLIST_COMMIT
            snapshot_payload = build_snapshot(args.source, commit=commit)
        else:
            commit, raw_bytes = fetch_registry(args.ref)
            snapshot_payload = build_snapshot_from_bytes(raw_bytes, commit=commit)
        snapshot_path = Path(args.snapshot_output).expanduser().resolve()
        manifest_path = Path(args.manifest_output).expanduser().resolve()
        manifest = _compile_manifest(snapshot_payload)
        validate_watchlist_manifest(manifest)
        manifest_bytes = json.dumps(
            manifest, ensure_ascii=False, sort_keys=True, indent=2
        ).encode("utf-8") + b"\n"
        previous: Mapping[str, Any] = {}
        if manifest_path.exists():
            try:
                previous = json.loads(manifest_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                previous = {}
        previous_ids = {str(item["instrument_id"]) for item in previous.get("instruments", [])}
        current_ids = {str(item["instrument_id"]) for item in manifest["instruments"]}
        manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
        old_receipt: Mapping[str, Any] = {}
        if receipt_path.exists():
            try:
                old_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                old_receipt = {}
        receipt = {
            "schema_version": RECEIPT_SCHEMA,
            "observed_at": observed_at,
            "status": "success",
            "changed": not (
                old_receipt.get("registry_sha") == commit
                and old_receipt.get("manifest_hash") == manifest_hash
            ),
            "registry_repository": REGISTRY_REPOSITORY,
            "registry_ref": args.ref,
            "registry_sha": commit,
            "registry_source_sha256": snapshot_payload["upstream"]["source_sha256"],
            "manifest_hash": manifest_hash,
            "added_instruments": sorted(current_ids - previous_ids),
            "removed_instruments": sorted(previous_ids - current_ids),
            "instrument_ids": sorted(current_ids),
        }
        if not args.dry_run:
            _write_json(snapshot_path, snapshot_payload)
            _write_json(manifest_path, manifest)
            _write_json(receipt_path, receipt)
        print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as error:
        blocker = {
            "schema_version": RECEIPT_SCHEMA,
            "observed_at": observed_at,
            "status": "blocked",
            "changed": False,
            "registry_repository": REGISTRY_REPOSITORY,
            "registry_ref": args.ref,
            "blocker": f"{type(error).__name__}: {error}",
        }
        if not args.dry_run:
            _write_json(receipt_path, blocker)
        print(json.dumps(blocker, ensure_ascii=False, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
