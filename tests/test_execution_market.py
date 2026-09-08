from datetime import datetime, timedelta, timezone
import json

import httpx

from kline.execution_market.worker import (
    ExecutionMarketStore,
    INSTRUMENTS,
    ProviderUnavailable,
    fetch_binance,
    is_completed_bar,
    percentile95,
    run_once,
)


def test_identity_mapping_is_exact_and_separated_by_environment():
    assert INSTRUMENTS["XAUUSDT.BINANCE"] == {
        "provider": "binance_usdm_futures", "venue": "binance", "environment": "production", "symbol": "XAUUSDT"
    }
    assert INSTRUMENTS["BTC-USD-PERP.HYPERLIQUID"]["environment"] == "testnet"
    assert INSTRUMENTS["BTC-USD-PERP.HYPERLIQUID"]["venue"] == "hyperliquid"


def test_forming_bar_is_excluded_and_p95_is_deterministic():
    now = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)
    assert is_completed_bar(now - timedelta(seconds=1), now)
    assert not is_completed_bar(now + timedelta(seconds=1), now)
    assert percentile95([10, 20, 30, 40]) == 40


def test_store_upsert_is_idempotent_and_close_time_is_keyed(tmp_path):
    store = ExecutionMarketStore(tmp_path / "execution.db")
    from kline.execution_market.worker import ExecutionBar
    bar = ExecutionBar("XAUUSDT.BINANCE", "2026-09-08T11:58:00Z", "2026-09-08T11:58:59.999000Z", 1, 2, 0.5, 1.5, 3, "binance_usdm_futures", "binance", "production", "2026-09-08T12:00:00Z")
    store.write([bar], run_id="r1", fetched_at=datetime(2026, 9, 8, 12, tzinfo=timezone.utc))
    store.write([bar], run_id="r2", fetched_at=datetime(2026, 9, 8, 12, tzinfo=timezone.utc))
    assert store.db.execute("select count(*) from execution_market_candles").fetchone()[0] == 1
    store.close()


def test_stale_after_ninety_seconds_emits_event(tmp_path):
    store = ExecutionMarketStore(tmp_path / "execution.db")
    first = datetime(2026, 9, 8, 12, tzinfo=timezone.utc)
    assert store.mark_failure("XAUUSDT.BINANCE", first, "timeout") is False
    assert store.mark_failure("XAUUSDT.BINANCE", first + timedelta(seconds=90), "timeout") is True
    assert store.db.execute("select event from execution_market_events").fetchone()[0] == "execution_market_unavailable"
    store.close()


def test_lock_is_independent_and_reports_contention(tmp_path):
    from kline.execution_market.worker import ExecutionMarketLock
    path = tmp_path / "execution.lock"
    first = ExecutionMarketLock(path)
    first.__enter__()
    try:
        try:
            ExecutionMarketLock(path).__enter__()
        except RuntimeError as exc:
            assert "already held" in str(exc)
        else:
            raise AssertionError("second execution worker must not acquire the lock")
    finally:
        first.__exit__(None, None, None)


def test_binance_public_payload_is_normalized_without_forming_bar():
    now = datetime(2026, 9, 8, 12, 0, 30, tzinfo=timezone.utc)
    payload = [[1725796740000, "1", "2", "0.5", "1.5", "3", 1725796799999, "4"]]
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)))
    bars = fetch_binance("XAUUSDT.BINANCE", now, client)
    assert bars[0].provider == "binance_usdm_futures"
    assert bars[0].venue == "binance"
    assert bars[0].age_seconds > 0
    client.close()


def test_run_once_writes_receipt_and_does_not_need_default_paths(tmp_path, monkeypatch):
    from kline.execution_market import worker
    monkeypatch.setattr(worker, "fetch_binance", lambda *_: [])
    monkeypatch.setattr(worker, "fetch_hyperliquid", lambda *_: [])
    receipt_path = tmp_path / "receipt.json"
    receipt = run_once(tmp_path / "execution.db", receipt_path, clock=lambda: datetime(2026, 9, 8, 12, tzinfo=timezone.utc))
    assert receipt["status"] == "ok"
    assert json.loads(receipt_path.read_text())["schema_version"] == "execution-market-lag-v1"
