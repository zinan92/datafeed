"""Controlled four-hour MVP worker and local health/serving receipts.

This module is intentionally separate from the resident launchd service.  A
caller may run one pass or supervise the loop from its own process; installing
launchd is a later cutover decision.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import fcntl
import os
from pathlib import Path
from typing import Any, Callable

from kline.ingestion import (
    IngestionError,
    IngestionOrchestrator,
    IngestionPlan,
    IngestionRunReceipt,
)
from kline.mvp_manifest import MvpManifest
from kline.storage import StoragePort


MAX_INTERVAL_SECONDS = 4 * 60 * 60


class WorkerError(RuntimeError):
    """Worker configuration or supervision error."""


@dataclass(frozen=True)
class TargetGuardResult:
    status: str
    target: str
    detail: str = ""


@dataclass(frozen=True)
class WorkerRunResult:
    status: str
    run_id: str | None
    started_at: str
    completed_at: str
    receipt: IngestionRunReceipt | None = None
    reason: str | None = None


@dataclass(frozen=True)
class WorkerState:
    status: str
    last_attempt_at: str | None
    last_success_at: str | None
    last_run_id: str | None
    next_due_at: str | None
    last_reason: str | None


def next_due_at(*, last_started: datetime, now: datetime, interval_seconds: int) -> datetime:
    """Schedule the next run without accumulating interval-overrun drift."""

    due = last_started + timedelta(seconds=interval_seconds)
    if due <= now:
        return now
    return due


class _SingleRunLock:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._fd: int | None = None

    def __enter__(self) -> "_SingleRunLock":
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(self._path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(self._fd)
            self._fd = None
            raise WorkerError("worker lock is already held") from exc
        return self

    def __exit__(self, *_: object) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None


class MvpWorker:
    """Run the orchestrator on a bounded heartbeat with local supervision gates."""

    def __init__(
        self,
        manifest: MvpManifest,
        storage: StoragePort,
        *,
        orchestrator: IngestionOrchestrator | None = None,
        adapter_resolver: Callable[[Any], Any] | None = None,
        interval_seconds: int = MAX_INTERVAL_SECONDS,
        lock_path: str | Path = ".mvp-worker.lock",
        history_start: str | None = None,
        target_guard: Callable[[], TargetGuardResult | bool] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if interval_seconds <= 0 or interval_seconds > MAX_INTERVAL_SECONDS:
            raise WorkerError("interval_seconds must be >0 and no greater than four hours")
        self.manifest = manifest
        self.storage = storage
        self.interval_seconds = interval_seconds
        self.lock_path = Path(lock_path)
        self.history_start = history_start
        self._target_guard = target_guard or (lambda: True)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._orchestrator = orchestrator or IngestionOrchestrator(
            storage,
            adapter_resolver=adapter_resolver,
            clock=self._clock,
        )
        self._state = WorkerState("idle", None, None, None, None, None)
        self._restore_state()

    def _restore_state(self) -> None:
        latest = self.storage.latest_mvp_run() if hasattr(self.storage, "latest_mvp_run") else None
        if not latest:
            return
        status = (
            "success"
            if latest["status"] == "success"
            else "partial"
            if latest["status"] == "partial"
            else "failed"
        )
        last_attempt = latest.get("started_at")
        last_success = latest.get("completed_at") if status == "success" else None
        next_due = None
        completed = latest.get("completed_at")
        if isinstance(completed, str):
            try:
                parsed = datetime.fromisoformat(completed.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                next_due = self._iso(
                    parsed.astimezone(timezone.utc) + timedelta(seconds=self.interval_seconds)
                )
            except ValueError:
                next_due = None
        self._state = WorkerState(
            status,
            last_attempt,
            last_success,
            latest.get("run_id"),
            next_due,
            latest.get("error"),
        )

    @staticmethod
    def _iso(value: datetime) -> str:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()

    def _guard(self) -> TargetGuardResult:
        value = self._target_guard()
        if isinstance(value, TargetGuardResult):
            return value
        if value is True:
            return TargetGuardResult("ready", "unspecified")
        return TargetGuardResult("blocked", "unspecified", "target guard returned false")

    async def run_once(self) -> WorkerRunResult:
        started = self._clock()
        started_at = self._iso(started)
        due = next_due_at(last_started=started, now=started, interval_seconds=self.interval_seconds)
        guard = self._guard()
        if guard.status != "ready":
            completed_at = self._iso(self._clock())
            self._state = WorkerState(
                "blocked_target",
                started_at,
                self._state.last_success_at,
                None,
                self._iso(due),
                guard.detail or guard.status,
            )
            return WorkerRunResult(
                "blocked_target",
                None,
                started_at,
                completed_at,
                reason=guard.detail or guard.status,
            )
        try:
            lock = _SingleRunLock(self.lock_path)
            lock.__enter__()
        except WorkerError as exc:
            completed_at = self._iso(self._clock())
            self._state = WorkerState(
                "lock_contended",
                started_at,
                self._state.last_success_at,
                None,
                self._iso(due),
                str(exc),
            )
            return WorkerRunResult(
                "lock_contended", None, started_at, completed_at, reason=str(exc)
            )
        try:
            run_id = f"mvp-{started.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
            receipt = await self._orchestrator.run_once(
                IngestionPlan(
                    manifest=self.manifest,
                    run_id=run_id,
                    now=started,
                    history_start=self.history_start,
                )
            )
            completed_at = self._iso(self._clock())
            next_due = next_due_at(
                last_started=started,
                now=self._clock(),
                interval_seconds=self.interval_seconds,
            )
            status = "success" if receipt.status == "success" else "partial"
            self._state = WorkerState(
                status,
                started_at,
                completed_at if status == "success" else self._state.last_success_at,
                run_id,
                self._iso(next_due),
                None,
            )
            return WorkerRunResult(status, run_id, started_at, completed_at, receipt=receipt)
        except IngestionError as exc:
            completed_at = self._iso(self._clock())
            self._state = WorkerState(
                "failed",
                started_at,
                self._state.last_success_at,
                None,
                self._iso(due),
                str(exc),
            )
            return WorkerRunResult("failed", None, started_at, completed_at, reason=str(exc))
        finally:
            lock.__exit__(None, None, None)

    def health(self, *, now: datetime | None = None) -> dict[str, Any]:
        return build_mvp_health(
            self.manifest,
            self.storage,
            state=self._state,
            now=now or self._clock(),
            interval_seconds=self.interval_seconds,
        )

    def serving(self) -> dict[str, Any]:
        return build_mvp_serving_status(self.manifest, self.storage, now=self._clock())

    async def run_forever(self, stop_event: Any) -> None:
        """Supervise the worker until an asyncio-compatible stop event is set."""

        while not stop_event.is_set():
            result = await self.run_once()
            wait_seconds = self.interval_seconds
            if result.receipt is not None:
                wait_seconds = self.interval_seconds
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=wait_seconds)
            except TimeoutError:
                continue


def _source_states(manifest: MvpManifest) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for item in manifest.instruments:
        state = grouped.setdefault(
            item.source_id,
            {
                "source_id": item.source_id,
                "status": "configured",
                "instruments": 0,
                "blocked_cells": 0,
            },
        )
        state["instruments"] += 1
        if item.source_status == "blocked_for_entitlement":
            state["status"] = "blocked_for_entitlement"
            state["blocked_cells"] += len(item.blocked_timeframes) or 5
    return sorted(grouped.values(), key=lambda value: value["source_id"])


def build_mvp_health(
    manifest: MvpManifest,
    storage: StoragePort,
    *,
    state: WorkerState | None = None,
    now: datetime,
    interval_seconds: int = MAX_INTERVAL_SECONDS,
) -> dict[str, Any]:
    latest_run = storage.latest_mvp_run() if hasattr(storage, "latest_mvp_run") else None
    storage_health = storage.mvp_storage_health() if hasattr(storage, "mvp_storage_health") else {}
    quality = storage.mvp_quality_summary() if hasattr(storage, "mvp_quality_summary") else {}
    backup = storage.latest_mvp_backup() if hasattr(storage, "latest_mvp_backup") else None
    worker_state = state or WorkerState("idle", None, None, None, None, None)
    if latest_run is None:
        overall = "blocked"
    elif latest_run["status"] == "success":
        overall = "ready"
    elif latest_run["status"] == "partial":
        overall = "partial"
    else:
        overall = "failed"
    return {
        "status": overall,
        "manifest_version": manifest.version,
        "manifest_hash": _manifest_hash(manifest),
        "worker": {
            "status": worker_state.status,
            "last_attempt_at": worker_state.last_attempt_at,
            "last_success_at": worker_state.last_success_at,
            "last_run_id": worker_state.last_run_id,
            "next_due_at": worker_state.next_due_at,
            "last_reason": worker_state.last_reason,
            "interval_seconds": interval_seconds,
        },
        "last_run": latest_run,
        "sources": _source_states(manifest),
        "latest_closed_bars": storage.mvp_latest_closed_bars()
        if hasattr(storage, "mvp_latest_closed_bars")
        else [],
        "quality": quality,
        "row_counts": storage_health,
        "raw_retention": {
            "status": "receipt_only",
            "policy": "mvp_raw_payloads_are_not_persisted_by_worker",
            "legacy_raw_table_is_outside_mvp": True,
        },
        "backup": backup,
        "as_of": _iso(now),
    }


def build_mvp_serving_status(
    manifest: MvpManifest, storage: StoragePort, *, now: datetime | None = None
) -> dict[str, Any]:
    latest = {
        (
            row["source_id"],
            row["instrument_id"],
            row["timeframe"],
            row["adjustment_basis"],
            row["manifest_version"],
        ): row
        for row in storage.mvp_latest_closed_bars()
    }
    cells: list[dict[str, Any]] = []
    for instrument in manifest.instruments:
        for timeframe in ("15m", "1h", "4h", "1d", "1w"):
            status = "ready"
            if timeframe in instrument.not_applicable_timeframes:
                status = "not_applicable"
            elif (
                timeframe in instrument.blocked_timeframes
                or instrument.source_status == "blocked_for_entitlement"
            ):
                status = "blocked"
            elif timeframe not in instrument.required_timeframes:
                status = "not_applicable"
            else:
                row = latest.get(
                    (
                        instrument.source_id,
                        instrument.instrument_id,
                        timeframe,
                        instrument.adjustment_basis,
                        manifest.version,
                    )
                )
                if row is None:
                    status = "unavailable"
                elif now is not None and instrument.calendar_id in {"crypto_24x7", "us_futures"}:
                    try:
                        latest_stamp = datetime.fromisoformat(
                            row["latest_timestamp"].replace("Z", "+00:00")
                        )
                        if latest_stamp.tzinfo is None:
                            latest_stamp = latest_stamp.replace(tzinfo=timezone.utc)
                        age = (
                            now.astimezone(timezone.utc) - latest_stamp.astimezone(timezone.utc)
                        ).total_seconds()
                        max_age = {
                            "15m": 45 * 60,
                            "1h": 90 * 60,
                            "4h": 12 * 60 * 60,
                            "1d": 3 * 24 * 60 * 60,
                            "1w": 21 * 24 * 60 * 60,
                        }[timeframe]
                        if age > max_age:
                            status = "stale"
                    except (AttributeError, TypeError, ValueError):
                        status = "unavailable"
            row = latest.get(
                (
                    instrument.source_id,
                    instrument.instrument_id,
                    timeframe,
                    instrument.adjustment_basis,
                    manifest.version,
                )
            )
            cells.append(
                {
                    "instrument_id": instrument.instrument_id,
                    "display_symbol": instrument.display_symbol,
                    "provider_symbol": instrument.provider_symbol,
                    "source_id": instrument.source_id,
                    "manifest_version": manifest.version,
                    "timeframe": timeframe,
                    "status": status,
                    "adjustment_basis": instrument.adjustment_basis,
                    "latest_closed_timestamp": row["latest_timestamp"] if row else None,
                    "row_count": row["row_count"] if row else 0,
                }
            )
    return {
        "status": "ready" if any(cell["status"] == "ready" for cell in cells) else "blocked",
        "manifest_version": manifest.version,
        "manifest_hash": _manifest_hash(manifest),
        "cells": cells,
    }


def _manifest_hash(manifest: MvpManifest) -> str:
    from kline.mvp_manifest import manifest_digest

    return manifest_digest(manifest)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()
