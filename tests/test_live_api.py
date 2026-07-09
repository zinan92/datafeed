"""Tests for strict/live REST behavior."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException

from kline.api import get_candles
from kline.models import AssetClass, CachePolicy, Candle, FallbackPolicy, QualityPolicy, Timeframe
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
    store.save("BTC", AssetClass.CRYPTO, Timeframe.MIN_1, cached)
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
