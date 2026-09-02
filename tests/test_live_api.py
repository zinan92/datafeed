"""Tests for strict/live REST behavior."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException

from kline.api import get_candles
from kline.models import (
    AssetClass,
    CachePolicy,
    Candle,
    FallbackPolicy,
    QualityPolicy,
    Timeframe,
    TimeframeTransform,
)
from kline.ports import ProviderMeta, SourceManifest
from kline.providers.base import ProviderError
from kline.store import KlineStore


def _fresh_ts() -> str:
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    return now.strftime("%Y-%m-%dT%H:%M:%S")


def _candle(ts: str, close: float) -> Candle:
    return Candle(timestamp=ts, open=close, high=close + 1, low=close - 1, close=close, volume=10)


async def _get_candles(**overrides):
    params = {
        "timeframe": Timeframe.MIN_1,
        "start": None,
        "end": None,
        "limit": 1,
        "refresh": False,
        "source": "auto",
        "cache_policy": CachePolicy.ALLOW,
        "quality": QualityPolicy.STANDARD,
        "fallback_policy": FallbackPolicy.NONE,
        "require_execution_venue": False,
        "profile": None,
        "strict": False,
        "mode": "research",
    }
    params.update(overrides)
    return await get_candles(**params)


class FakeAdapter:
    def __init__(self, candles: list[Candle] | None = None, error: ProviderError | None = None) -> None:
        self.candles = candles or []
        self.error = error
        self.called = False
        self.manifest = SourceManifest(
            source_id="binance_usdm_futures",
            asset_class=AssetClass.COMMODITY,
            meta=ProviderMeta(
                name="binance_usdm_futures",
                source_mode="binance_usdm_futures",
                quality_flags=("public_api", "usd_m_futures", "live", "execution_venue"),
                continuous=True,
                execution_venue=True,
                realtime_supported=True,
                market_type="usd_m_futures",
                supported_symbols=("XAUUSDT",),
            ),
        )
        self._last_raw_response = {
            "request_params": {"symbol": "XAUUSDT", "interval": "1m"},
            "response_body": [["raw"]],
            "status_code": 200,
            "error": None,
        }

    @property
    def last_raw_response(self):
        return self._last_raw_response

    def canonical_ticker(self, ticker: str) -> str:
        return ticker

    def supported_timeframes(self) -> list[Timeframe]:
        return [Timeframe.MIN_1, Timeframe.MIN_5]

    async def fetch_candles(self, *_args, **_kwargs) -> list[Candle]:
        self.called = True
        if self.error:
            self._last_raw_response["error"] = str(self.error)
            self._last_raw_response["status_code"] = None
            raise self.error
        return self.candles


class FakeNativeCryptoAdapter(FakeAdapter):
    def __init__(self, candles: list[Candle]) -> None:
        super().__init__(candles)
        self.manifest = SourceManifest(
            source_id="binance_spot_public",
            asset_class=AssetClass.CRYPTO,
            meta=ProviderMeta(
                name="binance_spot",
                source_mode="binance_spot_public",
                quality_flags=("public_api", "spot", "research_only", "not_execution_venue"),
                continuous=True,
                realtime_supported=True,
                market_type="spot",
            ),
        )
        self._last_raw_response = {
            "provider_symbol": "BTCUSDT",
        }
        self.timeframe_transform = TimeframeTransform(
            raw_timeframe=Timeframe.HOUR_4,
            timeframe_origin="native",
            aggregation={"kind": "none", "rule": "native_passthrough"},
        )
        self.source_identity = {"provider_symbol": "BTCUSDT"}

    def supported_timeframes(self) -> list[Timeframe]:
        return [Timeframe.HOUR_4]


@pytest.fixture
def store(tmp_path: Path) -> KlineStore:
    return KlineStore(str(tmp_path / "test.db"))


async def test_strict_true_ignores_existing_cache(monkeypatch, store: KlineStore):
    cached = [_candle("2000-01-01T00:00:00", 1.0)]
    store.save("XAUUSDT", AssetClass.COMMODITY, Timeframe.MIN_1, cached)
    live = FakeAdapter([_candle(_fresh_ts(), 3300.0)])
    monkeypatch.setattr("kline.api.get_store", lambda: store)
    monkeypatch.setattr("kline.api.get_adapter_for_source", lambda *_args: live)

    response = await _get_candles(
        asset_class=AssetClass.COMMODITY,
        ticker="XAUUSDT",
        timeframe=Timeframe.MIN_1,
        start=None,
        end=None,
        limit=1,
        refresh=False,
        strict=True,
        mode="research",
    )

    assert live.called is True
    assert response.served_from == "upstream"
    assert response.provider == "binance_usdm_futures"
    assert response.execution_venue is True
    assert response.cache_policy == CachePolicy.BYPASS
    assert response.quality_policy == QualityPolicy.STRICT
    assert response.require_execution_venue is True
    assert response.is_synthetic is False
    assert response.candles[0].close == 3300.0
    assert response.candles[0].close != cached[0].close


async def test_mode_live_uses_strict_live_path(monkeypatch, store: KlineStore):
    store.save("XAUUSDT", AssetClass.COMMODITY, Timeframe.MIN_1, [_candle("2000-01-01T00:00:00", 1.0)])
    live = FakeAdapter([_candle(_fresh_ts(), 3310.0)])
    monkeypatch.setattr("kline.api.get_store", lambda: store)
    monkeypatch.setattr("kline.api.get_adapter_for_source", lambda *_args: live)

    response = await _get_candles(
        asset_class=AssetClass.COMMODITY,
        ticker="XAUUSDT",
        timeframe=Timeframe.MIN_1,
        start=None,
        end=None,
        limit=1,
        refresh=False,
        strict=False,
        mode="live",
    )

    assert live.called is True
    assert response.served_from == "upstream"
    assert response.cache_policy == CachePolicy.BYPASS
    assert response.quality_policy == QualityPolicy.STRICT
    assert response.candles[0].close == 3310.0


async def test_strict_upstream_failure_does_not_return_old_cache(monkeypatch, store: KlineStore):
    store.save("XAUUSDT", AssetClass.COMMODITY, Timeframe.MIN_1, [_candle("2000-01-01T00:00:00", 1.0)])
    live = FakeAdapter(error=ProviderError("exchange unavailable"))
    monkeypatch.setattr("kline.api.get_store", lambda: store)
    monkeypatch.setattr("kline.api.get_adapter_for_source", lambda *_args: live)

    with pytest.raises(HTTPException) as exc:
        await _get_candles(
            asset_class=AssetClass.COMMODITY,
            ticker="XAUUSDT",
            timeframe=Timeframe.MIN_1,
            start=None,
            end=None,
            limit=1,
            refresh=False,
            strict=True,
            mode="research",
        )

    assert live.called is True
    assert exc.value.status_code == 502
    assert exc.value.detail["error"] == "upstream_error"
    assert exc.value.detail["served_from"] == "upstream"
    assert exc.value.detail["cache_policy"] == "bypass"
    assert exc.value.detail["quality_policy"] == "strict"
    assert exc.value.detail["reject_reason"] == "upstream_error"
    assert exc.value.detail["provider"] == "binance_usdm_futures"
    assert exc.value.detail["timeframe"] == "1m"
    assert exc.value.detail["provider_symbol"] == "XAUUSDT"
    assert exc.value.detail["selected_source"] == "binance_usdm_futures"
    assert exc.value.detail["attempted_sources"] == ["binance_usdm_futures"]


async def test_native_4h_metadata_reaches_api_without_reaggregation(monkeypatch, store: KlineStore):
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    candles = [
        _candle((now - timedelta(hours=4)).isoformat(), 100.0),
        _candle(now.isoformat(), 101.0),
    ]
    native = FakeNativeCryptoAdapter(candles)
    monkeypatch.setattr("kline.api.get_store", lambda: store)
    monkeypatch.setattr("kline.api.get_adapter_for_source", lambda *_args: native)

    response = await _get_candles(
        asset_class=AssetClass.CRYPTO,
        ticker="BTC",
        timeframe=Timeframe.HOUR_4,
        limit=2,
        source="binance_spot_public",
        cache_policy=CachePolicy.BYPASS,
        quality=QualityPolicy.STRICT,
        fallback_policy=FallbackPolicy.NONE,
    )

    assert response.raw_timeframe == Timeframe.HOUR_4
    assert response.timeframe_origin == "native"
    assert response.aggregation["rule"] == "native_passthrough"
    assert response.provider_symbol == "BTCUSDT"
    assert response.source_identity["provider_symbol"] == "BTCUSDT"


async def test_source_quality_loss_reaches_envelope_and_operator_receipt(
    monkeypatch, store: KlineStore
):
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    upstream = FakeNativeCryptoAdapter([_candle(now.isoformat(), 101.0)])
    upstream.source_identity.update(
        {
            "quality_flags": ["invalid_row_excluded"],
            "excluded_row_count": 1,
            "excluded_rows": [
                {"timestamp": "2021-09-01", "reason": "non_finite_ohlcv"}
            ],
        }
    )
    monkeypatch.setattr("kline.api.get_store", lambda: store)
    monkeypatch.setattr("kline.api.get_adapter_for_source", lambda *_args: upstream)

    response = await _get_candles(
        asset_class=AssetClass.CRYPTO,
        ticker="BTC",
        timeframe=Timeframe.HOUR_4,
        limit=1,
        source="binance_spot_public",
        cache_policy=CachePolicy.ALLOW,
        quality=QualityPolicy.STANDARD,
    )

    assert "invalid_row_excluded" in response.quality_flags
    assert "invalid_row_excluded" in response.candles[0].quality_flags
    assert response.source_identity["excluded_row_count"] == 1
    observations = store.latest_source_observations()
    assert "invalid_row_excluded" in observations[0]["quality_flags"]


async def test_all_excluded_error_keeps_quality_loss_in_error_receipt(
    monkeypatch, store: KlineStore
):
    upstream = FakeAdapter(error=ProviderError("Yahoo all rows failed quality validation"))
    upstream.source_identity = {
        "provider_symbol": "QCOM",
        "quality_flags": ["invalid_row_excluded"],
        "excluded_row_count": 1,
    }
    monkeypatch.setattr("kline.api.get_store", lambda: store)
    monkeypatch.setattr("kline.api.get_adapter_for_source", lambda *_args: upstream)

    with pytest.raises(HTTPException) as exc:
        await _get_candles(
            asset_class=AssetClass.COMMODITY,
            ticker="XAUUSDT",
            timeframe=Timeframe.MIN_1,
            source="binance_usdm_futures",
            cache_policy=CachePolicy.ALLOW,
        )

    assert exc.value.status_code == 502
    assert "invalid_row_excluded" in exc.value.detail["quality_flags"]
    assert exc.value.detail["source_identity"]["excluded_row_count"] == 1
    observations = store.latest_source_observations()
    assert "invalid_row_excluded" in observations[0]["quality_flags"]


async def test_api_rejects_4h_for_non_context_yahoo_symbols(store: KlineStore):
    with pytest.raises(HTTPException) as exc:
        await _get_candles(
            asset_class=AssetClass.INDEX,
            ticker="^GSPC",
            timeframe=Timeframe.HOUR_4,
            source="yahoo_finance_index",
            cache_policy=CachePolicy.BYPASS,
            quality=QualityPolicy.STRICT,
            fallback_policy=FallbackPolicy.NONE,
        )

    assert exc.value.status_code == 400
    assert exc.value.detail["error"] == "timeframe_not_supported"
    assert exc.value.detail["selected_source"] == "yahoo_finance_index"
    assert exc.value.detail["timeframe"] == "4h"
    assert exc.value.detail["provider_symbol"] == "^GSPC"
    assert exc.value.detail["raw_timeframe"] is None


async def test_strict_empty_upstream_response_is_blocked(monkeypatch, store: KlineStore):
    live = FakeAdapter([])
    monkeypatch.setattr("kline.api.get_store", lambda: store)
    monkeypatch.setattr("kline.api.get_adapter_for_source", lambda *_args: live)

    with pytest.raises(HTTPException) as exc:
        await _get_candles(
            asset_class=AssetClass.COMMODITY,
            ticker="XAUUSDT",
            timeframe=Timeframe.MIN_1,
            start=None,
            end=None,
            limit=1,
            refresh=False,
            strict=True,
            mode="research",
        )

    assert exc.value.status_code == 503
    assert exc.value.detail["error"] == "data_blocked"
    assert exc.value.detail["reject_reason"] == "empty_data"
    assert "empty" in exc.value.detail["quality_flags"]


async def test_cache_policy_require_returns_cache_without_upstream(monkeypatch, store: KlineStore):
    cached = [_candle("2026-07-09T10:00:00", 101.0)]
    store.save(
        "BTC",
        AssetClass.CRYPTO,
        Timeframe.MIN_1,
        cached,
        source_id="binance_spot_public",
    )
    live = FakeAdapter([_candle(_fresh_ts(), 999.0)])
    monkeypatch.setattr("kline.api.get_store", lambda: store)
    monkeypatch.setattr("kline.api.get_adapter_for_source", lambda *_args: live)

    response = await _get_candles(
        asset_class=AssetClass.CRYPTO,
        ticker="BTC",
        timeframe=Timeframe.MIN_1,
        start=None,
        end=None,
        limit=1,
        refresh=False,
        cache_policy=CachePolicy.REQUIRE,
        mode="research",
    )

    assert live.called is False
    assert response.served_from == "cache"
    assert response.cache_policy == CachePolicy.REQUIRE
    assert response.candles[0].close == 101.0
    assert response.raw_timeframe is None
    assert response.timeframe_origin is None


async def test_phase1_cache_without_timeframe_receipt_blocks_instead_of_relabeling(
    monkeypatch, store: KlineStore
):
    store.save(
        "BTC",
        AssetClass.CRYPTO,
        Timeframe.DAY,
        [_candle("2026-07-09", 101.0)],
        source_id="binance_spot_public",
    )
    monkeypatch.setattr("kline.api.get_store", lambda: store)

    with pytest.raises(HTTPException) as exc:
        await _get_candles(
            asset_class=AssetClass.CRYPTO,
            ticker="BTC",
            timeframe=Timeframe.DAY,
            source="binance_spot_public",
            cache_policy=CachePolicy.ALLOW,
            quality=QualityPolicy.STANDARD,
        )

    assert exc.value.status_code == 503
    assert exc.value.detail["reject_reason"] == "timeframe_transform_missing"
    assert exc.value.detail["provider_symbol"] == "BTC"


async def test_realtime_profile_bypasses_cache_without_requiring_execution_venue(
    monkeypatch, store: KlineStore
):
    store.save("BTC", AssetClass.CRYPTO, Timeframe.MIN_1, [_candle("2000-01-01T00:00:00", 1.0)])
    upstream = FakeAdapter([_candle(_fresh_ts(), 120000.0)])
    monkeypatch.setattr("kline.api.get_store", lambda: store)
    monkeypatch.setattr("kline.api.get_adapter_for_source", lambda *_args: upstream)

    response = await _get_candles(
        asset_class=AssetClass.CRYPTO,
        ticker="BTC",
        timeframe=Timeframe.MIN_1,
        start=None,
        end=None,
        limit=1,
        refresh=False,
        source="binance_spot_public",
        profile="realtime",
        mode="research",
    )

    assert upstream.called is True
    assert response.served_from == "upstream"
    assert response.source_mode == "binance_spot_public"
    assert response.cache_policy == CachePolicy.BYPASS
    assert response.quality_policy == QualityPolicy.STRICT
    assert response.require_execution_venue is False
    assert response.execution_venue is False
    assert response.candles[0].close == 120000.0


async def test_explicit_fallback_is_visible_and_source_scoped(monkeypatch, store: KlineStore):
    primary = FakeAdapter(error=ProviderError("primary unavailable"))
    fallback = FakeAdapter([_candle("2026-07-15T10:00:00", 3333.0)])

    def adapter_for_source(source, _asset_class):
        return primary if source == "binance_usdm_futures" else fallback

    monkeypatch.setattr("kline.api.get_store", lambda: store)
    monkeypatch.setattr("kline.api.get_adapter_for_source", adapter_for_source)

    response = await _get_candles(
        asset_class=AssetClass.COMMODITY,
        ticker="GOLD",
        source="binance_usdm_futures",
        cache_policy=CachePolicy.BYPASS,
        fallback_policy=FallbackPolicy.EXPLICIT,
        fallback_sources=["yahoo_finance_futures"],
    )

    assert response.selected_source == "yahoo_finance_futures"
    assert response.selection_reason == "explicit_fallback"
    assert response.attempted_sources == [
        "binance_usdm_futures",
        "yahoo_finance_futures",
    ]
    assert response.candles[0].close == 3333.0
    assert response.instrument_id == "GOLD"
    assert response.provider_symbol == "GC=F"
    assert any("primary unavailable" in issue for issue in response.access_issues)
    # bypass is read-only: explicit fallback remains visible in the envelope,
    # but neither attempted source writes cache rows.
    assert store.count(
        "GC=F",
        AssetClass.COMMODITY,
        Timeframe.MIN_1,
        source_id="yahoo_finance_futures",
    ) == 0
    assert store.count(
        "XAUUSDT",
        AssetClass.COMMODITY,
        Timeframe.MIN_1,
        source_id="binance_usdm_futures",
    ) == 0


async def test_explicit_fallback_requires_named_sources(store: KlineStore):
    with pytest.raises(HTTPException) as exc:
        await _get_candles(
            asset_class=AssetClass.COMMODITY,
            ticker="GOLD",
            source="binance_usdm_futures",
            fallback_policy=FallbackPolicy.EXPLICIT,
            fallback_sources=[],
        )

    assert exc.value.status_code == 400
    assert exc.value.detail["error"] == "invalid_policy"
    assert "requires at least one" in exc.value.detail["detail"]


async def test_require_execution_venue_rejects_non_execution_source(store: KlineStore):
    with pytest.raises(HTTPException) as exc:
        await _get_candles(
            asset_class=AssetClass.CRYPTO,
            ticker="BTC",
            timeframe=Timeframe.MIN_1,
            start=None,
            end=None,
            limit=1,
            refresh=False,
            source="binance_spot_public",
            require_execution_venue=True,
            mode="research",
        )

    assert exc.value.status_code == 400
    assert exc.value.detail["error"] == "execution_venue_required"
    assert exc.value.detail["reject_reason"] == "not_execution_venue"
