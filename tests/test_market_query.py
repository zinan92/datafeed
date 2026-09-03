from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from kline.app import create_app
from kline.market_query import MarketQueryReader
from kline.models import AssetClass, CachePolicy, Candle, QualityPolicy, Timeframe
from kline.storage import (
    CandleSeriesKey,
    MvpCandle,
    MvpRunWrite,
    QualityReceiptWrite,
    SourceObservationWrite,
    WatermarkWrite,
)
from kline.store import KlineStore


MANIFEST_PATH = Path(__file__).parents[1] / "configs" / "watchlist_manifest.json"


def _seed_spy(store: KlineStore, *, rows: int = 2) -> None:
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
    candles = tuple(
        MvpCandle(
            key=key,
            timestamp=f"2026-09-0{index + 1}T00:00:00+00:00",
            open=100 + index,
            high=102 + index,
            low=99 + index,
            close=101 + index,
            volume=1000 + index,
        )
        for index in range(rows)
    )
    run_id = "market-query-spy"
    store.commit_mvp_run(
        MvpRunWrite(
            run_id=run_id,
            manifest_version=key.manifest_version,
            manifest_hash="a" * 64,
            started_at="2026-09-03T07:00:00+00:00",
            completed_at="2026-09-03T07:01:00+00:00",
            window_start=None,
            window_end="2026-09-03T07:00:00+00:00",
            policy={},
            candles=candles,
            source_observations=(
                SourceObservationWrite(
                    run_id=run_id,
                    key=key,
                    success=True,
                    request_start=None,
                    request_end="2026-09-03T07:00:00+00:00",
                    response_hash="b" * 64,
                    policy={
                        "source_identity": {
                            "provider_symbol": "SPY",
                            "repair_attempted": False,
                        }
                    },
                    candle_count=len(candles),
                    latest_timestamp=candles[-1].timestamp,
                    observed_at="2026-09-03T07:01:00+00:00",
                ),
            ),
            quality_receipts=(QualityReceiptWrite(run_id=run_id, key=key, status="pass"),),
            watermarks=(
                WatermarkWrite(
                    key=key,
                    last_closed_timestamp=candles[-1].timestamp,
                    cursor=None,
                    run_id=run_id,
                ),
            ),
        )
    )


def test_resolver_maps_display_and_provider_aliases_without_generic_class_ignoring(
    tmp_path: Path,
) -> None:
    market_db = tmp_path / "market.db"
    KlineStore(str(market_db))
    reader = MarketQueryReader(MANIFEST_PATH, market_db, minimum_series_rows=1)

    expected = {
        (AssetClass.ETF, "SPY", "yahoo_finance_etf"): "WATCH.CROSS.SPX",
        (AssetClass.ETF, "QQQ", "yahoo_finance_etf"): "WATCH.CROSS.NDX",
        (AssetClass.ETF, "UUP", "yahoo_finance_etf"): "WATCH.CROSS.DXY",
        (AssetClass.INDEX, "^VIX", "yahoo_finance_index"): "WATCH.CROSS.VIX",
        (AssetClass.US_STOCK, "SPY", "auto"): "WATCH.CROSS.SPX",
        (AssetClass.US_STOCK, "QQQ", "auto"): "WATCH.CROSS.NDX",
        (AssetClass.US_STOCK, "UUP", "auto"): "WATCH.CROSS.DXY",
        (AssetClass.US_STOCK, "^VIX", "auto"): "WATCH.CROSS.VIX",
    }
    for (asset_class, ticker, source), instrument_id in expected.items():
        resolved = reader.resolve_identity(
            asset_class=asset_class,
            ticker=ticker,
            requested_source=source,
        )
        assert resolved is not None
        assert resolved.instrument_id == instrument_id

    assert reader.resolve_identity(
        asset_class=AssetClass.COMMODITY,
        ticker="SPY",
        requested_source="auto",
    ) is None
    assert reader.resolve_identity(
        asset_class=AssetClass.US_STOCK,
        ticker="UNKNOWN",
        requested_source="auto",
    ) is None

    dxy_long_window = reader.read(
        asset_class=AssetClass.US_STOCK,
        ticker="UUP",
        timeframe=Timeframe.DAY,
        requested_source="auto",
        limit=1008,
    )
    assert dxy_long_window.hit is False
    assert dxy_long_window.miss_reason == "market_window_unverified"
    assert MarketQueryReader(MANIFEST_PATH, market_db, minimum_series_rows=1).read(
        asset_class=AssetClass.ETF,
        ticker="UUP",
        timeframe=Timeframe.DAY,
        requested_source="yahoo_finance_etf",
        limit=300,
    ).miss_reason == "market_window_unverified"


def test_market_reader_returns_native_daily_data_and_explicit_misses(tmp_path: Path) -> None:
    market_db = tmp_path / "market.db"
    store = KlineStore(str(market_db))
    _seed_spy(store)
    store._engine.dispose()
    reader = MarketQueryReader(MANIFEST_PATH, market_db, minimum_series_rows=1)

    hit = reader.read(
        asset_class=AssetClass.ETF,
        ticker="SPY",
        timeframe=Timeframe.DAY,
        requested_source="yahoo_finance_etf",
        limit=2,
    )
    assert hit.hit is True
    assert [candle.timestamp for candle in hit.candles] == ["2026-09-01", "2026-09-02"]
    assert hit.source_identity["query_served_from"] == "market_data_database"
    assert hit.source_identity["instrument_id"] == "WATCH.CROSS.SPX"
    assert hit.source_identity["identity_role"] == "proxy"
    assert hit.source_identity["repair_attempted"] is False

    exclusive_end = reader.read(
        asset_class=AssetClass.ETF,
        ticker="SPY",
        timeframe=Timeframe.DAY,
        requested_source="yahoo_finance_etf",
        limit=1,
        end="2026-09-02",
    )
    assert exclusive_end.hit is True
    assert [candle.timestamp for candle in exclusive_end.candles] == ["2026-09-01"]

    intraday = reader.read(
        asset_class=AssetClass.ETF,
        ticker="SPY",
        timeframe=Timeframe.MIN_30,
        requested_source="yahoo_finance_etf",
        limit=2,
    )
    assert intraday.hit is False
    assert intraday.miss_reason == "timeframe_not_persisted"

    insufficient = MarketQueryReader(
        MANIFEST_PATH, market_db, minimum_series_rows=3
    ).read(
        asset_class=AssetClass.ETF,
        ticker="SPY",
        timeframe=Timeframe.DAY,
        requested_source="yahoo_finance_etf",
        limit=3,
    )
    assert insufficient.hit is False
    assert insufficient.miss_reason == "insufficient_market_history"

    request_exceeds_history = reader.read(
        asset_class=AssetClass.ETF,
        ticker="SPY",
        timeframe=Timeframe.DAY,
        requested_source="yahoo_finance_etf",
        limit=3,
    )
    assert request_exceeds_history.hit is False
    assert request_exceeds_history.miss_reason == "insufficient_market_history"


def test_market_first_api_preserves_envelope_and_marks_both_backends(
    monkeypatch, tmp_path: Path
) -> None:
    legacy_db = tmp_path / "legacy.db"
    market_db = tmp_path / "market.db"
    legacy = KlineStore(str(legacy_db))
    legacy.save(
        "AAPL",
        AssetClass.US_STOCK,
        Timeframe.MIN_15,
        [Candle(timestamp="2026-09-03T10:00:00+00:00", open=100, high=102, low=99, close=101, volume=10)],
        source_id="yahoo_finance",
    )
    _seed_spy(KlineStore(str(market_db)), rows=1)
    monkeypatch.setenv("KLINE_DB_PATH", str(legacy_db))
    monkeypatch.setenv("KLINE_MARKET_DB_PATH", str(market_db))
    monkeypatch.setenv("KLINE_QUERY_BACKEND", "market_first")
    monkeypatch.setenv("KLINE_MARKET_MIN_ROWS", "1")
    monkeypatch.setenv("KLINE_LOAD_ENTRYPOINT_ADAPTERS", "false")

    with TestClient(create_app()) as client:
        market = client.get(
            "/api/candles/etf/SPY",
            params={
                "timeframe": "1d",
                "source": "yahoo_finance_etf",
                "cache_policy": CachePolicy.BYPASS.value,
                "quality": QualityPolicy.STANDARD.value,
                "limit": 1,
            },
        )
        legacy_response = client.get(
            "/api/candles/us_stock/AAPL",
            params={
                "timeframe": "15m",
                "source": "yahoo_finance",
                "cache_policy": CachePolicy.ALLOW.value,
                "quality": QualityPolicy.STANDARD.value,
                "limit": 1,
            },
        )
        health = client.get("/api/health")
        legacy_error = client.get(
            "/api/candles/us_stock/MSFT",
            params={
                "timeframe": "1d",
                "source": "yahoo_finance",
                "cache_policy": CachePolicy.REQUIRE.value,
                "quality": QualityPolicy.STANDARD.value,
                "limit": 1,
            },
        )

    assert market.status_code == 200
    market_payload = market.json()
    assert market_payload["ticker"] == "SPY"
    assert market_payload["asset_class"] == "etf"
    assert market_payload["served_from"] == "upstream"
    assert market_payload["instrument_id"] == "WATCH.CROSS.SPX"
    assert market_payload["source_identity"]["query_served_from"] == "market_data_database"

    assert legacy_response.status_code == 200
    legacy_payload = legacy_response.json()
    assert legacy_payload["served_from"] == "cache"
    assert legacy_payload["source_identity"]["query_served_from"] == "legacy_cache"
    assert legacy_payload["source_identity"]["served_from"] == "legacy_cache"
    assert legacy_payload["source_identity"]["market_data_miss_reason"] == (
        "timeframe_not_persisted"
    )

    query_health = health.json()["query_backend"]
    assert query_health["mode"] == "market_first"
    assert query_health["market_hits"] == 1
    assert query_health["legacy_fallbacks"] == 1
    assert query_health["market_database_path"] == "redacted"

    assert legacy_error.status_code == 404
    legacy_error_identity = legacy_error.json()["detail"]["source_identity"]
    assert legacy_error_identity["served_from"] == "legacy_cache"
    assert legacy_error_identity["query_served_from"] == "legacy_cache"
    assert legacy_error_identity["market_data_miss_reason"] == "identity_not_found"


def test_market_first_missing_database_fails_closed_without_path(
    monkeypatch, tmp_path: Path
) -> None:
    legacy_db = tmp_path / "legacy.db"
    missing_market_db = tmp_path / "secret-market-location.db"
    KlineStore(str(legacy_db))
    monkeypatch.setenv("KLINE_DB_PATH", str(legacy_db))
    monkeypatch.setenv("KLINE_MARKET_DB_PATH", str(missing_market_db))
    monkeypatch.setenv("KLINE_QUERY_BACKEND", "market_first")
    monkeypatch.setenv("KLINE_LOAD_ENTRYPOINT_ADAPTERS", "false")

    with TestClient(create_app()) as client:
        response = client.get(
            "/api/candles/etf/SPY",
            params={"timeframe": "1d", "source": "yahoo_finance_etf"},
        )
        health = client.get("/api/health")

    assert response.status_code == 503
    assert response.json()["detail"]["error"] == "market_query_unavailable"
    assert str(missing_market_db) not in response.text
    assert health.json()["query_backend"] == {
        "mode": "invalid",
        "status": "failed",
        "detail": "RuntimeError",
        "market_database_path": "redacted",
        "market_hits": 0,
        "market_misses": 0,
        "legacy_fallbacks": 0,
        "legacy_unique_cells": 0,
        "miss_reasons": {},
    }


def test_legacy_mode_remains_default_and_does_not_add_cutover_markers(
    monkeypatch, tmp_path: Path
) -> None:
    legacy_db = tmp_path / "legacy-only.db"
    legacy = KlineStore(str(legacy_db))
    legacy.save(
        "AAPL",
        AssetClass.US_STOCK,
        Timeframe.MIN_15,
        [
            Candle(
                timestamp="2026-09-03T10:00:00+00:00",
                open=100,
                high=102,
                low=99,
                close=101,
                volume=10,
            )
        ],
        source_id="yahoo_finance",
    )
    monkeypatch.setenv("KLINE_DB_PATH", str(legacy_db))
    monkeypatch.delenv("KLINE_QUERY_BACKEND", raising=False)
    monkeypatch.delenv("KLINE_MARKET_DB_PATH", raising=False)
    monkeypatch.setenv("KLINE_LOAD_ENTRYPOINT_ADAPTERS", "false")

    with TestClient(create_app()) as client:
        response = client.get(
            "/api/candles/us_stock/AAPL",
            params={
                "timeframe": "15m",
                "source": "yahoo_finance",
                "cache_policy": "allow",
            },
        )
        health = client.get("/api/health")

    assert response.status_code == 200
    assert "query_served_from" not in response.json()["source_identity"]
    assert health.json()["query_backend"]["mode"] == "legacy"
