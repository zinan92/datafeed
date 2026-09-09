from __future__ import annotations

import json
from pathlib import Path

from ops import sync_watchlist_registry as sync


def test_build_snapshot_preserves_listed_state(tmp_path: Path) -> None:
    source = tmp_path / "watchlist.yaml"
    source.write_text(
        """
version: 1
updated: '2026-09-08'
assets:
  - {id: SPX, kind: index, market: US, name: S&P 500}
sectors:
  - id: macro
    name: Macro
    macro: macro
    targets:
      - {id: Listed Co, type: company, market: US, name: Listed Co, listed: true, ticker: LST, reason: listed}
      - {id: Theme Only, type: company, market: US, name: Theme Only, listed: false, ticker: NOPE, reason: excluded}
""".strip()
        + "\n",
        encoding="utf-8",
    )

    snapshot = sync.build_snapshot(source)

    assert [target["id"] for target in snapshot["targets"]] == ["Listed Co", "Theme Only"]
    assert snapshot["targets"][0]["listed"] is True
    assert snapshot["targets"][1]["listed"] is False


def test_fetch_registry_resolves_ref_and_fetches_the_same_commit(monkeypatch) -> None:
    class Response:
        def __init__(self, body: bytes):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return self.body

    commit = "a" * 40
    calls: list[str] = []

    def fake_urlopen(request, timeout):
        calls.append(request.full_url)
        if "/commits/" in request.full_url:
            return Response(json.dumps({"sha": commit}).encode())
        return Response(b"version: 1\n")

    monkeypatch.setattr(sync, "urlopen", fake_urlopen)
    resolved, raw = sync.fetch_registry("main")

    assert resolved == commit
    assert raw == b"version: 1\n"
    assert calls == [
        f"{sync.GITHUB_API}/repos/{sync.REGISTRY_REPOSITORY}/commits/main",
        f"{sync.GITHUB_RAW}/{sync.REGISTRY_REPOSITORY}/{commit}/{sync.REGISTRY_FILE}",
    ]


def test_fetch_json_sends_bearer_token_from_env(monkeypatch) -> None:
    seen: dict[str, str] = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b"{}"

    def fake_urlopen(request, timeout):
        seen.update(request.headers)
        return Response()

    monkeypatch.setattr(sync, "urlopen", fake_urlopen)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN_FILE", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "tok-123")
    sync._fetch_json("https://api.github.com/x")
    assert seen.get("Authorization") == "Bearer tok-123"


def test_fetch_json_reads_token_file(monkeypatch, tmp_path: Path) -> None:
    seen: dict[str, str] = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b"{}"

    def fake_urlopen(request, timeout):
        seen.update(request.headers)
        return Response()

    token_file = tmp_path / "github-token"
    token_file.write_text("tok-file\n", encoding="utf-8")
    monkeypatch.setattr(sync, "urlopen", fake_urlopen)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN_FILE", str(token_file))
    sync._fetch_json("https://api.github.com/x")
    assert seen.get("Authorization") == "Bearer tok-file"


def test_fetch_json_anonymous_without_token(monkeypatch) -> None:
    seen: dict[str, str] = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b"{}"

    def fake_urlopen(request, timeout):
        seen.update(request.headers)
        return Response()

    monkeypatch.setattr(sync, "urlopen", fake_urlopen)
    for name in ("GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN_FILE"):
        monkeypatch.delenv(name, raising=False)
    sync._fetch_json("https://api.github.com/x")
    assert "Authorization" not in seen


def _existing_manifest(tmp_path: Path) -> Path:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "watchlist-manifest-v1",
                "instruments": [
                    {
                        "instrument_id": "WATCH.CROSS.SPX",
                        "asset_class": "index",
                        "ticker": "SPX",
                        "provider": "yahoo",
                        "metadata": {"registry_commit": "b" * 40},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return manifest_path


def test_lookup_failure_reuses_existing_manifest_when_allowed(monkeypatch, tmp_path: Path) -> None:
    manifest_path = _existing_manifest(tmp_path)
    receipt_path = tmp_path / "receipt.json"

    def failing_fetch(_ref):
        raise RuntimeError("registry ref lookup failed: HTTP Error 403: rate limit exceeded")

    monkeypatch.setattr(sync, "fetch_registry", failing_fetch)
    monkeypatch.setattr(sync, "validate_watchlist_manifest", lambda _m: None)
    before = manifest_path.read_bytes()

    code = sync.main(
        [
            "--ref", "main",
            "--snapshot-output", str(tmp_path / "snapshot.json"),
            "--manifest-output", str(manifest_path),
            "--receipt", str(receipt_path),
            "--reuse-manifest-on-lookup-failure",
        ]
    )

    assert code == 0
    assert manifest_path.read_bytes() == before
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["status"] == "lookup_failed_manifest_reused"
    assert receipt["changed"] is False
    assert receipt["registry_sha"] == "b" * 40
    assert "rate limit" in receipt["lookup_error"]


def test_lookup_failure_still_blocks_without_flag(monkeypatch, tmp_path: Path) -> None:
    manifest_path = _existing_manifest(tmp_path)
    receipt_path = tmp_path / "receipt.json"

    def failing_fetch(_ref):
        raise RuntimeError("registry ref lookup failed: HTTP Error 403: rate limit exceeded")

    monkeypatch.setattr(sync, "fetch_registry", failing_fetch)

    code = sync.main(
        [
            "--ref", "main",
            "--snapshot-output", str(tmp_path / "snapshot.json"),
            "--manifest-output", str(manifest_path),
            "--receipt", str(receipt_path),
        ]
    )

    assert code == 2
    assert json.loads(receipt_path.read_text(encoding="utf-8"))["status"] == "blocked"


def test_lookup_failure_blocks_when_no_manifest_exists(monkeypatch, tmp_path: Path) -> None:
    receipt_path = tmp_path / "receipt.json"

    def failing_fetch(_ref):
        raise RuntimeError("registry ref lookup failed: HTTP Error 403: rate limit exceeded")

    monkeypatch.setattr(sync, "fetch_registry", failing_fetch)

    code = sync.main(
        [
            "--ref", "main",
            "--snapshot-output", str(tmp_path / "snapshot.json"),
            "--manifest-output", str(tmp_path / "missing-manifest.json"),
            "--receipt", str(receipt_path),
            "--reuse-manifest-on-lookup-failure",
        ]
    )

    assert code == 2
    assert json.loads(receipt_path.read_text(encoding="utf-8"))["status"] == "blocked"
