from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from kline.app import create_app
from kline.combined_health import build_combined_health_matrix
from kline.storage import (
    CandleSeriesKey,
    MvpCandle,
    MvpRunWrite,
    QualityReceiptWrite,
    SourceObservationWrite,
    WatermarkWrite,
)
from kline.store import KlineStore


def _seed_watchlist_spx(store: KlineStore) -> None:
    key = CandleSeriesKey(
        instrument_id="WATCH.CROSS.SPX",
        display_symbol="SPX",
        provider_symbol="SPY",
        source_id="yahoo_finance_etf",
        asset_class="etf",
        timeframe="1d",
        adjustment_basis="raw_unadjusted",
        manifest_version="watchlist_universe_v1",
    )
    candle = MvpCandle(
        key=key,
        timestamp="2026-09-02T00:00:00+00:00",
        open=100,
        high=102,
        low=99,
        close=101,
        volume=1000,
    )
    store.commit_mvp_run(
        MvpRunWrite(
            run_id="watchlist-spx-test",
            manifest_version=key.manifest_version,
            manifest_hash="a" * 64,
            started_at="2026-09-03T07:00:00+00:00",
            completed_at="2026-09-03T07:01:00+00:00",
            window_start=None,
            window_end="2026-09-03T07:00:00+00:00",
            policy={"runner": "fixture"},
            candles=(candle,),
            source_observations=(
                SourceObservationWrite(
                    run_id="watchlist-spx-test",
                    key=key,
                    success=True,
                    request_start=None,
                    request_end="2026-09-03T07:00:00+00:00",
                    response_hash="b" * 64,
                    candle_count=1,
                    latest_timestamp=candle.timestamp,
                    observed_at="2026-09-03T07:01:00+00:00",
                ),
            ),
            quality_receipts=(
                QualityReceiptWrite(run_id="watchlist-spx-test", key=key, status="pass"),
            ),
            watermarks=(
                WatermarkWrite(
                    key=key,
                    last_closed_timestamp=candle.timestamp,
                    cursor=None,
                    run_id="watchlist-spx-test",
                ),
            ),
        )
    )


def test_combined_health_matrix_merges_screening_and_watchlist_stores(tmp_path) -> None:
    market_store = KlineStore(str(tmp_path / "market.db"))
    _seed_watchlist_spx(market_store)
    snapshot = build_combined_health_matrix(
        screening_store=KlineStore(str(tmp_path / "screening.db")),
        market_store=market_store,
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
    assert spx["latest_closed_timestamp"] == "2026-09-02T00:00:00+00:00"
    assert spx["technical_status"] == "ready"
    assert spx["status"] == "partial"
    assert spx["quality"]["status"] == "pass"
    assert spx["watermark"]["run_id"] == "watchlist-spx-test"
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
    KlineStore(str(screening_db))
    KlineStore(str(market_db))
    monkeypatch.setenv("KLINE_DB_PATH", str(screening_db))
    monkeypatch.setenv("KLINE_MARKET_DB_PATH", str(market_db))
    monkeypatch.setenv("KLINE_LOAD_ENTRYPOINT_ADAPTERS", "false")
    monkeypatch.setenv("KLINE_READ_ONLY", "true")

    with TestClient(create_app()) as client:
        response = client.get("/api/health/combined-matrix")

    assert response.status_code == 200
    payload = response.json()
    assert payload["scope"]["name"] == "screening_watchlist"
    assert payload["manifest_versions"]["watchlist"] == "watchlist_universe_v1"
    assert len(payload["cells"]) == 274 * 5
