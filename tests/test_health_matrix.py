from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from kline.app import create_app
from kline.health_matrix import MVP_DEMO_INSTRUMENT_IDS, build_mvp_health_matrix
from kline.mvp_manifest import load_manifest
from kline.storage import (
    CandleSeriesKey,
    MvpCandle,
    MvpRunWrite,
    QualityReceiptWrite,
    SourceObservationWrite,
    WatermarkWrite,
)
from kline.store import KlineStore


MANIFEST_PATH = Path(__file__).parents[1] / "configs" / "mvp_manifest.json"


def _btc_key(timeframe: str) -> CandleSeriesKey:
    return CandleSeriesKey(
        instrument_id="CRYPTO.PERP.BTC",
        display_symbol="BTC",
        provider_symbol="BTC",
        source_id="hyperliquid_perpetual_public",
        asset_class="crypto",
        timeframe=timeframe,
        adjustment_basis="raw_unadjusted",
        manifest_version="mvp_universe_v1",
    )


def _btc_candle(key: CandleSeriesKey) -> MvpCandle:
    return MvpCandle(
        key=key,
        timestamp="2026-08-31T00:00:00+00:00",
        open=100,
        high=102,
        low=99,
        close=101,
        volume=10,
    )


def test_matrix_returns_every_manifest_timeframe_cell_and_explicit_statuses(tmp_path: Path) -> None:
    manifest = load_manifest(MANIFEST_PATH)
    store = KlineStore(str(tmp_path / "matrix.db"))
    snapshot = build_mvp_health_matrix(
        manifest,
        store,
        now=datetime(2026, 9, 1, 12, tzinfo=timezone.utc),
    )

    assert len(snapshot["cells"]) == 6 * 5
    assert {cell["timeframe"] for cell in snapshot["cells"]} == {"15m", "1h", "4h", "1d", "1w"}
    assert {cell["instrument_id"] for cell in snapshot["cells"]} == set(MVP_DEMO_INSTRUMENT_IDS)
    aapl_1h = next(
        cell
        for cell in snapshot["cells"]
        if cell["display_symbol"] == "AAPL" and cell["timeframe"] == "1h"
    )
    assert aapl_1h["status"] == "blocked"
    assert aapl_1h["status_reason"] == "entitlement_blocked"
    assert aapl_1h["applicability"] == "applicable"
    assert aapl_1h["error"]["code"] == "entitlement_blocked"


def test_matrix_promotes_ready_cell_from_real_receipts(tmp_path: Path) -> None:
    manifest = load_manifest(MANIFEST_PATH)
    store = KlineStore(str(tmp_path / "matrix-ready.db"))
    key = _btc_key("1h")
    run_id = "matrix-run-1"
    candle = _btc_candle(key)
    store.commit_mvp_run(
        MvpRunWrite(
            run_id=run_id,
            manifest_version=manifest.version,
            manifest_hash="a" * 64,
            started_at="2026-08-31T00:00:00+00:00",
            window_start="2026-08-30T00:00:00+00:00",
            window_end="2026-09-01T00:00:00+00:00",
            policy={"overlap_bars": 2},
            candles=(candle,),
            source_observations=(
                SourceObservationWrite(
                    run_id=run_id,
                    key=key,
                    success=True,
                    request_start="2026-08-30T00:00:00+00:00",
                    request_end="2026-09-01T00:00:00+00:00",
                    response_hash="b" * 64,
                    candle_count=1,
                    latest_timestamp=candle.timestamp,
                    observed_at="2026-09-01T00:00:00+00:00",
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

    snapshot = build_mvp_health_matrix(
        manifest,
        store,
        now=datetime(2026, 8, 31, 1, tzinfo=timezone.utc),
        instrument_ids=("CRYPTO.PERP.BTC",),
    )
    btc_1h = next(
        cell
        for cell in snapshot["cells"]
        if cell["display_symbol"] == "BTC" and cell["timeframe"] == "1h"
    )
    assert btc_1h["status"] == "ready"
    assert btc_1h["latest_closed_timestamp"] == candle.timestamp
    assert btc_1h["watermark"]["run_id"] == run_id
    assert snapshot["coverage"]["1h"]["ready"] == 1
    assert snapshot["coverage"]["1h"]["applicable"] == 1
    assert (
        snapshot["coverage"]["1h"]["ready"]
        + snapshot["coverage"]["1h"]["partial"]
        + snapshot["coverage"]["1h"]["stale"]
        + snapshot["coverage"]["1h"]["failed"]
        + snapshot["coverage"]["1h"]["blocked"]
        + snapshot["coverage"]["1h"]["unavailable"]
        == snapshot["coverage"]["1h"]["applicable"]
    )


def test_health_matrix_api_and_ui_are_chinese_and_read_only(
    tmp_path: Path,
) -> None:
    from kline.config import Settings
    from kline.registry import init

    init(Settings(db_path=str(tmp_path / "api.db"), load_entrypoint_adapters=False))
    with TestClient(create_app()) as client:
        response = client.get("/api/mvp/health/matrix")
        page = client.get("/health-ui")

    assert response.status_code == 200
    payload = response.json()
    assert len(payload["cells"]) == 6 * 5
    assert payload["scope"] == {
        "name": "demo_3x3",
        "instrument_count": 6,
        "universes": {"a_share": 3, "us_stock": 3},
    }
    assert payload["refresh"]["poll_interval_seconds"] == 30
    assert payload["refresh"]["request_timeout_seconds"] == 10
    assert page.status_code == 200
    assert "资产 × 时间级别健康矩阵" in page.text
    assert "最近一次运行" in page.text
    assert "system notification" not in page.text.lower()
    assert "重试" not in page.text
