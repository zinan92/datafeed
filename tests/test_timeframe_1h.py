from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from kline.ingestion import IngestionOrchestrator, IngestionPlan
from kline.market_calendar import aggregate_15m_to_1h, assess_quality
from kline.models import Candle, Timeframe, TimeframeTransform
from kline.mvp_manifest import ALLOWED_TIMEFRAMES, load_manifest
from kline.providers.tushare_mvp import TuShareEntitlement, TuShareMvpProvider
from kline.providers.us_authorized import AuthorizedUSProvider, USDataEntitlement
from kline.storage import CandleSeriesKey, MvpCandle
from kline.store import KlineStore
from kline.ports import FetchReceipt


MANIFEST_PATH = Path(__file__).parents[1] / "configs" / "mvp_manifest.json"


def _key(timeframe: str = "15m") -> CandleSeriesKey:
    return CandleSeriesKey(
        instrument_id="CN.A.600519",
        display_symbol="600519",
        provider_symbol="600519.SS",
        source_id="test-source",
        asset_class="a_share",
        timeframe=timeframe,
        adjustment_basis="raw_unadjusted",
        manifest_version="mvp_universe_v1",
    )


def _mvp_bar(key: CandleSeriesKey, stamp: datetime, index: int = 0) -> MvpCandle:
    return MvpCandle(
        key=key,
        timestamp=stamp.isoformat(),
        open=100 + index,
        high=102 + index,
        low=99 + index,
        close=101 + index,
        volume=10 + index,
        amount=1000 + index,
    )


def test_mvp_timeframe_contract_includes_1h_and_excludes_30m() -> None:
    assert ALLOWED_TIMEFRAMES == ("15m", "1h", "4h", "1d", "1w")
    manifest = load_manifest(MANIFEST_PATH)
    for instrument in manifest.instruments:
        states = (
            set(instrument.required_timeframes)
            | set(instrument.not_applicable_timeframes)
            | set(instrument.blocked_timeframes)
        )
        assert states == set(ALLOWED_TIMEFRAMES)
        assert "30m" not in states


def test_storage_accepts_1h_series_key_and_entitlement() -> None:
    key = _key("1h")
    candle = _mvp_bar(key, datetime(2026, 8, 31, 1, 30, tzinfo=timezone.utc))
    store = KlineStore(":memory:")
    from kline.storage import EntitlementReceiptWrite, MvpRunWrite

    receipt = store.commit_mvp_run(
        MvpRunWrite(
            run_id="run-1h",
            manifest_version=key.manifest_version,
            manifest_hash="a" * 64,
            started_at="2026-08-31T00:00:00+00:00",
            window_start="2026-08-30T00:00:00+00:00",
            window_end="2026-08-31T00:00:00+00:00",
            policy={"timeframe": "1h"},
            candles=(candle,),
            entitlement_receipts=(
                EntitlementReceiptWrite(
                    receipt_id="ent-1h",
                    source_id=key.source_id,
                    status="active",
                    allowed_history={"hours": 24},
                    timeframe_permissions=("1h",),
                    persistence_allowed=True,
                    derived_allowed=True,
                    non_display_allowed=True,
                    valid_from="2026-08-01",
                    valid_to=None,
                    evidence_ref="operator://1h",
                    receipt_hash="b" * 64,
                ),
            ),
        )
    )
    assert receipt.candle_count == 1
    assert store.query_mvp_candles(key)[0].key.timeframe == "1h"


def test_cn_15m_to_1h_aggregation_is_calendar_aware_and_auditable() -> None:
    key = _key()
    local = datetime(2026, 8, 31, 9, 30, tzinfo=timezone(timedelta(hours=8)))
    rows = [_mvp_bar(key, local + timedelta(minutes=15 * i), i) for i in range(4)]

    result = aggregate_15m_to_1h(
        rows,
        calendar_id="cn_a",
        cutoff=datetime(2026, 9, 1, tzinfo=timezone.utc),
        run_id="run-1h-transform",
    )

    assert len(result.candles) == 1
    assert result.candles[0].key.timeframe == "1h"
    assert result.candles[0].volume == 46
    assert result.candles[0].is_derived is True
    assert result.transform_receipt is not None
    assert result.transform_receipt.input_timeframe == "15m"
    assert result.transform_receipt.output_timeframe == "1h"
    assert result.transform_receipt.aggregation_rule_version == "cn_a_session_1h_v1"
    assert result.transform_receipt.input_hash
    assert result.transform_receipt.output_hash


def test_quality_accepts_1h_and_detects_same_session_gap() -> None:
    key = _key("1h")
    first = _mvp_bar(key, datetime(2026, 8, 31, 1, 30, tzinfo=timezone.utc))
    second = _mvp_bar(key, datetime(2026, 8, 31, 3, 15, tzinfo=timezone.utc), 1)

    quality = assess_quality(
        [first, second],
        timeframe="1h",
        calendar_id="cn_a",
        cutoff=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )

    assert quality.status == "partial"
    assert any(issue.status == "gap" for issue in quality.issues)


class _FakeTuShareClient:
    def __init__(self) -> None:
        self.minute_calls: list[dict[str, object]] = []

    def stk_mins(self, **kwargs: object) -> pd.DataFrame:
        self.minute_calls.append(kwargs)
        return pd.DataFrame(
            [
                {
                    "trade_time": "2026-08-31 09:30:00",
                    "open": 100,
                    "high": 102,
                    "low": 99,
                    "close": 101,
                    "vol": 1000,
                    "amount": 100000,
                }
            ]
        )


@pytest.mark.asyncio
async def test_tushare_native_1h_requests_60min_and_keeps_identity() -> None:
    client = _FakeTuShareClient()
    entitlement = TuShareEntitlement(
        allowed_timeframes=("15m", "1h", "4h", "1d", "1w"),
        persistence_allowed=True,
        derived_allowed=True,
        non_display_allowed=True,
        evidence_ref="operator://tushare-1h",
        receipt_hash="c" * 64,
    )
    provider = TuShareMvpProvider(
        "secret-token",
        entitlement=entitlement,
        client=client,
        clock=lambda: datetime(2026, 9, 1, 8, tzinfo=timezone.utc),
    )

    candles = await provider.fetch("600519", Timeframe.HOUR_1)

    assert len(candles) == 1
    assert client.minute_calls[0]["freq"] == "60min"
    assert provider.timeframe_transform is not None
    assert provider.timeframe_transform.timeframe_origin == "native"


@pytest.mark.asyncio
async def test_tushare_derives_1h_from_15m_when_native_permission_is_absent() -> None:
    from tests.test_tushare_mvp import FakeTuShareClient, _entitlement, _minute_frame

    local_times = [
        *[
            (datetime(2026, 8, 31, 9, 30) + timedelta(minutes=15 * index)).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
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
    entitlement = _entitlement(
        allowed_timeframes=("15m", "4h", "1d", "1w"),
        derived_allowed=True,
    )
    provider = TuShareMvpProvider(
        "secret-token",
        entitlement=entitlement,
        client=client,
        clock=lambda: datetime(2026, 9, 1, tzinfo=timezone.utc),
    )

    candles = await provider.fetch("600519", Timeframe.HOUR_1)

    assert len(candles) == 4
    assert client.minute_calls[0]["freq"] == "15min"
    assert provider.timeframe_transform is not None
    assert provider.timeframe_transform.raw_timeframe == Timeframe.MIN_15
    assert provider.timeframe_transform.timeframe_origin == "aggregated"


class _FakeUSClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def fetch_bars(
        self, provider_symbol: str, timeframe: str, **kwargs: object
    ) -> list[dict[str, object]]:
        self.calls.append({"provider_symbol": provider_symbol, "timeframe": timeframe, **kwargs})
        return [
            {
                "timestamp": "2026-08-31T13:30:00Z",
                "open": 100,
                "high": 102,
                "low": 99,
                "close": 101,
                "volume": 1000,
            }
        ]


@pytest.mark.asyncio
async def test_authorized_us_accepts_native_1h() -> None:
    client = _FakeUSClient()
    entitlement = USDataEntitlement(
        allowed_timeframes=("15m", "1h", "4h", "1d", "1w"),
        persistence_allowed=True,
        derived_allowed=True,
        non_display_allowed=True,
        evidence_ref="operator://us-1h",
        receipt_hash="d" * 64,
    )
    provider = AuthorizedUSProvider(
        "secret-us-token",
        entitlement=entitlement,
        client=client,
        manifest=MANIFEST_PATH,
        clock=lambda: datetime(2026, 9, 1, 12, tzinfo=timezone.utc),
    )

    candles = await provider.fetch("AAPL", Timeframe.HOUR_1)

    assert len(candles) == 1
    assert client.calls[0]["timeframe"] == "1h"
    assert provider.timeframe_transform == TimeframeTransform(
        raw_timeframe=Timeframe.HOUR_1,
        timeframe_origin="native",
        aggregation={"kind": "none", "rule": "native_passthrough"},
    )


@pytest.mark.asyncio
async def test_authorized_us_derives_1h_from_15m_when_native_permission_is_absent() -> None:
    from tests.test_us_authorized import FakeUSClient, _bar, _entitlement

    zone = timezone(timedelta(hours=-4))
    start = datetime(2026, 8, 31, 9, 30, tzinfo=zone)
    client = FakeUSClient(
        [_bar((start + timedelta(minutes=15 * index)).isoformat()) for index in range(16)]
    )
    entitlement = _entitlement(
        allowed_timeframes=("15m", "4h", "1d", "1w"),
        derived_allowed=True,
    )
    provider = AuthorizedUSProvider(
        "secret-us-token",
        entitlement=entitlement,
        client=client,
        manifest=MANIFEST_PATH,
        clock=lambda: datetime(2026, 9, 1, tzinfo=timezone.utc),
    )

    candles = await provider.fetch("AAPL", Timeframe.HOUR_1)

    assert len(candles) == 4
    assert client.calls[0]["timeframe"] == "15m"
    assert provider.timeframe_transform is not None
    assert provider.timeframe_transform.raw_timeframe == Timeframe.MIN_15
    assert provider.timeframe_transform.timeframe_origin == "aggregated"


@pytest.mark.asyncio
async def test_ingestion_persists_derived_1h_with_transform_receipt(tmp_path: Path) -> None:
    manifest = load_manifest(MANIFEST_PATH)
    store = KlineStore(str(tmp_path / "ingestion-1h.db"))

    class DerivedHourAdapter:
        async def fetch_candles_with_receipt(
            self,
            ticker: str,
            timeframe: Timeframe,
            *,
            start: str | None,
            end: str | None,
            limit: int,
        ) -> FetchReceipt:
            del start, end, limit
            timestamps = {
                Timeframe.MIN_15: "2026-08-31T00:00:00+00:00",
                Timeframe.HOUR_1: "2026-08-31T00:00:00+00:00",
                Timeframe.HOUR_4: "2026-08-31T00:00:00+00:00",
                Timeframe.DAY: "2026-08-31T00:00:00+00:00",
                Timeframe.WEEK: "2026-08-28T00:00:00+00:00",
            }
            candle = Candle(
                timestamp=timestamps[timeframe],
                open=100.0,
                high=102.0,
                low=99.0,
                close=101.0,
                volume=10.0,
            )
            transform = None
            if timeframe == Timeframe.HOUR_1:
                transform = TimeframeTransform(
                    raw_timeframe=Timeframe.MIN_15,
                    timeframe_origin="aggregated",
                    aggregation={
                        "rule": "utc_fixed_1h_v1",
                        "bucket_anchor": "00:00",
                        "partial_bucket_policy": "drop_and_record",
                        "partial_bucket_count": 0,
                    },
                )
            elif timeframe == Timeframe.HOUR_4:
                transform = TimeframeTransform(
                    raw_timeframe=Timeframe.HOUR_1,
                    timeframe_origin="aggregated",
                    aggregation={
                        "rule": "utc_fixed_4h_v1",
                        "bucket_anchor": "00:00",
                        "partial_bucket_policy": "drop_and_record",
                        "partial_bucket_count": 0,
                    },
                )
            elif timeframe == Timeframe.WEEK:
                transform = TimeframeTransform(
                    raw_timeframe=Timeframe.DAY,
                    timeframe_origin="aggregated",
                    aggregation={
                        "rule": "completed_local_calendar_week_v1",
                        "bucket_anchor": "local_week",
                        "partial_bucket_policy": "defer_until_closed",
                        "partial_bucket_count": 0,
                    },
                )
            return FetchReceipt(
                candles=[candle],
                timeframe_transform=transform,
                source_identity={"provider_symbol": ticker},
                raw_response={"row_count": 1, "timeframe": timeframe.value},
            )

    adapter = DerivedHourAdapter()
    receipt = await IngestionOrchestrator(
        store,
        adapter_resolver=lambda instrument: (
            adapter if instrument.instrument_id == "CRYPTO.PERP.BTC" else None
        ),
    ).run_once(
        IngestionPlan(
            manifest=manifest,
            run_id="run-derived-1h",
            now=datetime(2026, 9, 1, 12, tzinfo=timezone.utc),
            history_start="2026-08-31T00:00:00+00:00",
            fetch_limit=10,
        )
    )

    assert receipt.status == "partial"
    assert any(
        cell.instrument_id == "CRYPTO.PERP.BTC"
        and cell.timeframe == "1h"
        and cell.status == "ready"
        for cell in receipt.requested_cells
    )
    key = CandleSeriesKey(
        instrument_id="CRYPTO.PERP.BTC",
        display_symbol="BTC",
        provider_symbol="BTC",
        source_id="hyperliquid_perpetual_public",
        asset_class="crypto",
        timeframe="1h",
        adjustment_basis="raw_unadjusted",
        manifest_version=manifest.version,
    )
    rows = store.query_mvp_candles(key)
    assert len(rows) == 1
    assert rows[0].is_derived is True
    assert rows[0].transform_receipt_id is not None
    assert store.mvp_storage_health()["transform_receipts"] >= 1
