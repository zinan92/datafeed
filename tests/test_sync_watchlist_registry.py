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
