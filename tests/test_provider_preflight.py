from datetime import datetime, timedelta, timezone
import json

from ops.provider_preflight import (
    DEFAULT_TARGETS,
    Bar,
    PolicyReceipt,
    PreflightTarget,
    classify_status,
    derive_series,
    idempotency_check,
    parse_eastmoney_payload,
    parse_yahoo_chart_payload,
    run_preflight,
    HttpObservation,
)


def _yahoo_payload() -> dict:
    return {
        "chart": {
            "result": [
                {
                    "meta": {"exchangeTimezoneName": "America/New_York", "symbol": "AAPL"},
                    "timestamp": [
                        1788183000,  # 2026-08-31 09:30 America/New_York
                        1788183900,  # 09:45
                        1788184800,  # 10:00
                    ],
                    "indicators": {
                        "quote": [
                            {
                                "open": [100.0, 101.0, 102.0],
                                "high": [101.0, 102.0, 101.0],  # third row is invalid
                                "low": [99.0, 100.0, 101.0],
                                "close": [100.5, 101.5, 102.0],
                                "volume": [1000, 1100, 1200],
                            }
                        ]
                    },
                }
            ],
            "error": None,
        }
    }


def test_yahoo_parser_keeps_closed_rows_and_records_invalid_ohlc() -> None:
    parsed = parse_yahoo_chart_payload(
        _yahoo_payload(),
        provider_symbol="AAPL",
        timeframe="15m",
        calendar_id="us_equities",
        timezone_name="America/New_York",
        now=datetime(2026, 8, 31, 15, 0, tzinfo=timezone.utc),
    )

    assert len(parsed.bars) == 2
    assert parsed.bars[0].timestamp.endswith("13:30:00+00:00")
    assert parsed.quality.invalid_rows == 1
    assert parsed.quality.status == "partial"


def test_eastmoney_parser_normalizes_traded_volume_and_identity() -> None:
    payload = {
        "rc": 0,
        "data": {
            "code": "600519",
            "market": 1,
            "name": "贵州茅台",
            "klines": [
                "2026-08-31 09:30,100,101,102,99,123,456,1,1,1,1",
                "2026-08-31 09:45,101,102,103,100,124,457,1,1,1,1",
            ],
        },
    }
    parsed = parse_eastmoney_payload(
        payload,
        provider_symbol="600519",
        timeframe="15m",
        timezone_name="Asia/Shanghai",
        now=datetime(2026, 8, 31, 8, 0, tzinfo=timezone.utc),
    )

    assert len(parsed.bars) == 2
    assert parsed.bars[0].timestamp.endswith("01:30:00+00:00")
    assert parsed.bars[0].volume == 123.0
    assert parsed.bars[0].amount == 456.0
    assert parsed.quality.status == "ready"


def test_entitlement_unknown_is_partial_but_explicit_false_is_blocked() -> None:
    target = PreflightTarget(
        asset_class="us_stock",
        display_symbol="AAPL",
        provider_symbol="AAPL",
        source_id="yahoo_chart",
        source_kind="yahoo_chart",
        calendar_id="us_equities",
        timezone="America/New_York",
        volume_semantics="traded",
    )
    bars = (Bar("2026-08-31T13:30:00+00:00", 1, 2, 0.5, 1.5, 10, 20),)

    unknown = classify_status(target, "15m", bars, policy=PolicyReceipt())
    assert unknown.status == "partial"
    assert unknown.status_reason == "entitlement_unverified"

    blocked = classify_status(
        target,
        "15m",
        bars,
        policy=PolicyReceipt(persistence_allowed=False),
    )
    assert blocked.status == "blocked"
    assert blocked.status_reason == "persistence_not_allowed"

    ready = classify_status(
        target,
        "15m",
        bars,
        policy=PolicyReceipt(
            persistence_allowed=True,
            derived_allowed=True,
            non_display_allowed=True,
        ),
    )
    assert ready.status == "ready"


def test_derived_permission_only_blocks_derived_cell() -> None:
    target = PreflightTarget(
        asset_class="us_stock",
        display_symbol="AAPL",
        provider_symbol="AAPL",
        source_id="yahoo_chart",
        source_kind="yahoo_chart",
        calendar_id="us_equities",
        timezone="America/New_York",
        volume_semantics="traded",
    )
    bars = (Bar("2026-08-31T13:30:00+00:00", 1, 2, 0.5, 1.5, 10, None),)
    policy = PolicyReceipt(
        persistence_allowed=True,
        derived_allowed=False,
        non_display_allowed=True,
    )

    derived = classify_status(target, "4h", bars, policy=policy, is_derived=True)
    native = classify_status(target, "1h", bars, policy=policy, is_derived=False)

    assert derived.status == "blocked"
    assert derived.status_reason == "derived_not_allowed"
    assert native.status == "ready"


def test_cn_15m_derives_completed_4h_with_transform_receipt() -> None:
    target = PreflightTarget(
        asset_class="a_share",
        display_symbol="600519",
        provider_symbol="600519",
        source_id="eastmoney_kline",
        source_kind="eastmoney",
        calendar_id="cn_a",
        timezone="Asia/Shanghai",
        volume_semantics="traded",
    )
    # Use the canonical session timestamps explicitly: 8 morning + 8 afternoon bars.
    timestamps = [
        datetime(2026, 8, 31, 1, 30, tzinfo=timezone.utc) + timedelta(minutes=15 * i)
        for i in range(8)
    ] + [
        datetime(2026, 8, 31, 5, 0, tzinfo=timezone.utc) + timedelta(minutes=15 * i)
        for i in range(8)
    ]
    bars = tuple(
        Bar(
            timestamp=timestamp.isoformat(),
            open=100 + i,
            high=101 + i,
            low=99 + i,
            close=100.5 + i,
            volume=1000 + i,
            amount=2000 + i,
        )
        for i, timestamp in enumerate(timestamps)
    )
    derived = derive_series(
        target,
        input_timeframe="15m",
        output_timeframe="4h",
        bars=bars,
        now=datetime(2026, 8, 31, 8, 0, tzinfo=timezone.utc),
    )

    assert len(derived.bars) == 1
    assert derived.transform["output_timeframe"] == "4h"
    assert derived.transform["input_timeframe"] == "15m"
    assert derived.bars[0].is_derived is True


def test_idempotency_check_has_zero_duplicate_keys() -> None:
    bars = (
        Bar("2026-08-31T13:30:00+00:00", 1, 2, 0.5, 1.5, 10, None),
        Bar("2026-08-31T13:45:00+00:00", 1.5, 2.5, 1, 2, 11, None),
    )
    result = idempotency_check(
        source_id="yahoo_chart",
        instrument_id="us_stock:AAPL",
        timeframe="15m",
        bars=bars,
    )

    assert result["first_inserted"] == 2
    assert result["second_inserted"] == 0
    assert result["row_count_after_rerun"] == 2
    assert result["duplicate_keys"] == 0


def test_default_preflight_targets_are_unique_and_include_cross_market() -> None:
    keys = [(target.source_id, target.provider_symbol) for target in DEFAULT_TARGETS]
    assert len(DEFAULT_TARGETS) == 9
    assert len(set(keys)) == len(keys)
    assert {target.asset_class for target in DEFAULT_TARGETS} == {
        "a_share",
        "us_stock",
        "index",
        "crypto",
        "commodity",
    }


def test_run_preflight_returns_redacted_receipt_with_derived_cells() -> None:
    target = PreflightTarget(
        asset_class="a_share",
        display_symbol="600519",
        provider_symbol="600519",
        source_id="eastmoney_kline",
        source_kind="eastmoney",
        calendar_id="cn_a",
        timezone="Asia/Shanghai",
        volume_semantics="traded",
        requested_timeframes=("15m", "1d", "4h", "1w"),
    )
    intraday_timestamps = [
        datetime(2026, 8, 31, 9, 30) + timedelta(minutes=15 * i) for i in range(8)
    ] + [datetime(2026, 8, 31, 13, 0) + timedelta(minutes=15 * i) for i in range(8)]
    intraday = [
        f"{timestamp:%Y-%m-%d %H:%M},100,101,102,99,{1000 + i},{2000 + i},1,1,1,1"
        for i, timestamp in enumerate(intraday_timestamps)
    ]
    daily = [f"2026-08-{day:02d},100,101,102,99,1000,2000,1,1,1,1" for day in range(24, 29)]

    def fake_fetcher(url: str, *, params: dict[str, str], timeout: float) -> HttpObservation:
        del url, timeout
        payload = {
            "rc": 0,
            "data": {
                "code": "600519",
                "market": 1,
                "klines": daily if params["klt"] == "101" else intraday,
            },
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return HttpObservation(200, body, payload, 1.5)

    receipt = run_preflight(
        [target],
        now=datetime(2026, 8, 31, 8, 0, tzinfo=timezone.utc),
        fetcher=fake_fetcher,
    )

    assert receipt["read_only"] is True
    assert receipt["database"]["production_database_touched"] is False
    assert receipt["first_gate"]["name"] == "3+3 real end-to-end for 7 days"
    assert receipt["decision_by_asset_class"]["a_share"]["status"] == "partial"
    assert receipt["summary"]["cells"] == 4
    assert {cell["timeframe"] for cell in receipt["cells"]} == {"15m", "1d", "4h", "1w"}
    assert all(cell["status"] == "partial" for cell in receipt["cells"])
    assert all(
        cell["response"]["response_sha256"]
        for cell in receipt["cells"]
        if cell["timeframe"] in {"15m", "1d"}
    )
    derived = {
        cell["timeframe"]: cell for cell in receipt["cells"] if cell["timeframe"] in {"4h", "1w"}
    }
    assert derived["4h"]["is_derived"] is True
    assert derived["1w"]["is_derived"] is True
    assert all(item["duplicate_keys"] == 0 for item in receipt["idempotency"])
