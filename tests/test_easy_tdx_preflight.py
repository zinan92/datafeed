from datetime import datetime, timedelta, timezone

from ops.easy_tdx_preflight import (
    A_SHARE_TARGETS,
    parse_easy_tdx_frame,
    run_easy_tdx_preflight,
)
from ops.provider_preflight import PolicyReceipt, PreflightTarget


class FakeFrame:
    def __init__(self, rows: list[dict]):
        self._rows = rows

    def to_dict(self, *, orient: str) -> list[dict]:
        assert orient == "records"
        return list(self._rows)

    def __len__(self) -> int:
        return len(self._rows)


def _target(*, policy: PolicyReceipt | None = None) -> PreflightTarget:
    return PreflightTarget(
        asset_class="a_share",
        display_symbol="600519",
        provider_symbol="600519",
        source_id="easy_tdx_mac",
        source_kind="easy_tdx",
        calendar_id="cn_a",
        timezone="Asia/Shanghai",
        volume_semantics="traded",
        policy=policy or PolicyReceipt(),
    )


def test_easy_tdx_parser_normalizes_volume_and_drops_forming_rows() -> None:
    frame = FakeFrame(
        [
            {
                "datetime": "2026-08-31 09:30:00",
                "open": 100,
                "high": 101,
                "low": 99,
                "close": 100.5,
                "vol": 1234,
                "amount": 4567,
            },
            {
                "datetime": "2026-08-31 15:00:00",
                "open": 100,
                "high": 101,
                "low": 99,
                "close": 100.5,
                "vol": 1,
                "amount": 2,
            },
        ]
    )

    bars, quality = parse_easy_tdx_frame(
        frame,
        target=_target(),
        timeframe="15m",
        now=datetime(2026, 8, 31, 7, 0, tzinfo=timezone.utc),
    )

    assert len(bars) == 1
    assert bars[0].timestamp == "2026-08-31T01:30:00+00:00"
    assert bars[0].volume == 1234.0
    assert quality.forming_rows == 1
    assert quality.invalid_rows == 0


def test_easy_tdx_preflight_records_native_and_derived_rows() -> None:
    timestamps = [datetime(2026, 8, 31, 9, 30) + timedelta(minutes=15 * i) for i in range(8)] + [
        datetime(2026, 8, 31, 13, 0) + timedelta(minutes=15 * i) for i in range(8)
    ]
    intraday = [
        {
            "datetime": timestamp.strftime("%Y-%m-%d %H:%M:%S"),
            "open": 100 + i,
            "high": 101 + i,
            "low": 99 + i,
            "close": 100.5 + i,
            "vol": 1000 + i,
            "amount": 2000 + i,
        }
        for i, timestamp in enumerate(timestamps)
    ]
    daily = [
        {
            "datetime": "2026-08-18 00:00:00",
            "open": 100,
            "high": 101,
            "low": 99,
            "close": 100.5,
            "vol": 1000,
            "amount": 2000,
        }
    ]

    class FakeClient:
        def get_stock_kline(self, market: int, code: str, *, period, count: int, adjust: int):
            del market, code, count, adjust
            return FakeFrame(intraday if period in {"15m", "1h"} else daily)

    target = _target(
        policy=PolicyReceipt(
            status="active",
            persistence_allowed=True,
            derived_allowed=True,
            non_display_allowed=True,
        )
    )
    receipt = run_easy_tdx_preflight(
        [target],
        client=FakeClient(),
        periods={"15m": "15m", "1h": "1h", "1d": "1d", "1w": "1w"},
        now=datetime(2026, 8, 31, 8, 0, tzinfo=timezone.utc),
        count=64,
    )

    assert receipt["summary"]["cells"] == 5
    assert {cell["timeframe"] for cell in receipt["cells"]} == {"15m", "1h", "4h", "1d", "1w"}
    assert all(cell["status"] == "ready" for cell in receipt["cells"])
    assert (
        next(cell for cell in receipt["cells"] if cell["timeframe"] == "4h")["is_derived"] is True
    )
    assert receipt["decision_by_asset_class"]["a_share"]["status"] == "ready"
    assert receipt["decision_by_asset_class"]["a_share"]["canonical_promotion_allowed"] is True
    assert all(item["duplicate_keys"] == 0 for item in receipt["idempotency"])


def test_default_easy_tdx_targets_are_the_three_a_share_pilot_names() -> None:
    assert [target.display_symbol for target in A_SHARE_TARGETS] == ["600519", "300750", "688981"]


def test_easy_tdx_preflight_keeps_missing_derived_cell_explicitly_blocked() -> None:
    target = _target()

    class EmptyClient:
        def get_stock_kline(self, market: int, code: str, *, period, count: int, adjust: int):
            del market, code, period, count, adjust
            return FakeFrame([])

    receipt = run_easy_tdx_preflight(
        [PreflightTarget(**{**target.__dict__, "requested_timeframes": ("15m", "4h")})],
        client=EmptyClient(),
        periods={"15m": "15m"},
        now=datetime(2026, 8, 31, 8, 0, tzinfo=timezone.utc),
    )

    assert [cell["timeframe"] for cell in receipt["cells"]] == ["15m", "4h"]
    derived = receipt["cells"][1]
    assert derived["status"] == "blocked"
    assert derived["status_reason"] == "no_complete_derived_bars"
