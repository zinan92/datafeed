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
    age_stats,
    next_aligned_run_delay,
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
    assert age_stats([10, 20, 30, 40]) == {"p50": 20.0, "p95": 40.0, "max": 40.0, "samples": 4}


def test_forming_price_is_stored_separately_from_completed_candles(tmp_path):
    from kline.execution_market.worker import ExecutionBar
    store = ExecutionMarketStore(tmp_path / "execution.db")
    completed = ExecutionBar("XAUUSDT.BINANCE", "2026-09-08T11:59:00Z", "2026-09-08T11:59:59Z", 1, 2, 0.5, 100, 3, "binance_usdm_futures", "binance", "production", "2026-09-08T12:00:00Z")
    forming = ExecutionBar("XAUUSDT.BINANCE", "2026-09-08T12:00:00Z", "2026-09-08T12:00:59.999Z", 1, 2, 0.5, 101, 3, "binance_usdm_futures", "binance", "production", "2026-09-08T12:00:10Z")
    store.write([completed], run_id="r1", fetched_at=datetime(2026, 9, 8, 12, tzinfo=timezone.utc))
    store.write_forming([forming], sampled_at=datetime(2026, 9, 8, 12, 0, 10, tzinfo=timezone.utc))
    assert store.latest("XAUUSDT.BINANCE")[0].close == 100
    assert store.db.execute("select price from execution_market_forming_quotes").fetchone()[0] == 101
    store.close()


def test_poll_slots_are_one_second_after_minute_and_interval_aligned():
    now = datetime(2026, 9, 8, 12, 0, 59, tzinfo=timezone.utc)
    assert next_aligned_run_delay(now, 5) == 2
    now = datetime(2026, 9, 8, 12, 1, 1, tzinfo=timezone.utc)
    assert next_aligned_run_delay(now, 5) == 5


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


def test_read_age_histogram_is_per_instrument_and_accumulates(tmp_path):
    from kline.execution_market.worker import ExecutionBar
    store = ExecutionMarketStore(tmp_path / "execution.db")
    bar = ExecutionBar("XAUUSDT.BINANCE", "2026-09-08T11:59:00Z", "2026-09-08T11:59:59Z", 1, 2, 0.5, 1.5, 3, "binance_usdm_futures", "binance", "production", "2026-09-08T12:00:00Z")
    store.write([bar], run_id="r1", fetched_at=datetime(2026, 9, 8, 12, 0, 1, tzinfo=timezone.utc))
    receipt = store.write([], run_id="r2", fetched_at=datetime(2026, 9, 8, 12, 0, 6, tzinfo=timezone.utc))
    assert receipt["read_age_seconds"]["XAUUSDT.BINANCE"]["samples"] == 2
    assert receipt["read_age_p50_seconds"]["XAUUSDT.BINANCE"] == 2.0
    assert receipt["read_age_max_seconds"]["XAUUSDT.BINANCE"] == 7.0
    store.close()
