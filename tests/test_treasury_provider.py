"""Focused tests for official Treasury level and spread candles."""

from __future__ import annotations

from datetime import date

import httpx
import pytest

from kline.models import AssetClass, Timeframe
from kline.providers.base import ProviderError
from kline.providers.treasury import TreasuryCsvProvider
from kline.provenance import normalize_source, source_manifest


_CSV = """Date,2 Yr,10 Yr
08/10/2026,4.00,4.50
08/11/2026,4.10,4.60
08/12/2026,4.05,N/A
08/13/2026,4.20,4.70
08/14/2026,4.30,4.80
08/17/2026,4.40,4.90
08/18/2026,4.50,5.00
08/19/2026,4.60,5.10
08/20/2026,4.70,5.20
"""
_HEADER_ONLY = "Date,2 Yr,10 Yr\n"


def _transport(text: str = _CSV) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        year = request.url.path.split("/")[-2]
        body = text if year == "2026" else _HEADER_ONLY
        return httpx.Response(200, text=body, request=request)

    return httpx.MockTransport(handle)


@pytest.mark.asyncio
async def test_treasury_daily_parses_official_two_year_column_and_excludes_today():
    provider = TreasuryCsvProvider(
        transport=_transport(),
        today=lambda: date(2026, 8, 20),
    )

    candles = await provider.fetch(
        "DGS2",
        Timeframe.DAY,
        start="2026-08-10",
        end="2026-08-21",
        limit=20,
    )

    assert [item.timestamp for item in candles] == [
        "2026-08-10",
        "2026-08-11",
        "2026-08-12",
        "2026-08-13",
        "2026-08-14",
        "2026-08-17",
        "2026-08-18",
        "2026-08-19",
    ]
    assert candles[-1].open == candles[-1].high == candles[-1].low == candles[-1].close == 4.60
    assert provider.source_identity["source_id"] == "treasury_official_csv"
    assert provider.source_identity["provider_symbol"] == "2 Yr"
    assert provider.timeframe_transform is not None
    assert provider.timeframe_transform.timeframe_origin == "native"
    assert provider.last_raw_response["response_body"]["responses"]


@pytest.mark.asyncio
async def test_treasury_weekly_keeps_last_completed_level_only():
    provider = TreasuryCsvProvider(
        transport=_transport(),
        today=lambda: date(2026, 8, 20),
    )

    candles = await provider.fetch(
        "US2Y",
        Timeframe.WEEK,
        start="2026-08-10",
        end="2026-08-21",
        limit=10,
    )

    assert [item.timestamp for item in candles] == ["2026-08-14"]
    assert candles[0].open == candles[0].high == candles[0].low == candles[0].close == 4.30
    assert provider.timeframe_transform is not None
    assert provider.timeframe_transform.raw_timeframe == Timeframe.DAY
    assert provider.timeframe_transform.timeframe_origin == "aggregated"
    assert provider.timeframe_transform.aggregation["rule"] == "completed_iso_week_last_level"


@pytest.mark.asyncio
async def test_treasury_spread_uses_same_date_inputs_and_basis_points():
    provider = TreasuryCsvProvider(
        transport=_transport(),
        derived_spread=True,
        today=lambda: date(2026, 8, 20),
    )

    candles = await provider.fetch(
        "T10Y2Y",
        Timeframe.DAY,
        start="2026-08-10",
        end="2026-08-12",
        limit=10,
    )

    assert [item.timestamp for item in candles] == ["2026-08-10", "2026-08-11"]
    assert candles[0].close == pytest.approx(50.0)
    assert candles[1].close == pytest.approx(50.0)
    assert candles[0].open == candles[0].high == candles[0].low == candles[0].close
    assert provider.source_identity["source_id"] == "treasury_official_csv_derived"
    assert provider.source_identity["derivation"]["rule"] == "same_date_10y_minus_2y"
    assert provider.source_identity["derivation"]["input_columns"] == ["10 Yr", "2 Yr"]


@pytest.mark.asyncio
async def test_treasury_missing_same_date_inputs_fail_closed():
    csv = "Date,2 Yr,10 Yr\n08/10/2026,4.00,N/A\n"
    provider = TreasuryCsvProvider(
        transport=_transport(csv),
        derived_spread=True,
        today=lambda: date(2026, 8, 20),
    )

    with pytest.raises(ProviderError, match="required same-date input is missing"):
        await provider.fetch(
            "US2S10S",
            Timeframe.DAY,
            start="2026-08-10",
            end="2026-08-11",
            limit=10,
        )


@pytest.mark.asyncio
async def test_treasury_mixed_valid_and_missing_level_rows_do_not_return_partial_series():
    provider = TreasuryCsvProvider(
        transport=_transport(),
        today=lambda: date(2026, 8, 20),
    )

    with pytest.raises(ProviderError, match="required observation is missing"):
        await provider.fetch(
            "US10Y",
            Timeframe.DAY,
            start="2026-08-10",
            end="2026-08-14",
            limit=10,
        )


@pytest.mark.asyncio
async def test_treasury_malformed_csv_schema_is_not_ready():
    provider = TreasuryCsvProvider(
        transport=_transport("Date,2 Yr\n08/10/2026,4.00\n"),
        today=lambda: date(2026, 8, 20),
    )

    with pytest.raises(ProviderError, match="missing required columns"):
        await provider.fetch(
            "US10Y",
            Timeframe.DAY,
            start="2026-08-10",
            end="2026-08-11",
            limit=10,
        )


@pytest.mark.asyncio
async def test_treasury_malformed_csv_row_with_blank_date_is_not_partial_success():
    provider = TreasuryCsvProvider(
        transport=_transport("Date,2 Yr,10 Yr\n,4.00,4.50\n"),
        today=lambda: date(2026, 8, 20),
    )

    with pytest.raises(ProviderError, match="missing Date"):
        await provider.fetch(
            "DGS2",
            Timeframe.DAY,
            start="2026-08-10",
            end="2026-08-11",
            limit=10,
        )


@pytest.mark.asyncio
async def test_treasury_transport_error_has_explicit_failure():
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("official source unavailable", request=request)

    provider = TreasuryCsvProvider(
        transport=httpx.MockTransport(handle),
        today=lambda: date(2026, 8, 20),
    )

    with pytest.raises(ProviderError, match="Official Treasury CSV request failed"):
        await provider.fetch(
            "DGS2",
            Timeframe.DAY,
            start="2026-08-10",
            end="2026-08-11",
            limit=10,
        )

    assert provider.last_raw_response["error"]


@pytest.mark.asyncio
async def test_treasury_reused_provider_does_not_leak_previous_identity_on_bad_ticker():
    provider = TreasuryCsvProvider(
        transport=_transport(),
        today=lambda: date(2026, 8, 20),
    )
    await provider.fetch(
        "DGS2",
        Timeframe.DAY,
        start="2026-08-10",
        end="2026-08-11",
        limit=10,
    )

    with pytest.raises(ProviderError, match="Unsupported Treasury maturity"):
        await provider.fetch("DGS30", Timeframe.DAY, limit=10)

    assert provider.source_identity == {}
    assert provider.timeframe_transform is None


def test_treasury_manifests_are_explicit_and_timeframe_bound():
    levels = source_manifest("treasury_official_csv", AssetClass.MACRO)
    spread = source_manifest("treasury_official_csv_derived", AssetClass.MACRO)

    assert normalize_source("treasury", AssetClass.MACRO) == "treasury_official_csv"
    assert levels.canonical_ticker("DGS2") == "2 Yr"
    assert levels.canonical_ticker("DGS10") == "10 Yr"
    assert spread.canonical_ticker("T10Y2Y") == "10 Yr-2 Yr"
    assert levels.supports_timeframe("DGS2", Timeframe.DAY)
    assert levels.supports_timeframe("DGS2", Timeframe.WEEK)
    assert not levels.supports_timeframe("DGS2", Timeframe.HOUR_4)
