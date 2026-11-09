"""#194: /api/health must not block other requests, and must not re-run PRAGMA integrity_check each call."""
from __future__ import annotations

import asyncio
import threading
import time

from kline import api


class SlowStore:
    """A store whose integrity check is slow, like the 485 MB production database."""

    def __init__(self, integrity_seconds: float = 0.6) -> None:
        self.integrity_seconds = integrity_seconds
        self.integrity_calls = 0
        self.storage_calls = 0
        self.threads: set[str] = set()

    def storage_integrity(self) -> str:
        self.integrity_calls += 1
        self.threads.add(threading.current_thread().name)
        time.sleep(self.integrity_seconds)
        return "ok"

    def storage_health(self, *, integrity: str | None = None) -> dict:
        self.storage_calls += 1
        return {"status": "ok" if integrity == "ok" else "error", "integrity": integrity, "candle_rows": 2, "source_count": 1}

    def source_coverage(self) -> list[dict]:
        return [{"source_id": "unknown_source", "asset_class": "crypto", "ticker": "BTC", "timeframe": "1m", "count": 2}]

    def latest_source_observations(self) -> list[dict]:
        return []


def _run_with(store: SlowStore, coroutine_factory):
    original = api.get_store
    api.get_store = lambda: store
    api._HEALTH_CACHES.clear()
    try:
        return asyncio.run(coroutine_factory())
    finally:
        api.get_store = original
        api._HEALTH_CACHES.clear()


def test_the_event_loop_keeps_serving_while_health_checks_integrity() -> None:
    store = SlowStore()

    async def scenario():
        ticks: list[float] = []

        async def other_request() -> None:  # stands in for a cached GOLD read
            started = time.monotonic()
            for _ in range(10):
                await asyncio.sleep(0.02)
                ticks.append(time.monotonic() - started)

        started = time.monotonic()
        payload, _ = await asyncio.gather(api.health(), other_request())
        return payload, ticks, time.monotonic() - started

    payload, ticks, elapsed = _run_with(store, scenario)
    assert payload["storage"]["integrity"] == "ok" and payload["storage"]["status"] == "ok"
    assert ticks[-1] < 0.5  # the other request finished while integrity_check (0.6 s) was still running
    assert elapsed >= 0.6
    assert "MainThread" not in store.threads


def test_concurrent_health_calls_share_one_integrity_check_and_reuse_it() -> None:
    store = SlowStore(integrity_seconds=0.2)

    async def scenario():
        first = await asyncio.gather(*(api.health() for _ in range(5)))
        second = await api.health()
        return first, second

    first, second = _run_with(store, scenario)
    assert store.integrity_calls == 1 and store.storage_calls == 1
    assert all(item["storage"]["integrity"] == "ok" for item in first)
    assert second["storage_age_seconds"] >= 0 and second["storage"]["integrity_age_seconds"] >= 0.0
    assert second["storage_coverage"][0]["instrument_id"] == "BTC"


def test_storage_facts_refresh_after_their_ttl_but_integrity_waits_longer(monkeypatch) -> None:
    store = SlowStore(integrity_seconds=0.0)
    monkeypatch.setattr(api, "HEALTH_STORAGE_TTL_SECONDS", 0.05)
    monkeypatch.setattr(api, "HEALTH_INTEGRITY_TTL_SECONDS", 60.0)

    async def scenario():
        await api.health()
        await asyncio.sleep(0.1)
        await api.health()

    _run_with(store, scenario)
    assert store.storage_calls == 2  # coverage is re-read
    assert store.integrity_calls == 1  # the full check is not
