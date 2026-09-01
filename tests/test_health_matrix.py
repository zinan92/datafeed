from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import replace
import json
from pathlib import Path

from fastapi.testclient import TestClient

from kline.app import create_app
from kline.free_source_profile import apply_free_source_profile
from kline.health_matrix import (
    MVP_DEMO_INSTRUMENT_IDS,
    _entitlement_block_reason,
    _freshness_stale,
    build_mvp_health_matrix,
)
from kline.mvp_manifest import load_manifest, manifest_digest
from kline.storage import (
    CandleSeriesKey,
    EntitlementReceiptWrite,
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
        scope="demo_3x3",
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
                    policy={"api_key": "supersecret", "safe": "keep"},
                    candle_count=1,
                    latest_timestamp=candle.timestamp,
                    observed_at="2026-09-01T00:00:00+00:00",
                    error="token=abc secretvalue",
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
            entitlement_receipts=(
                EntitlementReceiptWrite(
                    receipt_id="matrix-entitlement-1",
                    source_id="hyperliquid_perpetual_public",
                    status="active",
                    allowed_history={"days": 365},
                    timeframe_permissions=("15m", "1h", "4h", "1d", "1w"),
                    persistence_allowed=True,
                    derived_allowed=True,
                    non_display_allowed=True,
                    valid_from="2026-01-01",
                    valid_to=None,
                    evidence_ref="operator://matrix-test",
                    receipt_hash="c" * 64,
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
    assert btc_1h["entitlement"]["status"] == "active"
    assert snapshot["coverage"]["1h"]["ready"] == 1
    assert snapshot["coverage"]["1h"]["technical_ready"] == 1
    assert btc_1h["run_id"] == run_id
    serialized = json.dumps(btc_1h, ensure_ascii=False)
    assert "supersecret" not in serialized
    assert "abc" not in serialized
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


def test_not_applicable_cell_keeps_the_full_null_payload_shape(tmp_path: Path) -> None:
    manifest = load_manifest(MANIFEST_PATH)
    store = KlineStore(str(tmp_path / "matrix-not-applicable.db"))
    snapshot = build_mvp_health_matrix(
        manifest,
        store,
        instrument_ids=("US.ETF.UUP",),
        now=datetime(2026, 9, 1, 12, tzinfo=timezone.utc),
    )

    intraday = next(cell for cell in snapshot["cells"] if cell["timeframe"] == "15m")
    daily = next(cell for cell in snapshot["cells"] if cell["timeframe"] == "1d")
    assert intraday["applicability"] == "not_applicable"
    assert intraday["status"] == "not_applicable"
    for field in (
        "provider_symbol",
        "source_id",
        "source_mode",
        "entitlement",
        "latest_closed_timestamp",
        "transform",
        "last_attempt_at",
        "last_success_at",
        "run_id",
        "policy",
        "coverage",
        "quality",
        "watermark",
        "error",
    ):
        assert intraday[field] is None
    assert daily["applicability"] == "applicable"
    assert daily["status"] == "blocked"
    assert daily["status_reason"] == "entitlement_unverified"


def test_full_scope_preserves_manifest_cartesian_product_and_coverage_invariants(
    tmp_path: Path,
) -> None:
    manifest = load_manifest(MANIFEST_PATH)
    store = KlineStore(str(tmp_path / "matrix-full.db"))
    snapshot = build_mvp_health_matrix(
        manifest,
        store,
        now=datetime(2026, 9, 1, 12, tzinfo=timezone.utc),
    )

    assert len(snapshot["cells"]) == 216 * 5
    assert all(cell["timeframe"] != "30m" for cell in snapshot["cells"])
    assert snapshot["scope"]["name"] == "full_216"
    for timeframe, counts in snapshot["coverage"].items():
        total = sum(1 for cell in snapshot["cells"] if cell["timeframe"] == timeframe)
        assert counts["applicable"] + counts["not_applicable"] == total
        assert (
            sum(
                counts[state]
                for state in ("ready", "partial", "stale", "failed", "blocked", "unavailable")
            )
            == counts["applicable"]
        )


def test_freshness_uses_session_and_continuous_calendar_slas() -> None:
    manifest = load_manifest(MANIFEST_PATH)
    aapl = next(item for item in manifest.instruments if item.display_symbol == "AAPL")
    aapl = replace(aapl, source_status="configured")
    now = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
    assert _freshness_stale(aapl, "1d", "2026-08-25T00:00:00+00:00", now=now)

    btc = next(item for item in manifest.instruments if item.display_symbol == "BTC")
    assert _freshness_stale(btc, "1h", "2026-08-31T00:00:00+00:00", now=now)


def test_derived_entitlement_does_not_block_native_one_hour_cells() -> None:
    manifest = load_manifest(MANIFEST_PATH)
    btc = next(item for item in manifest.instruments if item.display_symbol == "BTC")
    receipt = {
        "status": "active",
        "persistence_allowed": True,
        "derived_allowed": False,
        "timeframe_permissions": ("15m", "1h", "4h", "1d", "1w"),
    }
    assert _entitlement_block_reason(btc, "1h", receipt, derived=False) is None
    assert _entitlement_block_reason(btc, "1h", receipt, derived=True) == "derived_not_allowed"


def test_health_matrix_joins_receipts_to_a_qfq_free_profile_cell(tmp_path: Path) -> None:
    manifest = apply_free_source_profile(load_manifest(MANIFEST_PATH))
    store = KlineStore(str(tmp_path / "matrix-qfq.db"))
    key = CandleSeriesKey(
        instrument_id="CN.A.600519",
        display_symbol="600519",
        provider_symbol="600519.SH",
        source_id="tencent_stock_free",
        asset_class="a_share",
        timeframe="15m",
        adjustment_basis="qfq",
        manifest_version=manifest.version,
    )
    run_id = "qfq-run-1"
    candle = MvpCandle(
        key=key,
        timestamp="2026-09-01T06:45:00+00:00",
        open=100,
        high=102,
        low=99,
        close=101,
        volume=10,
    )
    store.commit_mvp_run(
        MvpRunWrite(
            run_id=run_id,
            manifest_version=manifest.version,
            manifest_hash=manifest_digest(manifest),
            started_at="2026-09-01T07:00:00+00:00",
            window_start=None,
            window_end="2026-09-01T07:00:00+00:00",
            policy={},
            candles=(candle,),
            source_observations=(
                SourceObservationWrite(
                    run_id=run_id,
                    key=key,
                    success=True,
                    request_start=None,
                    request_end=None,
                    response_hash=None,
                    candle_count=1,
                    latest_timestamp=candle.timestamp,
                    policy={"source_identity": {"selected_source": "tencent"}},
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
        manifest, store, scope="demo_3x3", now=datetime(2026, 9, 1, 8, tzinfo=timezone.utc)
    )
    cell = next(
        item
        for item in snapshot["cells"]
        if item["display_symbol"] == "600519" and item["timeframe"] == "15m"
    )
    assert cell["status"] == "partial"
    assert cell["technical_status"] == "ready"
    assert cell["row_count"] == 1
    assert cell["policy"]["source_identity"]["selected_source"] == "tencent"


def test_run_timeline_does_not_fallback_to_older_than_24_hours(tmp_path: Path) -> None:
    manifest = load_manifest(MANIFEST_PATH)
    store = KlineStore(str(tmp_path / "matrix-runs.db"))
    store.commit_mvp_run(
        MvpRunWrite(
            run_id="old-run",
            manifest_version=manifest.version,
            manifest_hash="a" * 64,
            started_at="2026-08-01T00:00:00+00:00",
            window_start=None,
            window_end=None,
            policy={},
            completed_at="2026-08-01T00:01:00+00:00",
        )
    )
    snapshot = build_mvp_health_matrix(
        manifest,
        store,
        scope="demo_3x3",
        now=datetime(2026, 9, 1, 12, tzinfo=timezone.utc),
    )
    assert snapshot["runs"] == []


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
    assert len(payload["cells"]) == 216 * 5
    assert payload["scope"] == {
        "name": "full_216",
        "instrument_count": 216,
        "universes": {"a_share": 100, "us_stock": 100, "cross_market": 16},
    }
    assert (
        next(cell for cell in payload["cells"] if cell["display_symbol"] == "AAPL")["source_id"]
        == "yahoo_finance_free"
    )
    assert (
        next(cell for cell in payload["cells"] if cell["display_symbol"] == "600519")["source_id"]
        == "tencent_stock_free"
    )
    assert payload["refresh"]["poll_interval_seconds"] == 30
    assert payload["refresh"]["request_timeout_seconds"] == 10
    assert page.status_code == 200
    assert "资产 × 时间级别健康矩阵" in page.text
    assert "部分可用（含授权阻塞）" in page.text
    assert "有技术数据" in page.text
    assert "最近一次运行" in page.text
    assert "system notification" not in page.text.lower()
    assert "重试" not in page.text


def test_matrix_api_failure_preserves_last_success_timestamp(monkeypatch, tmp_path: Path) -> None:
    import kline.api as api_module
    from kline.config import Settings
    from kline.registry import init

    init(Settings(db_path=str(tmp_path / "api-loss.db"), load_entrypoint_adapters=False))

    with TestClient(create_app()) as client:
        assert client.get("/api/mvp/health/matrix").status_code == 200

        def fail_manifest(_path: Path):
            raise RuntimeError("token=should-not-leak")

        monkeypatch.setattr(api_module, "load_manifest", fail_manifest)
        response = client.get("/api/mvp/health/matrix")

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["error"] == "dashboard_unavailable"
    assert detail["last_success_at"]
    assert "should-not-leak" not in response.text


def test_matrix_api_exposes_demo_scope_only_when_explicit(tmp_path: Path) -> None:
    from kline.config import Settings
    from kline.registry import init

    init(Settings(db_path=str(tmp_path / "api-scope.db"), load_entrypoint_adapters=False))
    with TestClient(create_app()) as client:
        demo = client.get("/api/mvp/health/matrix?scope=demo_3x3")
        invalid = client.get("/api/mvp/health/matrix?scope=source_switch")

    assert demo.status_code == 200
    assert demo.json()["scope"]["name"] == "demo_3x3"
    assert len(demo.json()["cells"]) == 30
    assert invalid.status_code == 400
