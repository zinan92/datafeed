from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from kline.app import create_app
from kline.combined_health import build_combined_health_matrix
from kline.health_matrix import MATRIX_SCOPE_WATCHLIST, build_mvp_health_matrix
from kline.storage import (
    CandleSeriesKey,
    MvpCandle,
    MvpRunWrite,
    QualityReceiptWrite,
    SourceObservationWrite,
    WatermarkWrite,
)
from kline.store import KlineStore
from kline.watchlist_manifest import load_watchlist_manifest


WATCHLIST_MANIFEST_PATH = Path(__file__).parents[1] / "configs" / "watchlist_manifest.json"


def _seed_watchlist_proxy(
    store: KlineStore,
    *,
    instrument_id: str = "WATCH.CROSS.SPX",
    display_symbol: str = "SPX",
    provider_symbol: str = "SPY",
    quality_status: str = "pass",
) -> None:
    run_id = f"watchlist-{display_symbol.lower()}-test"
    key = CandleSeriesKey(
        instrument_id=instrument_id,
        display_symbol=display_symbol,
        provider_symbol=provider_symbol,
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
            run_id=run_id,
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
                    run_id=run_id,
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
                QualityReceiptWrite(run_id=run_id, key=key, status=quality_status),
            ),
            watermarks=(
                WatermarkWrite(
                    key=key,
                    last_closed_timestamp=candle.timestamp,
                    cursor=None,
                    run_id=run_id,
                ),
            ),
        )
    )


def _seed_watchlist_daily(
    store: KlineStore,
    *,
    instrument_id: str,
    display_symbol: str,
    provider_symbol: str,
    source_id: str,
    asset_class: str,
    adjustment_basis: str,
    timestamp: str,
) -> None:
    run_id = f"watchlist-{display_symbol.lower()}-freshness-test"
    key = CandleSeriesKey(
        instrument_id=instrument_id,
        display_symbol=display_symbol,
        provider_symbol=provider_symbol,
        source_id=source_id,
        asset_class=asset_class,
        timeframe="1d",
        adjustment_basis=adjustment_basis,
        manifest_version="watchlist_universe_v1",
    )
    candle = MvpCandle(
        key=key,
        timestamp=timestamp,
        open=100,
        high=102,
        low=99,
        close=101,
        volume=None if asset_class == "index" else 1000,
        volume_semantics="not_applicable" if asset_class == "index" else "traded",
    )
    store.commit_mvp_run(
        MvpRunWrite(
            run_id=run_id,
            manifest_version=key.manifest_version,
            manifest_hash="c" * 64,
            started_at="2026-09-04T00:10:00+00:00",
            completed_at="2026-09-04T00:11:00+00:00",
            window_start=None,
            window_end="2026-09-04T00:10:00+00:00",
            policy={"runner": "fixture"},
            candles=(candle,),
            source_observations=(
                SourceObservationWrite(
                    run_id=run_id,
                    key=key,
                    success=True,
                    request_start=None,
                    request_end="2026-09-04T00:10:00+00:00",
                    response_hash="d" * 64,
                    candle_count=1,
                    latest_timestamp=candle.timestamp,
                    observed_at="2026-09-04T00:11:00+00:00",
                ),
            ),
            quality_receipts=(QualityReceiptWrite(run_id=run_id, key=key, status="pass"),),
            watermarks=(
                WatermarkWrite(
                    key=key,
                    last_closed_timestamp=candle.timestamp,
                    cursor=None,
                    run_id=run_id,
                ),
            ),
        )
    )


def test_combined_health_matrix_merges_screening_and_watchlist_stores(tmp_path) -> None:
    market_store = KlineStore(str(tmp_path / "market.db"))
    _seed_watchlist_proxy(market_store)
    snapshot = build_combined_health_matrix(
        screening_store=KlineStore(str(tmp_path / "screening.db")),
        market_store=market_store,
        now=datetime(2026, 9, 3, 12, tzinfo=timezone.utc),
    )

    assert snapshot["scope"] == {
        "name": "screening_watchlist",
        "instrument_count": 323,
        "universes": {"a_share": 100, "us_stock": 100, "cross_market": 16, "watchlist": 107},
    }
    assert len(snapshot["cells"]) == 323 * 5
    watchlist_cells = [cell for cell in snapshot["cells"] if cell["instrument_id"].startswith("WATCH.")]
    assert len(watchlist_cells) == 107 * 5
    assert {cell["universe"] for cell in watchlist_cells} == {"watchlist"}
    assert {cell["dataset"] for cell in watchlist_cells} == {"watchlist"}
    spx = next(cell for cell in watchlist_cells if cell["instrument_id"] == "WATCH.CROSS.SPX" and cell["timeframe"] == "1d")
    assert spx["provider_symbol"] == "SPY"
    assert spx["source_id"] == "yahoo_finance_etf"
    assert spx["metadata"]["identity_role"] == "proxy"
    assert spx["latest_closed_timestamp"] == "2026-09-02T00:00:00+00:00"
    assert spx["technical_status"] == "ready"
    assert spx["status"] == "ready_unverified"
    assert snapshot["coverage"]["1d"]["ready_unverified"] == 1
    assert spx["quality"]["status"] == "pass"
    assert spx["watermark"]["run_id"] == "watchlist-spx-test"
    hk_cells = [
        cell
        for cell in watchlist_cells
        if cell["instrument_id"].startswith("WATCH.HK.")
    ]
    kr_cells = [
        cell
        for cell in watchlist_cells
        if cell["instrument_id"].startswith("WATCH.KR.")
    ]
    assert len(hk_cells) == 4 * 5
    assert len(kr_cells) == 1 * 5
    assert {cell["metadata"]["registry_market"] for cell in hk_cells} == {"HK"}
    assert {cell["metadata"]["registry_market"] for cell in kr_cells} == {"KR"}
    assert all(len(cell["metadata"]["registry_commit"]) == 40 for cell in hk_cells + kr_cells)
    assert all(len(cell["metadata"]["registry_source_sha256"]) == 64 for cell in hk_cells + kr_cells)
    assert snapshot["manifest_versions"] == {
        "screening": "mvp_universe_v1",
        "watchlist": "watchlist_universe_v1",
    }
    for timeframe, counts in snapshot["coverage"].items():
        assert counts["applicable"] + counts["not_applicable"] == 323


def test_ready_unverified_is_overall_data_healthy_without_claiming_entitlement(
    tmp_path,
) -> None:
    store = KlineStore(str(tmp_path / "watchlist-ready-unverified.db"))
    _seed_watchlist_proxy(store)
    manifest = load_watchlist_manifest(WATCHLIST_MANIFEST_PATH)

    snapshot = build_mvp_health_matrix(
        manifest,
        store,
        now=datetime(2026, 9, 3, 12, tzinfo=timezone.utc),
        scope=MATRIX_SCOPE_WATCHLIST,
        instrument_ids=("WATCH.CROSS.SPX",),
        free_source_ids={"yahoo_finance_etf"},
    )

    daily = next(cell for cell in snapshot["cells"] if cell["timeframe"] == "1d")
    assert daily["status"] == "ready_unverified"
    assert daily["status_reason"] == "entitlement_unverified"
    assert daily["technical_status"] == "ready"
    assert daily["entitlement"]["status"] == "unverified"
    assert snapshot["status"] == "ready"
    assert snapshot["coverage"]["1d"]["ready_unverified"] == 1
    assert snapshot["coverage"]["1d"]["partial"] == 0
    assert snapshot["coverage"]["1d"]["ready"] == 0


def test_health_uses_declared_china_daily_timestamp_conventions(tmp_path) -> None:
    store = KlineStore(str(tmp_path / "watchlist-china-freshness.db"))
    _seed_watchlist_daily(
        store,
        instrument_id="WATCH.CN.A.600900",
        display_symbol="600900",
        provider_symbol="600900",
        source_id="tencent_stock_free",
        asset_class="a_share",
        adjustment_basis="qfq",
        timestamp="2026-09-02T16:00:00+00:00",
    )
    _seed_watchlist_daily(
        store,
        instrument_id="WATCH.CROSS.SHCOMP",
        display_symbol="sh000001",
        provider_symbol="sh000001",
        source_id="tencent_kline",
        asset_class="index",
        adjustment_basis="raw_index_level",
        timestamp="2026-09-03T00:00:00+00:00",
    )
    _seed_watchlist_daily(
        store,
        instrument_id="WATCH.CROSS.STAR50",
        display_symbol="sh000688",
        provider_symbol="sh000688",
        source_id="tencent_kline",
        asset_class="index",
        adjustment_basis="raw_index_level",
        timestamp="2026-09-02T00:00:00+00:00",
    )
    manifest = load_watchlist_manifest(WATCHLIST_MANIFEST_PATH)

    snapshot = build_mvp_health_matrix(
        manifest,
        store,
        now=datetime(2026, 9, 4, 0, 15, tzinfo=timezone.utc),
        scope=MATRIX_SCOPE_WATCHLIST,
        instrument_ids=(
            "WATCH.CN.A.600900",
            "WATCH.CROSS.SHCOMP",
            "WATCH.CROSS.STAR50",
        ),
        free_source_ids={"tencent_stock_free", "tencent_kline"},
    )

    daily = {
        cell["instrument_id"]: cell
        for cell in snapshot["cells"]
        if cell["timeframe"] == "1d"
    }
    assert daily["WATCH.CN.A.600900"]["status"] == "ready_unverified"
    assert daily["WATCH.CROSS.SHCOMP"]["status"] == "ready_unverified"
    assert daily["WATCH.CROSS.STAR50"]["status"] == "stale"
    assert daily["WATCH.CROSS.STAR50"]["status_reason"] == "freshness_sla_exceeded"
    assert snapshot["coverage"]["1d"]["ready_unverified"] == 2
    assert snapshot["coverage"]["1d"]["stale"] == 1


def test_real_partial_still_degrades_a_mixed_unverified_snapshot(tmp_path) -> None:
    store = KlineStore(str(tmp_path / "watchlist-mixed.db"))
    _seed_watchlist_proxy(store)
    _seed_watchlist_proxy(
        store,
        instrument_id="WATCH.CROSS.NDX",
        display_symbol="NDX",
        provider_symbol="QQQ",
        quality_status="partial",
    )
    manifest = load_watchlist_manifest(WATCHLIST_MANIFEST_PATH)

    snapshot = build_mvp_health_matrix(
        manifest,
        store,
        now=datetime(2026, 9, 3, 12, tzinfo=timezone.utc),
        scope=MATRIX_SCOPE_WATCHLIST,
        instrument_ids=("WATCH.CROSS.SPX", "WATCH.CROSS.NDX"),
        free_source_ids={"yahoo_finance_etf"},
    )

    daily = {
        cell["display_symbol"]: cell
        for cell in snapshot["cells"]
        if cell["timeframe"] == "1d"
    }
    assert daily["SPX"]["status"] == "ready_unverified"
    assert daily["NDX"]["status"] == "partial"
    assert daily["NDX"]["status_reason"] == "quality_partial"
    assert snapshot["status"] == "partial"


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
    assert len(payload["cells"]) == 323 * 5
