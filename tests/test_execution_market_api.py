from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from kline.app import create_app
from kline.execution_market.worker import ExecutionBar, ExecutionMarketStore


def _bar(close_time: datetime, close: float = 2400.0) -> ExecutionBar:
    close_text = close_time.isoformat().replace("+00:00", "Z")
    open_text = (close_time - timedelta(seconds=59)).isoformat().replace("+00:00", "Z")
    return ExecutionBar(
        "XAUUSDT.BINANCE", open_text, close_text, close, close + 1, close - 1,
        close, 10, "binance_usdm_futures", "binance", "production", close_text,
    )


def _client(tmp_path, monkeypatch, bars: list[ExecutionBar]) -> TestClient:
    path = tmp_path / "execution-market.db"
    store = ExecutionMarketStore(path)
    now = datetime.now(timezone.utc)
    completed = [bar for bar in bars if datetime.fromisoformat(bar.close_time.replace("Z", "+00:00")) <= now]
    forming = [bar for bar in bars if datetime.fromisoformat(bar.close_time.replace("Z", "+00:00")) > now]
    store.write(completed, run_id="test", fetched_at=now)
    store.write_forming(forming[-1:], sampled_at=now)
    store.close()
    monkeypatch.setenv("KLINE_EXECUTION_MARKET_DB", str(path))
    return TestClient(create_app())


def test_fresh_response_maps_execution_reader_fields_and_separates_forming(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    with _client(tmp_path, monkeypatch, [_bar(now - timedelta(seconds=30)), _bar(now + timedelta(seconds=30), 2500)]) as client:
        response = client.get("/api/execution-market/binance/XAUUSDT", params={"limit": 240})

    assert response.status_code == 200
    payload = response.json()
    assert payload["schema_version"] == "execution-market-v2"
    assert payload["price"] == 2400.0
    assert payload["last_price"] == 2500.0
    assert payload["price_time"] is not None
    assert payload["last_age_seconds"] < 5
    assert payload["trusted"] is True
    assert payload["fresh"] is True
    assert payload["source"] == "binance_usdm_futures"
    assert payload["observed_at"] == payload["bars"][-1]["close_time"]
    assert len(payload["bars"]) == 1


def test_stale_response_is_present_but_not_fresh(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch, [_bar(datetime.now(timezone.utc) - timedelta(seconds=120))]) as client:
        payload = client.get("/api/execution-market/binance/XAUUSDT").json()

    assert payload["price"] == 2400.0
    assert payload["last_price"] is None
    assert payload["trusted"] is True
    assert payload["fresh"] is False
    assert payload["reason"] == "stale"
    assert payload["age_seconds"] > 90


def test_missing_response_is_explicit(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch, []) as client:
        payload = client.get("/api/execution-market/binance/XAUUSDT").json()

    assert payload == {
        **payload,
        "schema_version": "execution-market-v2",
        "price": None,
        "trusted": False,
        "fresh": False,
        "observed_at": None,
        "age_seconds": None,
        "last_price": None,
        "price_time": None,
        "last_age_seconds": None,
        "reason": "missing",
        "bars": [],
    }
