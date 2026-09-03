"""Capture and byte-diff the real Newsletter and Human Review request set."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


NEWSLETTER_FILES = (
    Path(
        "/Users/wendy/Library/Application Support/ParkKlineDaily/app/product/data_core/"
        "market_regime_daily_source.py"
    ),
    Path(
        "/Users/wendy/Library/Application Support/ParkKlineDaily/app/product/data_core/"
        "market_regime_weekly_contract.py"
    ),
    Path(
        "/Users/wendy/Library/Application Support/ParkKlineDaily/app/product/data_core/"
        "market_regime_weekly_datafeed.py"
    ),
)
HUMAN_REVIEW_FILES = (
    Path(
        "/Users/wendy/Library/Application Support/HumanKlineReview/app/"
        "human_kline_review/datafeed_source.py"
    ),
    Path(
        "/Users/wendy/Library/Application Support/HumanKlineReview/app/"
        "human_kline_review/history.py"
    ),
    Path(
        "/Users/wendy/Library/Application Support/HumanKlineReview/app/"
        "human_kline_review/universe.py"
    ),
)


@dataclass(frozen=True)
class ConsumerRequest:
    consumer: str
    asset_key: str
    asset_class: str
    ticker: str
    timeframe: str
    source: str
    limit: int
    quality: str
    fallback_sources: tuple[str, ...] = ()
    conditional: bool = False

    @property
    def request_id(self) -> str:
        suffix = ":conditional" if self.conditional else ""
        return (
            f"{self.consumer}:{self.asset_key}:{self.asset_class}:{self.ticker}:"
            f"{self.timeframe}{suffix}"
        )


def _newsletter_requests() -> list[ConsumerRequest]:
    daily = (
        ("dxy", "etf", "UUP", "yahoo_finance_etf", ()),
        ("sp500", "etf", "SPY", "yahoo_finance_etf", ()),
        ("nasdaq", "etf", "QQQ", "yahoo_finance_etf", ()),
        ("us_dividend", "etf", "SCHD", "yahoo_finance_etf", ()),
        ("vix", "index", "^VIX", "yahoo_finance_index", ()),
        ("shanghai", "index", "sh000001", "tencent_kline", ("sina_index",)),
        ("star50", "index", "sh000688", "tencent_kline", ("sina_index",)),
        ("china_dividend", "index", "sh000015", "tencent_kline", ("sina_index",)),
        ("nikkei", "index", "^N225", "yahoo_finance_index", ()),
        ("kospi", "index", "^KS11", "yahoo_finance_index", ()),
    )
    multi = (
        ("bitcoin", "crypto", "BTC", "hyperliquid_perpetual_public"),
        ("ethereum", "crypto", "ETH", "hyperliquid_perpetual_public"),
        ("hype", "crypto", "HYPE", "hyperliquid_perpetual_public"),
        ("wti", "commodity", "CL=F", "yahoo_finance_futures"),
        ("gold", "commodity", "GC=F", "yahoo_finance_futures"),
        ("silver", "commodity", "SI=F", "yahoo_finance_futures"),
    )
    requests = [
        ConsumerRequest("newsletter", key, asset_class, ticker, "1d", source, 300, "strict", fallbacks)
        for key, asset_class, ticker, source, fallbacks in daily
    ]
    requests.extend(
        ConsumerRequest("newsletter", key, asset_class, ticker, timeframe, source, 300, "strict")
        for key, asset_class, ticker, source in multi
        for timeframe in ("1d", "4h", "30m")
    )
    return requests


def _human_requests() -> list[ConsumerRequest]:
    primary = (
        ("dxy", "us_stock", "UUP", "auto", ("1d", "30m")),
        ("sp500", "us_stock", "SPY", "auto", ("1d", "30m")),
        ("nasdaq", "us_stock", "QQQ", "auto", ("1d", "30m")),
        ("us_dividend", "us_stock", "SCHD", "auto", ("1d", "30m")),
        ("vix", "us_stock", "^VIX", "auto", ("1d", "4h", "30m")),
        ("bitcoin", "crypto", "BTC", "hyperliquid_perpetual_public", ("1d", "4h", "30m")),
        ("ethereum", "crypto", "ETH", "hyperliquid_perpetual_public", ("1d", "4h", "30m")),
        ("hype", "crypto", "HYPE", "hyperliquid_perpetual_public", ("1d", "4h", "30m")),
        ("shanghai", "index", "sh000001", "tencent_kline", ("1d",)),
        ("star50", "index", "sh000688", "tencent_kline", ("1d",)),
        ("china_dividend", "index", "sh000015", "tencent_kline", ("1d",)),
        ("nikkei", "index", "^N225", "auto", ("1d",)),
        ("kospi", "index", "^KS11", "auto", ("1d",)),
        ("wti", "commodity", "CL=F", "auto", ("1d", "4h", "30m")),
        ("gold", "us_stock", "MGCV26.CMX", "yahoo_finance", ("1d", "4h", "30m")),
        ("silver", "us_stock", "SILZ26.CMX", "yahoo_finance", ("1d", "4h", "30m")),
    )
    factor = {"1d": 1, "4h": 4, "30m": 1}
    requests = [
        ConsumerRequest(
            "human_review",
            key,
            asset_class,
            ticker,
            timeframe,
            source,
            1000 * factor[timeframe] + 8,
            "standard",
        )
        for key, asset_class, ticker, source, timeframes in primary
        for timeframe in timeframes
    ]
    for key, asset_class, ticker, source in (
        ("vix", "us_stock", "^VIX", "auto"),
        ("bitcoin", "crypto", "BTC", "hyperliquid_perpetual_public"),
        ("ethereum", "crypto", "ETH", "hyperliquid_perpetual_public"),
        ("hype", "crypto", "HYPE", "hyperliquid_perpetual_public"),
        ("wti", "commodity", "CL=F", "auto"),
        ("gold", "us_stock", "MGCV26.CMX", "yahoo_finance"),
        ("silver", "us_stock", "SILZ26.CMX", "yahoo_finance"),
    ):
        requests.append(
            ConsumerRequest(
                "human_review",
                key,
                asset_class,
                ticker,
                "1h",
                source,
                4008,
                "standard",
                conditional=True,
            )
        )
    return requests


def consumer_requests() -> tuple[ConsumerRequest, ...]:
    requests = (*_newsletter_requests(), *_human_requests())
    ids = [item.request_id for item in requests]
    if len(ids) != len(set(ids)):
        raise RuntimeError("consumer request ids must be unique")
    return requests


def _file_hashes(paths: Sequence[Path]) -> dict[str, str]:
    return {
        str(path): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in paths
        if path.is_file()
    }


def _request_url(base_url: str, item: ConsumerRequest, cutoff: str) -> str:
    deterministic_start = {
        "1d": "2021-01-01",
        "4h": "2024-09-03",
        "1h": "2024-09-03",
        "30m": "2026-07-06",
    }[item.timeframe]
    fallback_policy = "explicit" if item.fallback_sources else "none"
    params: list[tuple[str, str]] = [
        ("timeframe", item.timeframe),
        ("source", item.source),
        ("cache_policy", "bypass"),
        ("quality", item.quality),
        ("fallback_policy", fallback_policy),
        ("limit", str(item.limit)),
        ("start", deterministic_start),
        ("end", cutoff),
    ]
    params.extend(("fallback_sources", value) for value in item.fallback_sources)
    return (
        f"{base_url.rstrip('/')}/api/candles/{quote(item.asset_class, safe='')}/"
        f"{quote(item.ticker, safe='')}?{urlencode(params)}"
    )


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _normalized_payload(payload: Any) -> Any:
    if not isinstance(payload, Mapping):
        return payload
    result = dict(payload)
    result.pop("age_seconds", None)
    result.pop("instrument_id", None)
    identity = result.get("source_identity")
    if isinstance(identity, Mapping):
        cleaned = dict(identity)
        for key in (
            "served_from",
            "query_served_from",
            "market_data_miss_reason",
            "instrument_id",
            "display_symbol",
            "market_data_source_id",
            "manifest_version",
            "adjustment_basis",
            "volume_semantics",
            "stored_asset_class",
            "requested_asset_class",
            "requested_ticker",
            "identity_role",
            "proxy_for",
        ):
            cleaned.pop(key, None)
        result["source_identity"] = cleaned
    detail = result.get("detail")
    if isinstance(detail, Mapping):
        result["detail"] = _normalized_payload(detail)
    return result


def _capture_one(base_url: str, item: ConsumerRequest, cutoff: str, timeout: float) -> dict[str, Any]:
    url = _request_url(base_url, item, cutoff)
    request = Request(url, headers={"Accept": "application/json"}, method="GET")
    try:
        with urlopen(request, timeout=timeout) as response:
            status = int(response.status)
            raw = response.read()
    except HTTPError as error:
        status = int(error.code)
        raw = error.read()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload = {"unparseable_body_sha256": hashlib.sha256(raw).hexdigest()}
    normalized = _normalized_payload(payload)
    candles = payload.get("candles") if isinstance(payload, Mapping) else None
    identity = payload.get("source_identity") if isinstance(payload, Mapping) else None
    if not isinstance(identity, Mapping) and isinstance(payload, Mapping):
        detail = payload.get("detail")
        identity = detail.get("source_identity") if isinstance(detail, Mapping) else None
    return {
        "request_id": item.request_id,
        "consumer": item.consumer,
        "asset_key": item.asset_key,
        "asset_class": item.asset_class,
        "ticker": item.ticker,
        "timeframe": item.timeframe,
        "source": item.source,
        "limit": item.limit,
        "quality": item.quality,
        "conditional": item.conditional,
        "url": url,
        "http_status": status,
        "raw_sha256": hashlib.sha256(raw).hexdigest(),
        "normalized_sha256": hashlib.sha256(_canonical(normalized)).hexdigest(),
        "candles_sha256": hashlib.sha256(_canonical(candles)).hexdigest()
        if isinstance(candles, list)
        else None,
        "candle_count": len(candles) if isinstance(candles, list) else 0,
        "query_served_from": identity.get("query_served_from")
        if isinstance(identity, Mapping)
        else None,
        "payload": payload,
    }


def capture_snapshot(
    *, base_url: str, cutoff: str, timeout: float = 60.0, workers: int = 4
) -> dict[str, Any]:
    requests = consumer_requests()
    results: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="cutover-verify") as pool:
        futures = {
            pool.submit(_capture_one, base_url, item, cutoff, timeout): item
            for item in requests
        }
        for future in as_completed(futures):
            item = futures[future]
            try:
                results[item.request_id] = future.result()
            except Exception as error:  # pragma: no cover - evidence boundary
                results[item.request_id] = {
                    "request_id": item.request_id,
                    "transport_error": f"{type(error).__name__}:{error}",
                }
    ordered = [results[item.request_id] for item in requests]
    return {
        "schema_version": "consumer-cutover-snapshot-v1",
        "captured_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "base_url": base_url,
        "cutoff": cutoff,
        "request_count": len(requests),
        "newsletter_request_count": sum(item.consumer == "newsletter" for item in requests),
        "human_primary_request_count": sum(
            item.consumer == "human_review" and not item.conditional for item in requests
        ),
        "human_conditional_request_count": sum(item.conditional for item in requests),
        "consumer_file_sha256": {
            **_file_hashes(NEWSLETTER_FILES),
            **_file_hashes(HUMAN_REVIEW_FILES),
        },
        "requests": ordered,
    }


def capture_paired_snapshots(
    *,
    baseline_url: str,
    candidate_url: str,
    cutoff: str,
    timeout: float = 60.0,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Replay each cell against both services in lockstep.

    Sequential calls avoid doubling Yahoo/Tencent concurrency while still
    keeping the two observations adjacent in time.
    """

    requests = consumer_requests()
    baseline_rows: list[dict[str, Any]] = []
    candidate_rows: list[dict[str, Any]] = []
    for item in requests:
        baseline_rows.append(_capture_one(baseline_url, item, cutoff, timeout))
        candidate_rows.append(_capture_one(candidate_url, item, cutoff, timeout))

    common = {
        "schema_version": "consumer-cutover-snapshot-v1",
        "captured_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "cutoff": cutoff,
        "request_count": len(requests),
        "newsletter_request_count": 28,
        "human_primary_request_count": 34,
        "human_conditional_request_count": 7,
        "consumer_file_sha256": {
            **_file_hashes(NEWSLETTER_FILES),
            **_file_hashes(HUMAN_REVIEW_FILES),
        },
    }
    baseline = {**common, "base_url": baseline_url, "requests": baseline_rows}
    candidate = {**common, "base_url": candidate_url, "requests": candidate_rows}
    return baseline, candidate, compare_snapshots(baseline, candidate)


def compare_snapshots(baseline: Mapping[str, Any], candidate: Mapping[str, Any]) -> dict[str, Any]:
    baseline_rows = {row["request_id"]: row for row in baseline.get("requests", ())}
    candidate_rows = {row["request_id"]: row for row in candidate.get("requests", ())}
    all_ids = sorted(set(baseline_rows) | set(candidate_rows))
    differences: list[dict[str, Any]] = []
    market_hits = 0
    legacy_fallbacks = 0
    for request_id in all_ids:
        before = baseline_rows.get(request_id)
        after = candidate_rows.get(request_id)
        if after:
            if after.get("query_served_from") == "market_data_database":
                market_hits += 1
            elif str(after.get("query_served_from") or "").startswith("legacy_"):
                legacy_fallbacks += 1
        if before is None or after is None:
            differences.append({"request_id": request_id, "reason": "request_set_changed"})
            continue
        changed: list[str] = []
        for field in ("http_status", "normalized_sha256", "candles_sha256", "candle_count"):
            if before.get(field) != after.get(field):
                changed.append(field)
        if changed:
            differences.append({"request_id": request_id, "changed": changed})
    return {
        "schema_version": "consumer-cutover-comparison-v1",
        "baseline_cutoff": baseline.get("cutoff"),
        "candidate_cutoff": candidate.get("cutoff"),
        "request_count": len(all_ids),
        "exact_match_count": len(all_ids) - len(differences),
        "difference_count": len(differences),
        "market_database_requests": market_hits,
        "legacy_requests": legacy_fallbacks,
        "differences": differences,
    }


def _write_json(path: str | Path, payload: Mapping[str, Any]) -> None:
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Capture or compare consumer cutover evidence")
    subparsers = parser.add_subparsers(dest="command", required=True)
    capture = subparsers.add_parser("capture")
    capture.add_argument("--base-url", default="http://127.0.0.1:8100")
    capture.add_argument("--cutoff", default=None)
    capture.add_argument("--timeout", type=float, default=60.0)
    capture.add_argument("--workers", type=int, default=4)
    capture.add_argument("--output", required=True)
    compare = subparsers.add_parser("compare")
    compare.add_argument("--baseline", required=True)
    compare.add_argument("--candidate", required=True)
    compare.add_argument("--output", required=True)
    paired = subparsers.add_parser("paired")
    paired.add_argument("--baseline-url", required=True)
    paired.add_argument("--candidate-url", required=True)
    paired.add_argument("--cutoff", default=None)
    paired.add_argument("--timeout", type=float, default=60.0)
    paired.add_argument("--baseline-output", required=True)
    paired.add_argument("--candidate-output", required=True)
    paired.add_argument("--comparison-output", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "capture":
        cutoff = args.cutoff or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        payload = capture_snapshot(
            base_url=args.base_url,
            cutoff=cutoff,
            timeout=args.timeout,
            workers=args.workers,
        )
        _write_json(args.output, payload)
        print(json.dumps({key: payload[key] for key in ("cutoff", "request_count")}, sort_keys=True))
        return 0 if not any("transport_error" in row for row in payload["requests"]) else 2
    if args.command == "paired":
        cutoff = args.cutoff or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        baseline, candidate, comparison = capture_paired_snapshots(
            baseline_url=args.baseline_url,
            candidate_url=args.candidate_url,
            cutoff=cutoff,
            timeout=args.timeout,
        )
        _write_json(args.baseline_output, baseline)
        _write_json(args.candidate_output, candidate)
        _write_json(args.comparison_output, comparison)
        print(json.dumps(comparison, ensure_ascii=False, sort_keys=True))
        return 0 if comparison["difference_count"] == 0 else 2
    baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
    candidate = json.loads(Path(args.candidate).read_text(encoding="utf-8"))
    payload = compare_snapshots(baseline, candidate)
    _write_json(args.output, payload)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0 if payload["difference_count"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
