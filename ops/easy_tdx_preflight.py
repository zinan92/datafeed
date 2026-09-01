"""Optional read-only A-share smoke for easy_tdx's MacClient path.

The dependency is intentionally optional: importing this module does not
install or activate easy_tdx in the production datafeed.  The CLI is used in
an isolated environment to record real source capability and volume evidence;
the resulting receipt is still entitlement-unknown until an operator reviews
the upstream data-use terms.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from hashlib import sha256
import time
from typing import Any, Mapping, Protocol, Sequence
from zoneinfo import ZoneInfo

from ops.provider_preflight import (
    Bar,
    PreflightTarget,
    _decision,
    _decision_by_asset_class,
    _is_closed,
    _quality,
    _stamp,
    classify_status,
    derive_series,
    idempotency_check,
    render_markdown,
)


A_SHARE_TARGETS: tuple[PreflightTarget, ...] = (
    PreflightTarget(
        "a_share",
        "600519",
        "600519",
        "easy_tdx_mac",
        "easy_tdx",
        "cn_a",
        "Asia/Shanghai",
        "traded",
        display_name="贵州茅台",
        volume_scope="venue_reported",
    ),
    PreflightTarget(
        "a_share",
        "300750",
        "300750",
        "easy_tdx_mac",
        "easy_tdx",
        "cn_a",
        "Asia/Shanghai",
        "traded",
        display_name="宁德时代",
        volume_scope="venue_reported",
    ),
    PreflightTarget(
        "a_share",
        "688981",
        "688981",
        "easy_tdx_mac",
        "easy_tdx",
        "cn_a",
        "Asia/Shanghai",
        "traded",
        display_name="中芯国际",
        volume_scope="venue_reported",
    ),
)


class EasyTdxClient(Protocol):
    def get_stock_kline(
        self,
        market: int,
        code: str,
        *,
        period: Any,
        start: int = 0,
        count: int = 800,
        adjust: Any = 0,
    ) -> Any: ...


def _value(row: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in row:
            return row[name]
    return None


def parse_easy_tdx_frame(
    frame: Any,
    *,
    target: PreflightTarget,
    timeframe: str,
    now: datetime,
) -> tuple[tuple[Bar, ...], Any]:
    """Normalize a MacClient DataFrame-like object and return quality facts."""

    try:
        rows = frame.to_dict(orient="records")
    except (AttributeError, TypeError) as exc:
        raise ValueError("easy_tdx response does not expose records") from exc
    if not isinstance(rows, list):
        raise ValueError("easy_tdx records are not a list")
    zone = ZoneInfo(target.timezone)
    bars: list[Bar] = []
    invalid = 0
    forming = 0
    missing_volume = 0
    for row in rows:
        if not isinstance(row, Mapping):
            invalid += 1
            continue
        raw_timestamp = _value(row, "datetime", "time", "date")
        try:
            parsed = datetime.fromisoformat(str(raw_timestamp).replace("Z", "+00:00"))
            local = (
                parsed.replace(tzinfo=zone) if parsed.tzinfo is None else parsed.astimezone(zone)
            )
            timestamp = local.astimezone(timezone.utc)
        except (TypeError, ValueError):
            invalid += 1
            continue
        if not _is_closed(timestamp, timeframe, calendar_id=target.calendar_id, zone=zone, now=now):
            forming += 1
            continue
        try:
            values = tuple(float(_value(row, field)) for field in ("open", "high", "low", "close"))
            volume_raw = _value(row, "vol", "volume")
            amount_raw = _value(row, "amount", "turnover")
            volume = float(volume_raw) if volume_raw is not None else None
            amount = float(amount_raw) if amount_raw is not None else None
        except (TypeError, ValueError):
            invalid += 1
            continue
        if not all(value == value and abs(value) != float("inf") for value in values):
            invalid += 1
            continue
        open_value, high_value, low_value, close_value = values
        if high_value < max(open_value, close_value) or low_value > min(open_value, close_value):
            invalid += 1
            continue
        if volume is None:
            missing_volume += 1
        elif volume < 0 or volume != volume or abs(volume) == float("inf"):
            invalid += 1
            continue
        if amount is not None and (amount < 0 or amount != amount or abs(amount) == float("inf")):
            invalid += 1
            continue
        bars.append(
            Bar(_stamp(timestamp), open_value, high_value, low_value, close_value, volume, amount)
        )
    quality = _quality(
        tuple(bars), invalid_rows=invalid, forming_rows=forming, missing_volume=missing_volume
    )
    return tuple(sorted(bars, key=lambda bar: bar.timestamp)), quality


def _market_code(symbol: str) -> int:
    normalized = symbol.upper().split(".")[0]
    return 1 if normalized.startswith(("6", "68", "9")) else 0


def _periods() -> Mapping[str, Any]:
    try:
        from easy_tdx import Period
    except ImportError as exc:  # pragma: no cover - exercised by CLI environment
        raise RuntimeError(
            "easy_tdx is not installed; use an isolated preflight environment"
        ) from exc
    return {"15m": Period.MIN_15, "1h": Period.MIN_60, "1d": Period.DAILY, "1w": Period.WEEKLY}


def _client() -> EasyTdxClient:
    try:
        from easy_tdx import MacClient
    except ImportError as exc:  # pragma: no cover - exercised by CLI environment
        raise RuntimeError(
            "easy_tdx is not installed; use an isolated preflight environment"
        ) from exc
    return MacClient.from_best_host(timeout=5.0, ping_timeout=2.0)


def _frame_hash(frame: Any) -> str | None:
    try:
        rows = frame.to_dict(orient="records")
    except (AttributeError, TypeError):
        return None
    return sha256(
        json.dumps(rows, ensure_ascii=False, default=str, sort_keys=True).encode()
    ).hexdigest()


def run_easy_tdx_preflight(
    targets: Sequence[PreflightTarget] = A_SHARE_TARGETS,
    *,
    client: EasyTdxClient | None = None,
    now: datetime | None = None,
    count: int = 800,
    periods: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run a bounded real/fake-client preflight for A-share targets."""

    observed_at = now or datetime.now(timezone.utc)
    active_client = client or _client()
    active_periods = periods or _periods()
    cells: list[dict[str, Any]] = []
    idempotency: list[dict[str, Any]] = []
    for target in targets:
        base: dict[str, tuple[tuple[Bar, ...], Any]] = {}
        for timeframe, period in active_periods.items():
            started = time.monotonic()
            request = {
                "provider": "easy_tdx_mac",
                "provider_symbol": target.provider_symbol,
                "market": _market_code(target.provider_symbol),
                "period": timeframe,
                "count": count,
                "adjust": "none",
            }
            try:
                frame = active_client.get_stock_kline(
                    request["market"],
                    target.provider_symbol,
                    period=period,
                    count=count,
                    adjust=0,
                )
                bars, quality = parse_easy_tdx_frame(
                    frame, target=target, timeframe=timeframe, now=observed_at
                )
                base[timeframe] = (bars, quality)
                cell = classify_status(
                    target,
                    timeframe,
                    bars,
                    policy=target.policy,
                    quality=quality,
                )
                response = {
                    "http_status": 200,
                    "latency_ms": round((time.monotonic() - started) * 1000, 2),
                    "response_sha256": _frame_hash(frame),
                    "row_count_raw": len(frame),
                }
                if bars:
                    idempotency.append(
                        {
                            "source_id": target.source_id,
                            "instrument_id": target.instrument_id,
                            "timeframe": timeframe,
                            **idempotency_check(
                                source_id=target.source_id,
                                instrument_id=target.instrument_id,
                                timeframe=timeframe,
                                bars=bars,
                            ),
                        }
                    )
            except Exception as error:  # provider SDK errors become typed cell facts
                cell = classify_status(
                    target,
                    timeframe,
                    (),
                    policy=target.policy,
                    error={"code": "request_failed", "message": str(error)},
                )
                response = {
                    "http_status": 0,
                    "latency_ms": round((time.monotonic() - started) * 1000, 2),
                    "response_sha256": None,
                }
            cells.append(
                cell.as_dict(request=request, response=response, observed_at=_stamp(observed_at))
            )

        for output_timeframe, input_timeframe in (("4h", "15m"),):
            source = base.get(input_timeframe)
            if source is None or not source[0]:
                continue
            derived = derive_series(
                target,
                input_timeframe=input_timeframe,
                output_timeframe=output_timeframe,
                bars=source[0],
                now=observed_at,
            )
            cell = classify_status(
                target,
                output_timeframe,
                derived.bars,
                policy=target.policy,
                quality=derived.quality,
                is_derived=True,
                transform=derived.transform,
            )
            cells.append(cell.as_dict(observed_at=_stamp(observed_at)))
            if derived.bars:
                idempotency.append(
                    {
                        "source_id": target.source_id,
                        "instrument_id": target.instrument_id,
                        "timeframe": output_timeframe,
                        **idempotency_check(
                            source_id=target.source_id,
                            instrument_id=target.instrument_id,
                            timeframe=output_timeframe,
                            bars=derived.bars,
                        ),
                    }
                )

    counts: dict[str, int] = {}
    for cell in cells:
        counts[cell["status"]] = counts.get(cell["status"], 0) + 1
    return {
        "schema_version": "provider-preflight-v1",
        "source_variant": "easy_tdx_mac",
        "observed_at": _stamp(observed_at),
        "target_count": len(targets),
        "targets": [
            {
                "instrument_id": target.instrument_id,
                "display_symbol": target.display_symbol,
                "display_name": target.display_name,
                "asset_class": target.asset_class,
                "source_id": target.source_id,
                "provider_symbol": target.provider_symbol,
                "source_kind": target.source_kind,
                "calendar_id": target.calendar_id,
                "timezone": target.timezone,
                "volume_semantics": target.volume_semantics,
                "volume_scope": target.volume_scope,
                "volume_completeness": target.volume_completeness,
                "policy": target.policy.as_dict(),
            }
            for target in targets
        ],
        "summary": {"cells": len(cells), "by_status": dict(sorted(counts.items()))},
        "cells": cells,
        "idempotency": idempotency,
        "read_only": True,
        "database": {"mode": "sqlite_memory", "production_database_touched": False},
        "decision": _decision(cells),
        "decision_by_asset_class": _decision_by_asset_class(cells),
        "first_gate": {
            "name": "3+3 real end-to-end for 7 days",
            "status": "partial"
            if any(cell["status"] in {"ready", "partial"} for cell in cells)
            else "blocked",
            "requirement": "technical rows, explicit source rights, closed-bar quality, and idempotent writes",
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json-out", type=Path, required=True)
    parser.add_argument("--md-out", type=Path, required=True)
    parser.add_argument("--count", type=int, default=800)
    args = parser.parse_args(argv)
    receipt = run_easy_tdx_preflight(count=args.count)
    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.md_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    args.md_out.write_text(render_markdown(receipt), encoding="utf-8")
    print(
        json.dumps(
            {
                "json": str(args.json_out),
                "markdown": str(args.md_out),
                "decision": receipt["decision"],
            },
            ensure_ascii=False,
        )
    )
    return 0 if receipt["decision"]["status"] in {"ready", "partial"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
