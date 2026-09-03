from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from kline.app import create_app
from kline.combined_health import build_combined_health_matrix
from kline.store import KlineStore


def test_combined_health_matrix_merges_screening_and_watchlist_stores(tmp_path) -> None:
    snapshot = build_combined_health_matrix(
        screening_store=KlineStore(str(tmp_path / "screening.db")),
        market_store=KlineStore(str(tmp_path / "market.db")),
        now=datetime(2026, 9, 3, 12, tzinfo=timezone.utc),
    )

    assert snapshot["scope"] == {
        "name": "screening_watchlist",
        "instrument_count": 274,
        "universes": {"a_share": 100, "us_stock": 100, "cross_market": 16, "watchlist": 58},
    }
    assert len(snapshot["cells"]) == 274 * 5
    watchlist_cells = [cell for cell in snapshot["cells"] if cell["instrument_id"].startswith("WATCH.")]
    assert len(watchlist_cells) == 58 * 5
    assert {cell["universe"] for cell in watchlist_cells} == {"watchlist"}
    assert {cell["dataset"] for cell in watchlist_cells} == {"watchlist"}
    spx = next(cell for cell in watchlist_cells if cell["instrument_id"] == "WATCH.CROSS.SPX" and cell["timeframe"] == "1d")
    assert spx["provider_symbol"] == "SPY"
    assert spx["source_id"] == "yahoo_finance_etf"
    assert spx["metadata"]["identity_role"] == "proxy"
    assert snapshot["manifest_versions"] == {
        "screening": "mvp_universe_v1",
        "watchlist": "watchlist_universe_v1",
    }
    for timeframe, counts in snapshot["coverage"].items():
        assert counts["applicable"] + counts["not_applicable"] == 274


def test_combined_health_api_reads_market_database_without_replacing_screening(
    monkeypatch, tmp_path
) -> None:
    screening_db = tmp_path / "screening-api.db"
    market_db = tmp_path / "market-api.db"
    monkeypatch.setenv("KLINE_DB_PATH", str(screening_db))
    monkeypatch.setenv("KLINE_MARKET_DB_PATH", str(market_db))
    monkeypatch.setenv("KLINE_LOAD_ENTRYPOINT_ADAPTERS", "false")

    with TestClient(create_app()) as client:
        response = client.get("/api/health/combined-matrix")

    assert response.status_code == 200
    payload = response.json()
    assert payload["scope"]["name"] == "screening_watchlist"
    assert payload["manifest_versions"]["watchlist"] == "watchlist_universe_v1"
    assert len(payload["cells"]) == 274 * 5
