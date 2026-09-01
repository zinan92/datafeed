"""Single high-level, resumable ingestion seam for the MVP database."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import inspect
import json
import time as time_module
from typing import Any, Callable, Mapping, Sequence

from kline.market_calendar import QualityResult, assess_quality
from kline.models import AssetClass, Candle, Timeframe, TimeframeTransform
from kline.mvp_manifest import ALLOWED_TIMEFRAMES, MvpManifest, manifest_digest
from kline.ports import FetchReceipt, MarketDataPort
from kline.providers.base import EntitlementBlocked, ProviderError
from kline.storage import (
    CandleSeriesKey,
    MvpCandle,
    MvpRunReceipt,
    MvpRunWrite,
    QualityReceiptWrite,
    SourceObservationWrite,
    StorageError,
    StoragePort,
    TransformReceiptWrite,
    WatermarkWrite,
)


class IngestionError(RuntimeError):
    """The run could not produce a trustworthy storage receipt."""


# Coarse bars are the minimum useful fallback. Fetch them before potentially
# slow/flaky intraday endpoints so a degraded run still produces daily/weekly
# data for the dashboard.
INGESTION_TIMEFRAMES = ("1d", "1w", "15m", "1h", "4h")


@dataclass(frozen=True)
class IngestionPlan:
    manifest: MvpManifest
    run_id: str
    now: datetime | None = None
    history_start: str | None = None
    overlap_bars: int = 2
    fetch_limit: int = 500
    max_retries: int = 2
    retry_backoff_seconds: float = 0.0
    rate_budget: int | None = None
    policy: Mapping[str, Any] = field(default_factory=dict)
    instrument_ids: Sequence[str] | None = None
    timeframes: Sequence[str] | None = None
    request_interval_seconds: float = 0.0


@dataclass(frozen=True)
class CellResult:
    instrument_id: str
    display_symbol: str
    source_id: str
    provider_symbol: str
    timeframe: str
    status: str
    requested_start: str | None
    requested_end: str
    candle_count: int
    quality_flags: tuple[str, ...] = ()
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "instrument_id": self.instrument_id,
            "display_symbol": self.display_symbol,
            "source_id": self.source_id,
            "provider_symbol": self.provider_symbol,
            "timeframe": self.timeframe,
            "status": self.status,
            "requested_start": self.requested_start,
            "requested_end": self.requested_end,
            "candle_count": self.candle_count,
            "quality_flags": list(self.quality_flags),
            "error": self.error,
        }


@dataclass(frozen=True)
class IngestionRunReceipt:
    run_id: str
    status: str
    manifest_version: str
    manifest_hash: str
    started_at: str
    completed_at: str
    requested_cells: tuple[CellResult, ...]
    source_attempts: tuple[dict[str, Any], ...]
    row_counts: Mapping[str, int]
    quality: Mapping[str, int]
    blocked_cells: tuple[dict[str, Any], ...]
    storage_receipt: MvpRunReceipt

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "manifest_version": self.manifest_version,
            "manifest_hash": self.manifest_hash,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "requested_cells": [cell.to_dict() for cell in self.requested_cells],
            "source_attempts": [dict(item) for item in self.source_attempts],
            "row_counts": dict(self.row_counts),
            "quality": dict(self.quality),
            "blocked_cells": [dict(item) for item in self.blocked_cells],
            "storage_receipt": {
                "run_id": self.storage_receipt.run_id,
                "status": self.storage_receipt.status,
                "manifest_version": self.storage_receipt.manifest_version,
                "manifest_hash": self.storage_receipt.manifest_hash,
                "candle_count": self.storage_receipt.candle_count,
                "observation_count": self.storage_receipt.observation_count,
                "quality_count": self.storage_receipt.quality_count,
                "transform_count": self.storage_receipt.transform_count,
                "watermark_count": self.storage_receipt.watermark_count,
                "committed_at": self.storage_receipt.committed_at,
            },
        }


AdapterResolver = Callable[[Any], MarketDataPort | None]


class IngestionOrchestrator:
    """Execute one manifest plan and promote only validated closed candles."""

    def __init__(
        self,
        storage: StoragePort,
        *,
        adapter_resolver: AdapterResolver | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._storage = storage
        self._adapter_resolver = adapter_resolver or self._default_adapter_resolver
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    @staticmethod
    def _default_adapter_resolver(instrument: Any) -> MarketDataPort | None:
        try:
            from kline.registry import get_adapter_for_source

            return get_adapter_for_source(instrument.source_id, AssetClass(instrument.asset_class))
        except (KeyError, ValueError, ProviderError):
            return None

    @staticmethod
    def _now_iso(value: datetime) -> str:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()

    @staticmethod
    def _key(instrument: Any, timeframe: str, manifest: MvpManifest) -> CandleSeriesKey:
        return CandleSeriesKey(
            instrument_id=instrument.instrument_id,
            display_symbol=instrument.display_symbol,
            provider_symbol=instrument.provider_symbol,
            source_id=instrument.source_id,
            asset_class=instrument.asset_class,
            timeframe=timeframe,
            adjustment_basis=instrument.adjustment_basis,
            manifest_version=manifest.version,
        )

    @staticmethod
    def _hash(value: Any) -> str:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _interval(timeframe: str) -> timedelta:
        return {
            "15m": timedelta(minutes=15),
            "1h": timedelta(hours=1),
            "4h": timedelta(hours=4),
            "1d": timedelta(days=1),
            "1w": timedelta(days=7),
        }[timeframe]

    def _overlap_start(
        self, key: CandleSeriesKey, *, history_start: str | None, overlap_bars: int
    ) -> str | None:
        watermark = self._storage.get_mvp_watermark(key)
        if watermark is None:
            return history_start
        try:
            stamp = datetime.fromisoformat(watermark.last_closed_timestamp.replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            start = stamp - self._interval(key.timeframe) * max(0, overlap_bars)
            return start.astimezone(timezone.utc).isoformat()
        except ValueError as exc:
            raise IngestionError("persisted watermark is not an ISO timestamp") from exc

    async def _fetch_with_retries(
        self,
        adapter: MarketDataPort,
        ticker: str,
        timeframe: Timeframe,
        *,
        start: str | None,
        end: str,
        limit: int,
        plan: IngestionPlan,
    ) -> FetchReceipt:
        last_error: Exception | None = None
        attempts: list[dict[str, Any]] = []

        def adapter_attempts() -> list[dict[str, Any]]:
            value = getattr(adapter, "last_attempts", ()) or ()
            return [dict(item) for item in value if isinstance(item, Mapping)]

        def append_attempts(values: Sequence[Mapping[str, Any]], retry_number: int) -> None:
            attempts.extend(
                {**dict(item), "retry_number": retry_number}
                for item in values
                if isinstance(item, Mapping)
            )

        def attach_attempts(error: Exception) -> None:
            setattr(error, "attempts", tuple(attempts))

        for attempt in range(plan.max_retries + 1):
            try:
                fetch_with_receipt = getattr(adapter, "fetch_candles_with_receipt", None)
                if callable(fetch_with_receipt):
                    result = fetch_with_receipt(
                        ticker,
                        timeframe,
                        start=start,
                        end=end,
                        limit=limit,
                    )
                    if inspect.isawaitable(result):
                        result = await result
                    if isinstance(result, FetchReceipt):
                        append_attempts(result.attempts or tuple(adapter_attempts()), attempt + 1)
                        return FetchReceipt(
                            candles=result.candles,
                            timeframe_transform=result.timeframe_transform,
                            source_identity=result.source_identity,
                            raw_response=result.raw_response,
                            attempts=tuple(attempts),
                        )
                    if hasattr(result, "candles"):
                        append_attempts(
                            getattr(result, "attempts", ()) or adapter_attempts(), attempt + 1
                        )
                        return FetchReceipt(
                            candles=list(result.candles),
                            timeframe_transform=getattr(result, "timeframe_transform", None),
                            source_identity=getattr(result, "source_identity", {}) or {},
                            raw_response=getattr(result, "raw_response", None),
                            attempts=tuple(attempts),
                        )
                fetch = adapter.fetch_candles(
                    ticker,
                    timeframe,
                    start=start,
                    end=end,
                    limit=limit,
                )
                candles = await fetch if inspect.isawaitable(fetch) else fetch
                append_attempts(adapter_attempts(), attempt + 1)
                return FetchReceipt(
                    candles=list(candles),
                    timeframe_transform=getattr(adapter, "timeframe_transform", None),
                    source_identity=getattr(adapter, "source_identity", {}) or {},
                    raw_response=getattr(adapter, "last_raw_response", None),
                    attempts=tuple(attempts),
                )
            except EntitlementBlocked as exc:
                append_attempts(adapter_attempts(), attempt + 1)
                attach_attempts(exc)
                raise
            except ProviderError as exc:
                last_error = exc
                append_attempts(adapter_attempts(), attempt + 1)
                if (
                    getattr(exc, "code", "provider_error")
                    in {
                        "blocked_for_entitlement",
                        "market_closed",
                        "malformed_row",
                        "empty_response",
                    }
                    or attempt >= plan.max_retries
                ):
                    attach_attempts(exc)
                    raise
            except Exception as exc:
                last_error = exc
                append_attempts(adapter_attempts(), attempt + 1)
                if attempt >= plan.max_retries:
                    wrapped = ProviderError(f"provider fetch failed: {exc}")
                    attach_attempts(wrapped)
                    raise wrapped from exc
            if plan.retry_backoff_seconds:
                await asyncio.sleep(plan.retry_backoff_seconds * (attempt + 1))
        assert last_error is not None
        attach_attempts(last_error)
        wrapped = ProviderError(str(last_error))
        attach_attempts(wrapped)
        raise wrapped from last_error

    def _to_mvp_rows(
        self,
        instrument: Any,
        timeframe: str,
        candles: Sequence[Candle],
        manifest: MvpManifest,
        *,
        timeframe_transform: TimeframeTransform | None = None,
    ) -> list[MvpCandle]:
        key = self._key(instrument, timeframe, manifest)
        rows: list[MvpCandle] = []
        is_derived = timeframe in {"4h", "1w"} or (
            timeframe == "1h"
            and timeframe_transform is not None
            and timeframe_transform.timeframe_origin == "aggregated"
        )
        for candle in candles:
            volume = None if instrument.volume_semantics == "not_applicable" else candle.volume
            rows.append(
                MvpCandle(
                    key=key,
                    timestamp=candle.timestamp,
                    open=candle.open,
                    high=candle.high,
                    low=candle.low,
                    close=candle.close,
                    volume=volume,
                    amount=candle.amount,
                    volume_semantics=instrument.volume_semantics,
                    is_derived=is_derived,
                )
            )
        return rows

    @staticmethod
    def _quality_receipt(
        run_id: str, rows: Sequence[MvpCandle], quality: QualityResult
    ) -> QualityReceiptWrite:
        counts = {
            status: sum(issue.status == status for issue in quality.issues)
            for status in {
                "gap",
                "duplicate",
                "malformed",
                "forming",
                "missing",
                "stale",
                "partial",
            }
        }
        key = rows[0].key if rows else None
        if key is None:
            raise IngestionError("quality receipt requires a series key")
        details = {"issues": [issue.__dict__ for issue in quality.issues]}
        return QualityReceiptWrite(
            run_id=run_id,
            key=key,
            status=quality.status,
            gaps=counts["gap"],
            duplicates=counts["duplicate"],
            invalid_rows=counts["malformed"],
            blocked_cells=counts["forming"] + counts["missing"],
            details=details,
            receipt_hash=IngestionOrchestrator._hash(details),
        )

    async def run_once(self, plan: IngestionPlan) -> IngestionRunReceipt:
        """Run every manifest cell once; blocked cells remain explicit in the receipt."""

        started = plan.now or self._clock()
        started_at = self._now_iso(started)
        now = started if plan.now is not None else self._clock()
        end = self._now_iso(now)
        manifest = plan.manifest
        manifest_hash = manifest_digest(manifest)
        timeframes = tuple(plan.timeframes or INGESTION_TIMEFRAMES)
        if not timeframes or any(timeframe not in ALLOWED_TIMEFRAMES for timeframe in timeframes):
            raise IngestionError(f"ingestion timeframes must be drawn from {ALLOWED_TIMEFRAMES}")
        if plan.request_interval_seconds < 0:
            raise IngestionError("request_interval_seconds must be non-negative")
        selected_ids = set(plan.instrument_ids) if plan.instrument_ids is not None else None
        if selected_ids is not None:
            known_ids = {instrument.instrument_id for instrument in manifest.instruments}
            missing_ids = sorted(selected_ids - known_ids)
            if missing_ids:
                raise IngestionError(
                    f"ingestion identities missing from manifest: {', '.join(missing_ids)}"
                )
        cells: list[CellResult] = []
        blocked_cells: list[dict[str, Any]] = []
        source_attempts: list[dict[str, Any]] = []
        candles: list[MvpCandle] = []
        observations: list[SourceObservationWrite] = []
        qualities: list[QualityReceiptWrite] = []
        transforms: list[TransformReceiptWrite] = []
        watermarks: list[WatermarkWrite] = []
        request_count = 0
        quality_counts = {"pass": 0, "partial": 0, "fail": 0}
        watermark_regressions = 0
        last_request_at: float | None = None

        def receipt_attempts(value: Any) -> list[dict[str, Any]]:
            raw = getattr(value, "attempts", ()) or ()
            return [dict(item) for item in raw if isinstance(item, Mapping)]

        def error_attempts(error: Exception, adapter: Any) -> list[dict[str, Any]]:
            raw = getattr(error, "attempts", ()) or getattr(adapter, "last_attempts", ()) or ()
            return [dict(item) for item in raw if isinstance(item, Mapping)]

        def elapsed_ms(started_at: float | None) -> float | None:
            if started_at is None:
                return None
            return round((time_module.perf_counter() - started_at) * 1000, 1)

        def append_failed_observation(
            *,
            key: CandleSeriesKey,
            request_start: str | None,
            adapter: Any,
            error: str,
            latency_ms: float | None,
            provider_attempts: Sequence[Mapping[str, Any]],
        ) -> None:
            raw_response = getattr(adapter, "last_raw_response", None) if adapter else None
            response_hash = self._hash(raw_response) if isinstance(raw_response, Mapping) else None
            source_identity = getattr(adapter, "source_identity", {}) if adapter else {}
            observations.append(
                SourceObservationWrite(
                    run_id=plan.run_id,
                    key=key,
                    success=False,
                    request_start=request_start,
                    request_end=end,
                    response_hash=response_hash,
                    policy={
                        "fallback": "none",
                        "overlap_bars": plan.overlap_bars,
                        "source_identity": dict(source_identity or {})
                        if isinstance(source_identity, Mapping)
                        else {},
                        "provider_attempts": [dict(item) for item in provider_attempts],
                    },
                    candle_count=0,
                    latest_timestamp=None,
                    latency_ms=latency_ms,
                    served_from="upstream",
                    error=error,
                )
            )

        for instrument in manifest.instruments:
            if selected_ids is not None and instrument.instrument_id not in selected_ids:
                continue
            for timeframe in timeframes:
                if timeframe in instrument.not_applicable_timeframes:
                    cells.append(
                        CellResult(
                            instrument.instrument_id,
                            instrument.display_symbol,
                            instrument.source_id,
                            instrument.provider_symbol,
                            timeframe,
                            "not_applicable",
                            None,
                            end,
                            0,
                        )
                    )
                    continue
                if (
                    timeframe in instrument.blocked_timeframes
                    or instrument.source_status == "blocked_for_entitlement"
                ):
                    result = CellResult(
                        instrument.instrument_id,
                        instrument.display_symbol,
                        instrument.source_id,
                        instrument.provider_symbol,
                        timeframe,
                        "blocked_for_entitlement",
                        None,
                        end,
                        0,
                        error="manifest entitlement gate",
                    )
                    cells.append(result)
                    blocked_cells.append(result.to_dict())
                    continue
                if timeframe not in instrument.required_timeframes:
                    continue
                if plan.rate_budget is not None and request_count >= plan.rate_budget:
                    result = CellResult(
                        instrument.instrument_id,
                        instrument.display_symbol,
                        instrument.source_id,
                        instrument.provider_symbol,
                        timeframe,
                        "blocked_rate_budget",
                        None,
                        end,
                        0,
                        error="rate budget exhausted",
                    )
                    cells.append(result)
                    blocked_cells.append(result.to_dict())
                    quality_counts["fail"] += 1
                    continue
                key = self._key(instrument, timeframe, manifest)
                request_start = self._overlap_start(
                    key, history_start=plan.history_start, overlap_bars=plan.overlap_bars
                )
                adapter = self._adapter_resolver(instrument)
                request_count += 1
                if adapter is None:
                    result = CellResult(
                        instrument.instrument_id,
                        instrument.display_symbol,
                        instrument.source_id,
                        instrument.provider_symbol,
                        timeframe,
                        "unavailable",
                        request_start,
                        end,
                        0,
                        error="source adapter is not configured",
                    )
                    cells.append(result)
                    blocked_cells.append(result.to_dict())
                    source_attempts.append(
                        {
                            "source_id": instrument.source_id,
                            "provider_symbol": instrument.provider_symbol,
                            "timeframe": timeframe,
                            "status": "unavailable",
                            "error": "source adapter is not configured",
                        }
                    )
                    append_failed_observation(
                        key=key,
                        request_start=request_start,
                        adapter=None,
                        error="source adapter is not configured",
                        latency_ms=None,
                        provider_attempts=(),
                    )
                    qualities.append(
                        QualityReceiptWrite(
                            run_id=plan.run_id, key=key, status="blocked", blocked_cells=1
                        )
                    )
                    quality_counts["fail"] += 1
                    continue
                fetch_started_at: float | None = None
                try:
                    if last_request_at is not None and plan.request_interval_seconds:
                        elapsed = time_module.monotonic() - last_request_at
                        wait_seconds = plan.request_interval_seconds - elapsed
                        if wait_seconds > 0:
                            await asyncio.sleep(wait_seconds)
                    last_request_at = time_module.monotonic()
                    fetch_started_at = time_module.perf_counter()
                    fetch_receipt = await self._fetch_with_retries(
                        adapter,
                        instrument.display_symbol,
                        Timeframe(timeframe),
                        start=request_start,
                        end=end,
                        limit=plan.fetch_limit,
                        plan=plan,
                    )
                    latency_ms = round((time_module.perf_counter() - fetch_started_at) * 1000, 1)
                    provider_attempts = receipt_attempts(fetch_receipt)
                    raw_rows = fetch_receipt.candles
                    mvp_rows = self._to_mvp_rows(
                        instrument,
                        timeframe,
                        raw_rows,
                        manifest,
                        timeframe_transform=fetch_receipt.timeframe_transform,
                    )
                    quality = assess_quality(
                        mvp_rows,
                        timeframe=timeframe,
                        calendar_id=instrument.calendar_id,
                        cutoff=now,
                    )
                    quality_counts[quality.status] = quality_counts.get(quality.status, 0) + 1
                    quality_receipt = self._quality_receipt(plan.run_id, mvp_rows, quality)
                    qualities.append(quality_receipt)
                    response_hash = None
                    if fetch_receipt.raw_response is not None:
                        response_hash = self._hash(fetch_receipt.raw_response)
                    latest = max((row.timestamp for row in mvp_rows), default=None)
                    observations.append(
                        SourceObservationWrite(
                            run_id=plan.run_id,
                            key=key,
                            success=quality.status != "fail",
                            request_start=request_start,
                            request_end=end,
                            response_hash=response_hash,
                            policy={
                                "fallback": "none",
                                "overlap_bars": plan.overlap_bars,
                                "source_identity": dict(fetch_receipt.source_identity),
                                "provider_attempts": provider_attempts,
                            },
                            candle_count=len(mvp_rows),
                            latest_timestamp=latest,
                            latency_ms=latency_ms,
                            served_from="upstream",
                        )
                    )
                    source_attempts.append(
                        {
                            "source_id": instrument.source_id,
                            "provider_symbol": instrument.provider_symbol,
                            "timeframe": timeframe,
                            "status": quality.status,
                            "candle_count": len(mvp_rows),
                            "response_hash": response_hash,
                            "latency_ms": latency_ms,
                            "http_status": (fetch_receipt.raw_response or {}).get("http_status"),
                            "source_identity": dict(fetch_receipt.source_identity),
                            "provider_attempts": provider_attempts,
                        }
                    )
                    if quality.status == "pass" and mvp_rows:
                        candles.extend(mvp_rows)
                        existing_watermark = self._storage.get_mvp_watermark(key)
                        if (
                            existing_watermark is not None
                            and latest is not None
                            and latest < existing_watermark.last_closed_timestamp
                        ):
                            watermark_regressions += 1
                            source_attempts[-1]["watermark_regression_suppressed"] = True
                            for provider_attempt in provider_attempts:
                                provider_attempt["watermark_regression_suppressed"] = True
                        else:
                            watermarks.append(
                                WatermarkWrite(
                                    key=key,
                                    last_closed_timestamp=latest or end,
                                    cursor=None,
                                    run_id=plan.run_id,
                                )
                            )
                    if fetch_receipt.timeframe_transform is not None and mvp_rows:
                        transform = fetch_receipt.timeframe_transform
                        if transform.timeframe_origin == "aggregated":
                            transforms.append(
                                TransformReceiptWrite(
                                    run_id=plan.run_id,
                                    manifest_version=manifest.version,
                                    instrument_id=instrument.instrument_id,
                                    source_id=instrument.source_id,
                                    output_timeframe=timeframe,
                                    input_timeframe=transform.raw_timeframe.value,
                                    aggregation_rule_version=str(
                                        transform.aggregation.get("rule", "provider_derived")
                                    ),
                                    input_start=request_start or mvp_rows[0].timestamp,
                                    input_end=end,
                                    input_hash=response_hash or self._hash(raw_rows),
                                    output_hash=self._hash([row.timestamp for row in mvp_rows]),
                                    bucket_anchor=transform.aggregation.get("bucket_anchor"),
                                    partial_bucket_policy=transform.aggregation.get(
                                        "partial_bucket_policy"
                                    ),
                                    partial_bucket_count=int(
                                        transform.aggregation.get("partial_bucket_count", 0)
                                    ),
                                )
                            )
                    status = "ready" if quality.status == "pass" else quality.status
                    cell_error = None if quality.status != "fail" else "quality gate failed"
                    if source_attempts[-1].get("watermark_regression_suppressed"):
                        status = "partial"
                        cell_error = "watermark regression suppressed"
                    result = CellResult(
                        instrument.instrument_id,
                        instrument.display_symbol,
                        instrument.source_id,
                        instrument.provider_symbol,
                        timeframe,
                        status,
                        request_start,
                        end,
                        len(mvp_rows) if quality.status != "fail" else 0,
                        tuple(issue.status for issue in quality.issues),
                        cell_error,
                    )
                    cells.append(result)
                    if cell_error is not None:
                        blocked_cells.append(result.to_dict())
                except EntitlementBlocked as exc:
                    latency_ms = elapsed_ms(fetch_started_at)
                    provider_attempts = error_attempts(exc, adapter)
                    append_failed_observation(
                        key=key,
                        request_start=request_start,
                        adapter=adapter,
                        error=str(exc),
                        latency_ms=latency_ms,
                        provider_attempts=provider_attempts,
                    )
                    result = CellResult(
                        instrument.instrument_id,
                        instrument.display_symbol,
                        instrument.source_id,
                        instrument.provider_symbol,
                        timeframe,
                        "blocked_for_entitlement",
                        request_start,
                        end,
                        0,
                        error=str(exc),
                    )
                    cells.append(result)
                    blocked_cells.append(result.to_dict())
                    quality_counts["fail"] += 1
                    source_attempts.append(
                        {
                            "source_id": instrument.source_id,
                            "provider_symbol": instrument.provider_symbol,
                            "timeframe": timeframe,
                            "status": "blocked_for_entitlement",
                            "error": str(exc),
                            "latency_ms": latency_ms,
                            "provider_attempts": provider_attempts,
                        }
                    )
                    qualities.append(
                        QualityReceiptWrite(
                            run_id=plan.run_id,
                            key=key,
                            status="blocked",
                            blocked_cells=1,
                            details={"error": str(exc)},
                        )
                    )
                except ProviderError as exc:
                    latency_ms = elapsed_ms(fetch_started_at)
                    provider_attempts = error_attempts(exc, adapter)
                    append_failed_observation(
                        key=key,
                        request_start=request_start,
                        adapter=adapter,
                        error=str(exc),
                        latency_ms=latency_ms,
                        provider_attempts=provider_attempts,
                    )
                    result = CellResult(
                        instrument.instrument_id,
                        instrument.display_symbol,
                        instrument.source_id,
                        instrument.provider_symbol,
                        timeframe,
                        getattr(exc, "code", "unavailable"),
                        request_start,
                        end,
                        0,
                        error=str(exc),
                    )
                    cells.append(result)
                    blocked_cells.append(result.to_dict())
                    quality_counts["fail"] += 1
                    source_attempts.append(
                        {
                            "source_id": instrument.source_id,
                            "provider_symbol": instrument.provider_symbol,
                            "timeframe": timeframe,
                            "status": getattr(exc, "code", "unavailable"),
                            "error": str(exc),
                            "latency_ms": latency_ms,
                            "http_status": (getattr(adapter, "last_raw_response", {}) or {}).get(
                                "http_status"
                            ),
                            "provider_attempts": provider_attempts,
                        }
                    )
                    qualities.append(
                        QualityReceiptWrite(
                            run_id=plan.run_id,
                            key=key,
                            status="fail",
                            blocked_cells=1,
                            details={"error": str(exc)},
                        )
                    )
                except (StorageError, ValueError) as exc:
                    latency_ms = elapsed_ms(fetch_started_at)
                    provider_attempts = error_attempts(exc, adapter)
                    append_failed_observation(
                        key=key,
                        request_start=request_start,
                        adapter=adapter,
                        error=str(exc),
                        latency_ms=latency_ms,
                        provider_attempts=provider_attempts,
                    )
                    result = CellResult(
                        instrument.instrument_id,
                        instrument.display_symbol,
                        instrument.source_id,
                        instrument.provider_symbol,
                        timeframe,
                        "malformed",
                        request_start,
                        end,
                        0,
                        error=str(exc),
                    )
                    cells.append(result)
                    blocked_cells.append(result.to_dict())
                    quality_counts["fail"] += 1
                    source_attempts.append(
                        {
                            "source_id": instrument.source_id,
                            "provider_symbol": instrument.provider_symbol,
                            "timeframe": timeframe,
                            "status": "malformed",
                            "error": str(exc),
                            "latency_ms": latency_ms,
                            "provider_attempts": provider_attempts,
                        }
                    )
                    qualities.append(
                        QualityReceiptWrite(
                            run_id=plan.run_id,
                            key=key,
                            status="fail",
                            invalid_rows=1,
                            details={"error": str(exc)},
                        )
                    )

        failures = [cell for cell in cells if cell.status not in {"ready", "not_applicable"}]
        overall_status = "success" if not failures else "partial"
        try:
            storage_receipt = self._storage.commit_mvp_run(
                MvpRunWrite(
                    run_id=plan.run_id,
                    manifest_version=manifest.version,
                    manifest_hash=manifest_hash,
                    started_at=started_at,
                    window_start=plan.history_start,
                    window_end=end,
                    policy={**dict(plan.policy), "overlap_bars": plan.overlap_bars},
                    candles=tuple(candles),
                    source_observations=tuple(observations),
                    quality_receipts=tuple(qualities),
                    transform_receipts=tuple(transforms),
                    watermarks=tuple(watermarks),
                    status=overall_status,
                    completed_at=end,
                )
            )
        except StorageError as exc:
            raise IngestionError(f"atomic storage promotion failed: {exc}") from exc
        return IngestionRunReceipt(
            run_id=plan.run_id,
            status=overall_status,
            manifest_version=manifest.version,
            manifest_hash=manifest_hash,
            started_at=started_at,
            completed_at=end,
            requested_cells=tuple(cells),
            source_attempts=tuple(source_attempts),
            row_counts={
                "promoted_candles": len(candles),
                "observations": len(observations),
                "quality_receipts": len(qualities),
                "transform_receipts": len(transforms),
                "watermarks": len(watermarks),
                "watermarks_skipped": watermark_regressions,
            },
            quality=quality_counts,
            blocked_cells=tuple(blocked_cells),
            storage_receipt=storage_receipt,
        )
