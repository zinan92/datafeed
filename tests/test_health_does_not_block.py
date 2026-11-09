"""#194/#196/#198/#200: /api/health never blocks other requests and never makes a caller wait for the
database. PRAGMA integrity_check (~12 s on production) and the storage facts (~9 s) both run in
background tasks; until a first result exists the answer is "pending", never "ok"."""
from __future__ import annotations

import asyncio
import threading
import time

from kline import api


class SlowStore:
    """A store whose integrity check and storage facts are slow, like the 485 MB production database."""

    def __init__(self, integrity_seconds: float = 0.0, storage_seconds: float = 0.0) -> None:
        self.integrity_seconds = integrity_seconds
        self.storage_seconds = storage_seconds
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
        self.threads.add(threading.current_thread().name)
        time.sleep(self.storage_seconds)
        status = integrity if integrity in ("ok", "pending") else "error"
        return {"status": status, "integrity": integrity, "candle_rows": 2, "source_count": 1}

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


async def _settle(store: SlowStore) -> None:
    """Wait until both background refreshes have finished."""
    for _ in range(300):
        cache = api._HEALTH_CACHES.get(id(store))
        if cache and all(task is None or task.done() for task in (cache.integrity_refresh, cache.storage_refresh)):
            return
        await asyncio.sleep(0.01)
    raise AssertionError("background refresh never finished")


def test_the_first_call_after_a_start_answers_pending_at_once_then_ok() -> None:
    """#198/#200: the first call used to wait 26.2 s on production. It now reports pending, never ok."""
    store = SlowStore(integrity_seconds=0.6, storage_seconds=0.4)

    async def scenario():
        ticks: list[float] = []

        async def other_request() -> None:  # stands in for a cached GOLD read
            started = time.monotonic()
            for _ in range(10):
                await asyncio.sleep(0.02)
                ticks.append(time.monotonic() - started)

        started = time.monotonic()
        first, _ = await asyncio.gather(api.health(), other_request())
        quick = time.monotonic() - started
        await _settle(store)
        after = await api.health()
        return first, quick, ticks, after

    first, quick, ticks, after = _run_with(store, scenario)
    assert quick < 0.35  # waited for neither the 0.6 s check nor the 0.4 s facts
    assert first["storage"]["status"] == "pending" and first["storage"]["integrity"] == "pending"
    assert first["storage"]["integrity_age_seconds"] is None and first["storage_age_seconds"] is None
    assert first["storage_coverage"] == [] and first["storage"]["candle_rows"] is None
    assert ticks[-1] < 0.35
    assert after["storage"]["status"] == "ok" and after["storage"]["integrity"] == "ok"
    assert after["storage"]["candle_rows"] == 2 and after["storage_coverage"][0]["instrument_id"] == "BTC"
    assert after["storage_age_seconds"] >= 0 and after["storage"]["integrity_age_seconds"] >= 0
    assert "MainThread" not in store.threads


def test_facts_without_a_finished_integrity_check_are_pending_not_ok() -> None:
    store = SlowStore(integrity_seconds=0.5, storage_seconds=0.0)

    async def scenario():
        await api.health()
        await asyncio.sleep(0.1)  # facts are in; integrity is still running
        middle = await api.health()
        await _settle(store)
        return middle

    middle = _run_with(store, scenario)
    assert middle["storage"]["candle_rows"] == 2
    assert middle["storage"]["status"] == "pending" and middle["storage"]["integrity"] == "pending"


def test_concurrent_calls_share_one_refresh_of_each() -> None:
    store = SlowStore(integrity_seconds=0.2, storage_seconds=0.2)

    async def scenario():
        first = await asyncio.gather(*(api.health() for _ in range(5)))
        await _settle(store)
        second = await asyncio.gather(*(api.health() for _ in range(3)))
        return first, second

    first, second = _run_with(store, scenario)
    assert store.integrity_calls == 1 and store.storage_calls == 1
    assert all(item["storage"]["status"] == "pending" for item in first)
    assert all(item["storage"]["status"] == "ok" for item in second)


def test_expired_results_are_served_while_fresh_ones_run_in_the_background(monkeypatch) -> None:
    """#196/#200: on production a call after either TTL waited 8-11 s; now nobody waits."""
    store = SlowStore()
    monkeypatch.setattr(api, "HEALTH_STORAGE_TTL_SECONDS", 0.2)
    monkeypatch.setattr(api, "HEALTH_INTEGRITY_TTL_SECONDS", 0.3)

    async def scenario():
        await api.health()
        await _settle(store)
        store.integrity_seconds = store.storage_seconds = 0.5
        await asyncio.sleep(0.35)  # both results are now older than their TTL
        started = time.monotonic()
        calls = await asyncio.gather(*(api.health() for _ in range(3)))
        quick = time.monotonic() - started
        await _settle(store)
        after = await api.health()
        return calls, quick, after

    calls, quick, after = _run_with(store, scenario)
    assert quick < 0.3  # nobody waited for the 0.5 s refreshes
    assert all(item["storage"]["status"] == "ok" and item["storage_age_seconds"] >= 0.3 for item in calls)
    assert all(item["storage"]["integrity_age_seconds"] >= 0.3 for item in calls)
    assert store.integrity_calls == 2 and store.storage_calls == 2  # the first, then exactly one refresh each
    assert after["storage_age_seconds"] < 0.3 and after["storage"]["integrity_age_seconds"] < 0.3


def test_storage_facts_refresh_more_often_than_integrity(monkeypatch) -> None:
    store = SlowStore()
    monkeypatch.setattr(api, "HEALTH_STORAGE_TTL_SECONDS", 0.05)
    monkeypatch.setattr(api, "HEALTH_INTEGRITY_TTL_SECONDS", 60.0)

    async def scenario():
        await api.health()
        await _settle(store)
        await asyncio.sleep(0.1)
        await api.health()
        await _settle(store)

    _run_with(store, scenario)
    assert store.storage_calls == 2  # coverage is re-read
    assert store.integrity_calls == 1  # the full check is not


def test_a_failing_integrity_check_reports_an_error_not_pending_forever() -> None:
    store = SlowStore()

    def broken() -> str:
        raise OSError("disk I/O error")

    store.storage_integrity = broken

    async def scenario():
        await api.health()
        await _settle(store)
        return await api.health()

    after = _run_with(store, scenario)
    assert after["storage"]["status"] == "error"
    assert after["storage"]["integrity"] == "unavailable: OSError"


def test_failing_storage_facts_report_an_error_until_a_refresh_succeeds(monkeypatch) -> None:
    store = SlowStore()
    monkeypatch.setattr(api, "HEALTH_STORAGE_TTL_SECONDS", 0.0)
    original = store.source_coverage

    def broken() -> list[dict]:
        raise OSError("database is locked")

    store.source_coverage = broken

    async def scenario():
        await api.health()
        await _settle(store)
        failed = await api.health()
        await _settle(store)
        store.source_coverage = original
        await api.health()  # starts a refresh that succeeds
        await _settle(store)
        return failed, await api.health()

    failed, recovered = _run_with(store, scenario)
    assert failed["storage"]["status"] == "error" and failed["storage"]["error"] == "unavailable: OSError"
    assert failed["storage_coverage"] == []
    assert recovered["storage"]["status"] == "ok" and recovered["storage_coverage"][0]["instrument_id"] == "BTC"
