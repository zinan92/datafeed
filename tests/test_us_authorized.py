from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kline.models import AssetClass, Timeframe
from kline.mvp_manifest import load_manifest
from kline.providers.base import EntitlementBlocked, ProviderError
from kline.providers.us_authorized import AuthorizedUSProvider, USDataEntitlement


MANIFEST_PATH = Path(__file__).parents[1] / "configs" / "mvp_manifest.json"


def _entitlement(**overrides: object) -> USDataEntitlement:
    values: dict[str, object] = {
        "allowed_timeframes": ("15m", "4h", "1d", "1w"),
        "persistence_allowed": True,
        "derived_allowed": True,
        "non_display_allowed": True,
        "corporate_actions_allowed": True,
        "evidence_ref": "operator://us/receipt-1",
        "receipt_hash": "a" * 64,
        "allowed_history": {"daily_years": 5, "minute_days": 60},
    }
    values.update(overrides)
    return USDataEntitlement(**values)


class FakeUSClient:
    def __init__(self, bars: list[dict[str, object]] | None = None) -> None:
        self.bars = bars or []
        self.calls: list[dict[str, object]] = []
        self.actions_calls: list[dict[str, object]] = []

    def fetch_bars(
        self,
        provider_symbol: str,
        timeframe: str,
        *,
        start: str | None,
        end: str | None,
        limit: int,
    ) -> list[dict[str, object]]:
        self.calls.append(
            {
                "provider_symbol": provider_symbol,
                "timeframe": timeframe,
                "start": start,
                "end": end,
                "limit": limit,
            }
        )
        return self.bars

    def fetch_corporate_actions(
        self,
        provider_symbol: str,
        *,
        start: str | None,
        end: str | None,
    ) -> list[dict[str, object]]:
        self.actions_calls.append({"provider_symbol": provider_symbol, "start": start, "end": end})
        return [{"type": "split", "ratio": "2:1"}]


def _bar(timestamp: str, *, close: float = 101.0, adjusted: bool = False) -> dict[str, object]:
    return {
        "timestamp": timestamp,
        "open": 100.0,
        "high": max(close, 102.0),
        "low": 99.0,
        "close": close,
        "volume": 1000.0,
        "adjusted": adjusted,
    }


def _provider(
    client: FakeUSClient,
    *,
    clock: datetime = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc),
    entitlement: USDataEntitlement | None = None,
    max_retries: int = 2,
) -> AuthorizedUSProvider:
    return AuthorizedUSProvider(
        "secret-us-token",
        entitlement=entitlement or _entitlement(),
        client=client,
        manifest=MANIFEST_PATH,
        clock=lambda: clock,
        max_retries=max_retries,
    )


@pytest.mark.asyncio
async def test_us_provider_maps_exact_100_and_blocks_without_entitlement() -> None:
    manifest = load_manifest(MANIFEST_PATH)
    provider = AuthorizedUSProvider(manifest=manifest)

    assert provider.membership_report() == {
        "manifest_version": "mvp_universe_v1",
        "expected_members": 100,
        "mapped_members": 100,
        "complete": True,
        "source_id": "us_authorized_pending",
    }
    assert provider.supported_timeframes() == []
    with pytest.raises(EntitlementBlocked, match="blocked_for_entitlement"):
        await provider.fetch("AAPL", Timeframe.DAY)


@pytest.mark.asyncio
async def test_us_provider_keeps_raw_identity_share_class_and_venue_metadata() -> None:
    client = FakeUSClient([_bar("2026-08-31T13:30:00Z")])
    provider = _provider(client)

    candles = await provider.fetch("BRK.B", Timeframe.MIN_15, start="2026-08-31", end="2026-09-01")

    assert len(candles) == 1
    assert client.calls[0]["provider_symbol"] == "BRK.B"
    assert provider.source_identity["provider_symbol"] == "BRK.B"
    assert provider.source_identity["security_type"] == "common_stock"
    assert provider.source_identity["adjustment_basis"] == "raw_unadjusted"
    assert "secret-us-token" not in repr(provider.last_raw_response)

    tsm_client = FakeUSClient([_bar("2026-08-31")])
    tsm = _provider(tsm_client)
    await tsm.fetch("TSM", Timeframe.DAY)
    assert tsm.source_identity["adr_ratio"] == "1:5"


@pytest.mark.asyncio
async def test_us_provider_resolves_historical_alias_and_rejects_invalid_current_alias() -> None:
    client = FakeUSClient([_bar("2021-06-01")])
    provider = _provider(client)
    await provider.fetch("FB", Timeframe.DAY, start="2021-01-01", end="2021-12-31")
    assert provider.source_identity["instrument_id"] == "US.EQ.META"
    assert provider.source_identity["alias_used"] == "FB"
    await provider.fetch("fb", Timeframe.DAY, start="2021-01-01", end="2021-12-31")
    assert provider.source_identity["alias_used"] == "fb"

    with pytest.raises(ProviderError, match="not valid"):
        await provider.fetch("FB", Timeframe.DAY, start="2023-01-01", end="2023-12-31")


@pytest.mark.asyncio
async def test_us_provider_aggregates_15m_to_4h_and_daily_to_weekly() -> None:
    zone = timezone(timedelta(hours=-4))
    start = datetime(2026, 8, 31, 9, 30, tzinfo=zone)
    bars = [
        _bar((start + timedelta(minutes=15 * index)).isoformat(), close=100 + index)
        for index in range(16)
    ]
    client = FakeUSClient(bars)
    provider = _provider(client, clock=datetime(2026, 9, 1, tzinfo=timezone.utc))
    derived = await provider.fetch("AAPL", Timeframe.HOUR_4)
    assert len(derived) == 1
    assert provider.timeframe_transform is not None
    assert provider.timeframe_transform.timeframe_origin == "aggregated"

    daily_client = FakeUSClient([_bar(f"2026-08-{24 + index:02d}") for index in range(5)])
    weekly = _provider(daily_client, clock=datetime(2026, 8, 29, 12, tzinfo=timezone.utc))
    weekly_rows = await weekly.fetch("AAPL", Timeframe.WEEK)
    assert len(weekly_rows) == 1
    assert weekly.timeframe_transform is not None
    assert weekly.timeframe_transform.raw_timeframe == Timeframe.DAY


@pytest.mark.asyncio
async def test_us_provider_keeps_corporate_actions_separate_and_rejects_adjusted_rows() -> None:
    client = FakeUSClient([_bar("2026-08-31", adjusted=True)])
    provider = _provider(client)
    with pytest.raises(ProviderError, match="malformed"):
        await provider.fetch("AAPL", Timeframe.DAY)

    actions = await _provider(FakeUSClient()).fetch_corporate_actions("AAPL")
    assert actions == [{"type": "split", "ratio": "2:1"}]

    blocked = _provider(FakeUSClient(), entitlement=_entitlement(corporate_actions_allowed=False))
    with pytest.raises(EntitlementBlocked):
        await blocked.fetch_corporate_actions("AAPL")


@pytest.mark.asyncio
async def test_us_provider_retries_rate_limit_and_reports_empty_windows() -> None:
    class FlakyClient(FakeUSClient):
        def __init__(self) -> None:
            super().__init__([_bar("2026-08-31")])
            self.attempts = 0

        def fetch_bars(self, *args: object, **kwargs: object) -> list[dict[str, object]]:
            self.attempts += 1
            if self.attempts < 3:
                raise RuntimeError("429 rate limit")
            return super().fetch_bars(*args, **kwargs)  # type: ignore[arg-type]

    flaky = FlakyClient()
    assert len(await _provider(flaky).fetch("AAPL", Timeframe.DAY)) == 1
    assert flaky.attempts == 3

    with pytest.raises(ProviderError, match="no closed rows"):
        await _provider(FakeUSClient()).fetch("AAPL", Timeframe.DAY)


def test_us_entitlement_receipt_is_explicit() -> None:
    receipt = _entitlement().as_receipt()
    assert receipt.source_id == "us_authorized_pending"
    assert receipt.persistence_allowed is True
    assert receipt.derived_allowed is True
    assert "secret-us-token" not in repr(receipt)


@pytest.mark.asyncio
async def test_registry_exposes_pending_us_source_without_marking_it_ready(tmp_path: Path) -> None:
    from kline.config import Settings
    from kline.registry import get_adapter_for_source, init

    init(Settings(db_path=str(tmp_path / "pending-us.db"), load_entrypoint_adapters=False))
    adapter = get_adapter_for_source("us_authorized_pending", AssetClass.US_STOCK)
    with pytest.raises(EntitlementBlocked) as error:
        await adapter.fetch_candles("AAPL", Timeframe.DAY, limit=1)
    assert error.value.code == "blocked_for_entitlement"
