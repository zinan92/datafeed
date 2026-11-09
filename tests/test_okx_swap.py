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


def _public_handler(instrument: dict | None = None, tiers: list | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v5/public/instruments":
            assert request.url.params["instType"] == "SWAP"
            return _ok([instrument or _instrument()])
        if request.url.path == "/api/v5/public/position-tiers":
            assert request.url.params["instFamily"] == "XAU-USDT"
            assert request.url.params["tdMode"] == "cross"
            return _ok(tiers if tiers is not None else [
                {"tier": "1", "mmr": "0.004", "imr": "0.01", "maxLever": "100"},
                {"tier": "2", "mmr": "0.005", "imr": "0.015", "maxLever": "66.66"},
            ])
        if request.url.path == "/api/v5/public/time":
            return _ok([{"ts": "1793828110746"}])
        raise AssertionError(f"unexpected request {request.url.path}")

    return handler


@pytest.mark.asyncio
async def test_instrument_definition_converts_contracts_to_base_units() -> None:
    provider = OkxSwapProvider(transport=httpx.MockTransport(_public_handler()))
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
    assert definition.margin_maint_rate == "0.004"  # tier 1, read from upstream
    assert definition.upstream_server_time == 1793828110746
    assert definition.execution_venue is True
    assert definition.source_mode == "okx_swap_public"
    assert definition.derived_fields["contract_multiplier"] == "usd_m_notional_equals_price_times_quantity"
    # Values OKX has but we do not read stay "missing"; limits OKX does not have
    # are "not applicable", with a reason, and are null rather than a fake "0".
    assert definition.missing_fields == ["maker_fee_rate", "taker_fee_rate"]
    assert definition.min_notional is None and definition.max_price is None
    assert set(definition.not_applicable_fields) == {"min_notional", "max_price"}


@pytest.mark.asyncio
async def test_missing_position_tier_fails_visibly() -> None:
    provider = OkxSwapProvider(transport=httpx.MockTransport(_public_handler(tiers=[])))
    with pytest.raises(ProviderError, match="tier 1"):
        await provider.fetch_instrument_definition("GOLD")


@pytest.mark.asyncio
async def test_inverse_contracts_are_refused() -> None:
    provider = OkxSwapProvider(transport=httpx.MockTransport(_public_handler({**_instrument(), "ctType": "inverse"})))
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


@pytest.mark.asyncio
async def test_requests_reuse_one_kept_alive_client_per_event_loop() -> None:
    """A new TLS handshake per request (~1.3s from this Mac) misses the Dashboard's 2s bound."""

    newest_first = [_row(_ms(5, 1, 0), "4037.0"), _row(_ms(5, 0, 30), "4020.0")]
    provider = OkxSwapProvider(transport=httpx.MockTransport(lambda _request: _ok(newest_first)))
    created: list[httpx.AsyncClient] = []
    original = provider._new_client

    def counting() -> httpx.AsyncClient:
        client = original()
        created.append(client)
        return client

    provider._new_client = counting  # type: ignore[method-assign]
    await provider.fetch("GOLD", Timeframe.MIN_30, limit=2)
    await provider.fetch("GOLD", Timeframe.MIN_30, limit=2)

    assert len(created) == 1
    assert not created[0].is_closed
    await provider.aclose()
    assert created[0].is_closed


def test_a_new_event_loop_gets_a_new_client() -> None:
    import asyncio

    newest_first = [_row(_ms(5, 1, 0), "4037.0")]
    provider = OkxSwapProvider(transport=httpx.MockTransport(lambda _request: _ok(newest_first)))
    asyncio.run(provider.fetch("GOLD", Timeframe.MIN_30, limit=1))
    first = provider._shared_client
    asyncio.run(provider.fetch("GOLD", Timeframe.MIN_30, limit=1))
    assert provider._shared_client is not first


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("alias", "inst_id"),
    [("SILVER", "XAG-USDT-SWAP"), ("XAG", "XAG-USDT-SWAP"), ("WTI", "CL-USDT-SWAP"), ("CL", "CL-USDT-SWAP"), ("BRENT", "BZ-USDT-SWAP"), ("BZ-USDT-SWAP", "BZ-USDT-SWAP")],
)
async def test_silver_and_crude_are_enabled_under_the_commodity_source(alias: str, inst_id: str) -> None:
    """datafeed#188: silver and crude come from OKX public data like gold; BTC stays outside this source."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _ok([_row(_ms(5, 1))])

    provider = OkxSwapProvider(transport=httpx.MockTransport(handler))
    candles = await provider.fetch(alias, Timeframe.HOUR_1, limit=1)
    assert len(candles) == 1
    assert seen[0].url.params["instId"] == inst_id
    assert provider.source_identity["provider_symbol"] == inst_id


@pytest.mark.asyncio
async def test_silver_instrument_definition_reads_its_own_contract_and_tiers() -> None:
    silver = {**_instrument(), "instId": "XAG-USDT-SWAP", "ctVal": "0.01", "ctValCcy": "XAG", "tickSz": "0.01", "maxMktSz": "100000"}
    seen_families: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v5/public/instruments":
            return _ok([silver])
        if request.url.path == "/api/v5/public/position-tiers":
            seen_families.append(request.url.params["instFamily"])
            return _ok([{"tier": "1", "mmr": "0.01", "imr": "0.02", "maxLever": "50"}])
        if request.url.path == "/api/v5/public/time":
            return _ok([{"ts": "1793828110746"}])
        raise AssertionError(f"unexpected request {request.url.path}")

    provider = OkxSwapProvider(transport=httpx.MockTransport(handler))
    definition = await provider.fetch_instrument_definition("SILVER")
    assert definition.instrument_id == "XAG-USDT-SWAP.OKX"
    assert definition.asset_class == AssetClass.COMMODITY
    assert definition.price_increment == "0.01"
    assert definition.size_increment == "0.01"  # 1 contract × 0.01 XAG
    assert definition.margin_maint_rate == "0.01"
    assert seen_families == ["XAG-USDT"]
    assert definition.missing_fields == ["maker_fee_rate", "taker_fee_rate"]  # fees stay unverified, never a guess


@pytest.mark.asyncio
@pytest.mark.parametrize(("ticker", "symbol", "base"), [("WTI", "CL-USDT-SWAP", "CL"), ("BRENT", "BZ-USDT-SWAP", "BZ")])
async def test_crude_is_a_commodity_not_a_crypto_asset(ticker: str, symbol: str, base: str) -> None:
    row = {**_instrument(), "instId": symbol, "ctVal": "0.1", "ctValCcy": base, "tickSz": "0.01"}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v5/public/instruments":
            return _ok([row])
        if request.url.path == "/api/v5/public/position-tiers":
            return _ok([{"tier": "1", "mmr": "0.01", "imr": "0.02", "maxLever": "50"}])
        if request.url.path == "/api/v5/public/time":
            return _ok([{"ts": "1793828110746"}])
        raise AssertionError(f"unexpected request {request.url.path}")

    definition = await OkxSwapProvider(transport=httpx.MockTransport(handler)).fetch_instrument_definition(ticker)
    assert definition.asset_class == AssetClass.COMMODITY


def test_the_commodity_manifest_names_silver_and_crude() -> None:
    manifest = source_manifest("okx_swap_public", AssetClass.COMMODITY)
    assert manifest.ticker_aliases["SILVER"] == "XAG-USDT-SWAP"
    assert manifest.ticker_aliases["WTI"] == "CL-USDT-SWAP"
    assert manifest.ticker_aliases["BRENT"] == "BZ-USDT-SWAP"
    assert manifest.canonical_instrument_ids["XAG-USDT-SWAP"] == "SILVER"
    assert "BTC" not in manifest.ticker_aliases


@pytest.mark.asyncio
async def test_instrument_definition_reads_its_three_public_calls_at_once() -> None:
    """Instrument, position tiers and server time are independent: one round trip, not three (#210)."""
    import asyncio
    import time

    sync = _public_handler()
    in_flight = peak = 0

    async def slow(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.2)
        in_flight -= 1
        return sync(request)

    provider = OkxSwapProvider(transport=httpx.MockTransport(slow))
    began = time.monotonic()
    definition = await provider.fetch_instrument_definition("GOLD")
    took = time.monotonic() - began
    reference = await OkxSwapProvider(transport=httpx.MockTransport(sync)).fetch_instrument_definition("GOLD")
    assert peak == 3
    assert took < 0.45, took  # three calls one after another would take at least 0.6 s
    assert definition.model_dump(exclude={"observed_at"}) == reference.model_dump(exclude={"observed_at"})  # observed_at is the local read time
