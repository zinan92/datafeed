"""Single high-level, resumable ingestion seam for the MVP database."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import inspect
import json
from typing import Any, Callable, Mapping, Sequence

from kline.market_calendar import QualityResult, assess_quality
from kline.models import AssetClass, Candle, Timeframe
from kline.mvp_manifest import MvpManifest, manifest_digest
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
                        return result
                    if hasattr(result, "candles"):
                        return FetchReceipt(
                            candles=list(result.candles),
                            timeframe_transform=getattr(result, "timeframe_transform", None),
                            source_identity=getattr(result, "source_identity", {}) or {},
                            raw_response=getattr(result, "raw_response", None),
                        )
                fetch = adapter.fetch_candles(
                    ticker,
                    timeframe,
                    start=start,
                    end=end,
                    limit=limit,
                )
                candles = await fetch if inspect.isawaitable(fetch) else fetch
                return FetchReceipt(
                    candles=list(candles),
                    timeframe_transform=getattr(adapter, "timeframe_transform", None),
                    source_identity=getattr(adapter, "source_identity", {}) or {},
                    raw_response=getattr(adapter, "last_raw_response", None),
                )
            except EntitlementBlocked:
                raise
            except ProviderError as exc:
                last_error = exc
                if (
                    getattr(exc, "code", "provider_error")
                    in {
                        "blocked_for_entitlement",
                        "market_closed",
                        "malformed_row",
                    }
                    or attempt >= plan.max_retries
                ):
                    raise
            except Exception as exc:
                last_error = exc
                if attempt >= plan.max_retries:
                    raise ProviderError(f"provider fetch failed: {exc}") from exc
            if plan.retry_backoff_seconds:
                await asyncio.sleep(plan.retry_backoff_seconds * (attempt + 1))
        assert last_error is not None
        raise ProviderError(str(last_error)) from last_error

    def _to_mvp_rows(
        self, instrument: Any, timeframe: str, candles: Sequence[Candle], manifest: MvpManifest
    ) -> list[MvpCandle]:
        key = self._key(instrument, timeframe, manifest)
        rows: list[MvpCandle] = []
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
                    is_derived=timeframe in {"4h", "1w"},
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

        for instrument in manifest.instruments:
            for timeframe in ("15m", "4h", "1d", "1w"):
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
                    qualities.append(
                        QualityReceiptWrite(
                            run_id=plan.run_id, key=key, status="blocked", blocked_cells=1
                        )
                    )
                    quality_counts["fail"] += 1
                    continue
                try:
                    fetch_receipt = await self._fetch_with_retries(
                        adapter,
                        instrument.display_symbol,
                        Timeframe(timeframe),
                        start=request_start,
                        end=end,
                        limit=plan.fetch_limit,
                        plan=plan,
                    )
                    raw_rows = fetch_receipt.candles
                    mvp_rows = self._to_mvp_rows(instrument, timeframe, raw_rows, manifest)
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
                            policy={"fallback": "none", "overlap_bars": plan.overlap_bars},
                            candle_count=len(mvp_rows),
                            latest_timestamp=latest,
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
                        }
                    )
                    if quality.status == "pass" and mvp_rows:
                        candles.extend(mvp_rows)
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
                        None if quality.status != "fail" else "quality gate failed",
                    )
                    cells.append(result)
                    if quality.status == "fail":
                        blocked_cells.append(result.to_dict())
                except EntitlementBlocked as exc:
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
            },
            quality=quality_counts,
            blocked_cells=tuple(blocked_cells),
            storage_receipt=storage_receipt,
        )
