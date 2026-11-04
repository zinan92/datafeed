"""Tests for the OKX public swap provider (second execution-live gold source)."""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest

from kline.models import AssetClass, Timeframe
from kline.providers.base import ProviderError
from kline.providers.okx_swap import OkxSwapProvider
from kline.provenance import normalize_source, source_manifest, source_meta

STEP_30M = 30 * 60 * 1000


def _ms(day: int, hour: int, minute: int = 0) -> int:
    return int(datetime(2026, 11, day, hour, minute, tzinfo=timezone.utc).timestamp() * 1000)


def _row(ts: int, close: str = "4031.0", confirm: str = "1") -> list[str]:
    # [ts, o, h, l, c, vol(contracts), volCcy(base), volCcyQuote, confirm]
    return [str(ts), "4024.3", "4037.5", "4020.1", close, "96446", "96.446", "389215.69", confirm]


def _ok(data: list) -> httpx.Response:
    return httpx.Response(200, json={"code": "0", "msg": "", "data": data})


def _instrument() -> dict:
    return {
        "instId": "XAU-USDT-SWAP",
        "instType": "SWAP",
        "ctType": "linear",
        "ctVal": "0.001",
        "ctMult": "1",
        "ctValCcy": "XAU",
        "settleCcy": "USDT",
        "lotSz": "1",
        "minSz": "1",
        "maxLmtSz": "100000000",
        "maxMktSz": "640000",
        "tickSz": "0.1",
        "lever": "100",
        "state": "live",
    }


@pytest.mark.asyncio
async def test_recent_candles_are_reversed_to_oldest_first_and_use_base_volume() -> None:
    newest_first = [_row(_ms(5, 1, 30), "4031.0", "0"), _row(_ms(5, 1, 0), "4037.0"), _row(_ms(5, 0, 30), "4020.0")]
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _ok(newest_first)

    provider = OkxSwapProvider(transport=httpx.MockTransport(handler))
    candles = await provider.fetch("GOLD", Timeframe.MIN_30, limit=3)

    assert [c.timestamp for c in candles] == ["2026-11-05T00:30:00+00:00", "2026-11-05T01:00:00+00:00", "2026-11-05T01:30:00+00:00"]
    assert candles[-1].close == 4031.0
    assert candles[0].volume == 96.446  # volCcy (XAU), not contracts
    assert candles[0].amount == 389215.69
    assert seen[0].url.path == "/api/v5/market/candles"
    assert seen[0].url.params["instId"] == "XAU-USDT-SWAP"
    assert seen[0].url.params["bar"] == "30m"
    assert provider.source_identity["provider_symbol"] == "XAU-USDT-SWAP"


@pytest.mark.asyncio
async def test_daily_bars_request_utc_days_not_hong_kong_days() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["bar"] == "1Dutc"
        return _ok([_row(_ms(4, 0))])

    provider = OkxSwapProvider(transport=httpx.MockTransport(handler))
    candles = await provider.fetch("XAUUSDT", Timeframe.DAY, limit=1)

    assert candles[0].timestamp == "2026-11-04"


@pytest.mark.asyncio
async def test_start_bound_pages_back_into_history_and_returns_forward_window() -> None:
    latest = _ms(5, 2)
    recent = [_row(latest - i * STEP_30M) for i in range(300)]
    history_oldest = latest - 300 * STEP_30M
    history = [_row(history_oldest - i * STEP_30M) for i in range(100)]
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        after = request.url.params.get("after")
        if request.url.path.endswith("/candles") and after is None:
            return _ok(recent)
        if request.url.path.endswith("/candles"):
            return _ok([])
        return _ok([row for row in history if int(row[0]) < int(after)])

    start_ms = history_oldest - 50 * STEP_30M
    start = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc).isoformat()
    provider = OkxSwapProvider(transport=httpx.MockTransport(handler))
    candles = await provider.fetch("GOLD", Timeframe.MIN_30, start=start, limit=10)

    assert len(candles) == 10
    assert candles[0].timestamp == datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc).isoformat()
    assert "/api/v5/market/history-candles" in calls
    stamps = [c.timestamp for c in candles]
    assert stamps == sorted(stamps)


@pytest.mark.asyncio
async def test_okx_error_code_fails_visibly() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": "51001", "msg": "Instrument ID does not exist", "data": []})

    provider = OkxSwapProvider(transport=httpx.MockTransport(handler))
    with pytest.raises(ProviderError, match="Instrument ID does not exist"):
        await provider.fetch("GOLD", Timeframe.MIN_30, limit=5)


@pytest.mark.asyncio
async def test_http_geo_block_fails_visibly_without_fallback() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(451, json={"msg": "restricted location"})

    provider = OkxSwapProvider(transport=httpx.MockTransport(handler))
    with pytest.raises(ProviderError, match="451"):
        await provider.fetch("GOLD", Timeframe.MIN_30, limit=5)


@pytest.mark.asyncio
async def test_symbols_outside_the_allowlist_are_rejected_before_any_request() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not be called
        raise AssertionError("no request expected")

    provider = OkxSwapProvider(transport=httpx.MockTransport(handler))
    with pytest.raises(ProviderError, match="not enabled"):
        await provider.fetch("BTC", Timeframe.MIN_30, limit=5)


@pytest.mark.asyncio
async def test_instrument_definition_converts_contracts_to_base_units() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v5/public/instruments"
        assert request.url.params["instType"] == "SWAP"
        return _ok([_instrument()])

    provider = OkxSwapProvider(transport=httpx.MockTransport(handler))
    definition = await provider.fetch_instrument_definition("GOLD")

    assert definition.instrument_id == "XAU-USDT-SWAP.OKX"
    assert definition.venue == "OKX"
    assert definition.asset_class == AssetClass.COMMODITY
    assert definition.status == "TRADING"
    assert definition.price_increment == "0.1"
    assert definition.price_precision == 1
    assert definition.size_increment == "0.001"  # 1 contract × 0.001 XAU
    assert definition.min_quantity == "0.001"
    assert definition.market_max_quantity == "640"
    assert definition.margin_init_rate == "0.01"
    assert definition.execution_venue is True
    assert definition.source_mode == "okx_swap_public"
    assert "maker_fee_rate" in definition.missing_fields


@pytest.mark.asyncio
async def test_inverse_contracts_are_refused() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return _ok([{**_instrument(), "ctType": "inverse"}])

    provider = OkxSwapProvider(transport=httpx.MockTransport(handler))
    with pytest.raises(ProviderError, match="linear"):
        await provider.fetch_instrument_definition("GOLD")


def test_okx_is_a_registered_execution_source_with_its_own_identity() -> None:
    assert normalize_source("okx", AssetClass.COMMODITY) == "okx_swap_public"
    meta = source_meta("okx_swap_public", AssetClass.COMMODITY)
    assert meta.execution_venue is True
    assert meta.name == "okx"
    manifest = source_manifest("okx_swap_public", AssetClass.COMMODITY)
    assert manifest.canonical_ticker("GOLD") == "XAU-USDT-SWAP"
    assert manifest.canonical_instrument_id("GOLD") == "GOLD"
    # Binance stays the default for execution_live; OKX is only used when asked for.
    assert source_meta("binance_usdm_futures", AssetClass.COMMODITY).name == "binance_usdm_futures"
