from __future__ import annotations

from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd
import pytest

from kline.models import AssetClass, Timeframe
from kline.mvp_manifest import load_manifest
from kline.providers.base import EntitlementBlocked, ProviderError
from kline.providers.tushare_mvp import TuShareEntitlement, TuShareMvpProvider


MANIFEST_PATH = Path(__file__).parents[1] / "configs" / "mvp_manifest.json"


def _entitlement(**overrides: object) -> TuShareEntitlement:
    values: dict[str, object] = {
        "allowed_timeframes": ("15m", "4h", "1d", "1w"),
        "persistence_allowed": True,
        "derived_allowed": True,
        "non_display_allowed": True,
        "evidence_ref": "operator://tushare/receipt-1",
        "receipt_hash": "a" * 64,
        "allowed_history": {"daily_years": 5, "minute_days": 60},
    }
    values.update(overrides)
    return TuShareEntitlement(**values)


class FakeTuShareClient:
    def __init__(
        self, *, daily: pd.DataFrame | None = None, mins: pd.DataFrame | None = None
    ) -> None:
        self.daily_frame = daily if daily is not None else pd.DataFrame()
        self.mins_frame = mins if mins is not None else pd.DataFrame()
        self.daily_calls: list[dict[str, object]] = []
        self.minute_calls: list[dict[str, object]] = []

    def daily(self, **kwargs: object) -> pd.DataFrame:
        self.daily_calls.append(kwargs)
        return self.daily_frame

    def index_daily(self, **kwargs: object) -> pd.DataFrame:
        self.daily_calls.append(kwargs)
        return self.daily_frame

    def stk_mins(self, **kwargs: object) -> pd.DataFrame:
        self.minute_calls.append(kwargs)
        return self.mins_frame


def _daily_frame(*dates: str) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "trade_date": trading_date.replace("-", ""),
                "open": 100 + index,
                "high": 102 + index,
                "low": 99 + index,
                "close": 101 + index,
                "vol": 1000 + index,
                "amount": 100000 + index,
            }
            for index, trading_date in enumerate(dates)
        ]
    )


def _minute_frame(*times: str) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "trade_time": stamp,
                "open": 100 + index,
                "high": 102 + index,
                "low": 99 + index,
                "close": 101 + index,
                "vol": 1000 + index,
                "amount": 100000 + index,
            }
            for index, stamp in enumerate(times)
        ]
    )


def _provider(
    client: FakeTuShareClient,
    *,
    clock: datetime = datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc),
    entitlement: TuShareEntitlement | None = None,
    max_retries: int = 2,
) -> TuShareMvpProvider:
    return TuShareMvpProvider(
        "secret-token",
        entitlement=entitlement or _entitlement(),
        client=client,
        manifest=MANIFEST_PATH,
        clock=lambda: clock,
        max_retries=max_retries,
    )


@pytest.mark.asyncio
async def test_mvp_provider_maps_exact_100_members_and_is_blocked_without_entitlement() -> None:
    manifest = load_manifest(MANIFEST_PATH)
    provider = TuShareMvpProvider(manifest=manifest)

    assert provider.membership_report() == {
        "manifest_version": "mvp_universe_v1",
        "expected_members": 100,
        "mapped_members": 100,
        "complete": True,
        "source_id": "tushare_pro",
    }
    assert provider.supported_timeframes() == []
    with pytest.raises(EntitlementBlocked, match="blocked_for_entitlement") as error:
        await provider.fetch("300308", Timeframe.DAY)
    assert error.value.code == "blocked_for_entitlement"


def test_expired_or_partial_entitlement_reports_no_persistable_timeframes() -> None:
    expired = TuShareMvpProvider(
        "secret-token",
        entitlement=_entitlement(valid_to="2026-08-01"),
        manifest=MANIFEST_PATH,
        clock=lambda: datetime(2026, 9, 1, tzinfo=timezone.utc),
    )
    assert expired.supported_timeframes() == []
    partial = TuShareEntitlement(
        allowed_timeframes=("1d",),
        persistence_allowed=True,
        derived_allowed=False,
        non_display_allowed=True,
        evidence_ref="operator://tushare/receipt-2",
        receipt_hash="b" * 64,
    )
    assert TuShareMvpProvider("secret-token", entitlement=partial).supported_timeframes() == [
        Timeframe.DAY
    ]


@pytest.mark.asyncio
async def test_mvp_provider_fetches_daily_with_source_identity_and_no_token_leak() -> None:
    client = FakeTuShareClient(daily=_daily_frame("2026-08-31"))
    provider = _provider(client)

    candles = await provider.fetch("300308", Timeframe.DAY, start="2026-08-01", end="2026-09-01")

    assert len(candles) == 1
    assert client.daily_calls == [
        {"ts_code": "300308.SZ", "start_date": "2026-08-01", "end_date": "2026-09-01"}
    ]
    assert provider.source_identity["provider_symbol"] == "300308.SZ"
    assert provider.source_identity["instrument_id"] == "CN.A.300308"
    assert provider.last_raw_response is not None
    assert provider.last_raw_response["response_body"]["row_count"] == 1
    assert "secret-token" not in repr(provider.last_raw_response)


@pytest.mark.asyncio
async def test_mvp_provider_fetches_15m_and_aggregates_4h_without_30m() -> None:
    local_day = datetime(2026, 8, 31, 9, 30)
    local_times = [
        *[
            (local_day + timedelta(minutes=15 * index)).strftime("%Y-%m-%d %H:%M:%S")
            for index in range(8)
        ],
        *[
            (datetime(2026, 8, 31, 13, 0) + timedelta(minutes=15 * index)).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            for index in range(8)
        ],
    ]
    client = FakeTuShareClient(mins=_minute_frame(*local_times))
    provider = _provider(client, clock=datetime(2026, 9, 1, tzinfo=timezone.utc))

    native = await provider.fetch("300308", Timeframe.MIN_15, limit=100)
    derived = await provider.fetch("300308", Timeframe.HOUR_4, limit=10)

    assert len(native) == 16
    assert len(derived) == 1
    assert provider.timeframe_transform is not None
    assert provider.timeframe_transform.timeframe_origin == "aggregated"
    assert provider.timeframe_transform.aggregation["partial_bucket_count"] == 0
    assert "30m" not in [timeframe.value for timeframe in provider.supported_timeframes()]


@pytest.mark.asyncio
async def test_mvp_provider_weekly_transform_and_new_listing_exception() -> None:
    client = FakeTuShareClient(
        daily=_daily_frame("2026-08-24", "2026-08-25", "2026-08-26", "2026-08-27", "2026-08-28")
    )
    provider = _provider(client, clock=datetime(2026, 8, 29, 12, tzinfo=timezone.utc))

    candles = await provider.fetch("688825", Timeframe.WEEK, limit=10)

    assert len(candles) == 1
    assert provider.source_identity["history_status"] == "new_listing_exception"
    assert provider.timeframe_transform is not None
    assert provider.timeframe_transform.raw_timeframe == Timeframe.DAY


@pytest.mark.asyncio
async def test_mvp_provider_retries_rate_limit_and_rejects_malformed_or_empty_rows() -> None:
    class FlakyClient(FakeTuShareClient):
        def __init__(self) -> None:
            super().__init__(daily=_daily_frame("2026-08-31"))
            self.attempts = 0

        def daily(self, **kwargs: object) -> pd.DataFrame:
            self.attempts += 1
            if self.attempts < 3:
                raise RuntimeError("429 rate limit")
            return super().daily(**kwargs)

    flaky = FlakyClient()
    provider = _provider(flaky)
    assert len(await provider.fetch("300308", Timeframe.DAY)) == 1
    assert flaky.attempts == 3
    assert provider.last_raw_response is not None

    malformed = FakeTuShareClient(daily=pd.DataFrame([{"trade_date": "20260831", "open": "bad"}]))
    with pytest.raises(ProviderError, match="malformed") as malformed_error:
        await _provider(malformed).fetch("300308", Timeframe.DAY)
    assert malformed_error.value.code == "malformed_row"

    empty = FakeTuShareClient(daily=pd.DataFrame())
    with pytest.raises(ProviderError, match="no closed rows"):
        await _provider(empty).fetch("300308", Timeframe.DAY)


def test_entitlement_receipt_is_explicit_and_token_free() -> None:
    receipt = _entitlement().as_receipt()
    assert receipt.source_id == "tushare_pro"
    assert receipt.persistence_allowed is True
    assert receipt.non_display_allowed is True
    assert "secret-token" not in repr(receipt)


@pytest.mark.asyncio
async def test_registry_exposes_typed_blocked_a_share_adapter_without_token(tmp_path: Path) -> None:
    from kline.config import Settings
    from kline.registry import get_adapter_for_source, init

    init(
        Settings(
            db_path=str(tmp_path / "blocked.db"), tushare_token="", load_entrypoint_adapters=False
        )
    )
    adapter = get_adapter_for_source("tushare_pro", AssetClass.A_SHARE)
    with pytest.raises(EntitlementBlocked) as error:
        await adapter.fetch_candles("300308", Timeframe.DAY, limit=1)
    assert error.value.code == "blocked_for_entitlement"
