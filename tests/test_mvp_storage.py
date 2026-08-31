from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from kline.storage import (
    BackupReceiptWrite,
    CandleSeriesKey,
    EntitlementReceiptWrite,
    MvpCandle,
    MvpRunWrite,
    QualityReceiptWrite,
    SourceObservationWrite,
    StorageError,
    TransformReceiptWrite,
    WatermarkWrite,
)
from kline.store import KlineStore


def _key(
    *,
    instrument_id: str = "US.AAPL",
    display_symbol: str = "AAPL",
    provider_symbol: str = "AAPL",
    source_id: str = "source-a",
    asset_class: str = "us_stock",
    timeframe: str = "1d",
    adjustment_basis: str = "raw_unadjusted",
    manifest_version: str = "mvp_universe_v1",
) -> CandleSeriesKey:
    return CandleSeriesKey(
        instrument_id=instrument_id,
        display_symbol=display_symbol,
        provider_symbol=provider_symbol,
        source_id=source_id,
        asset_class=asset_class,
        timeframe=timeframe,
        adjustment_basis=adjustment_basis,
        manifest_version=manifest_version,
    )


def _candle(
    key: CandleSeriesKey,
    *,
    close: float = 101.0,
    volume: float | None = 10.0,
    semantics: str = "traded",
) -> MvpCandle:
    return MvpCandle(
        key=key,
        timestamp="2026-08-31T20:00:00+00:00",
        open=100.0,
        high=max(102.0, close),
        low=99.0,
        close=close,
        volume=volume,
        volume_semantics=semantics,
    )


def _run(*, run_id: str, key: CandleSeriesKey, candles: tuple[MvpCandle, ...] = ()) -> MvpRunWrite:
    return MvpRunWrite(
        run_id=run_id,
        manifest_version=key.manifest_version,
        manifest_hash="a" * 64,
        started_at="2026-08-31T20:00:00+00:00",
        window_start="2026-08-30T00:00:00+00:00",
        window_end="2026-08-31T20:00:00+00:00",
        policy={"overlap_bars": 2},
        candles=candles,
    )


@pytest.fixture
def store(tmp_path: Path) -> KlineStore:
    return KlineStore(str(tmp_path / "mvp.db"))


def test_mvp_series_key_is_source_and_instrument_aware(store: KlineStore) -> None:
    first = _key(source_id="source-a", instrument_id="US.AAPL")
    second = _key(source_id="source-b", instrument_id="US.AAPL")

    store.commit_mvp_run(_run(run_id="run-a", key=first, candles=(_candle(first, close=101),)))
    store.commit_mvp_run(_run(run_id="run-b", key=second, candles=(_candle(second, close=202),)))

    assert store.query_mvp_candles(first)[0].close == 101
    assert store.query_mvp_candles(second)[0].close == 202
    assert store.mvp_storage_health()["candles"] == 2


def test_mvp_volume_null_is_allowed_only_for_not_applicable(store: KlineStore) -> None:
    key = _key(instrument_id="US.INDEX.SPX", display_symbol="SPX", asset_class="index")
    store.commit_mvp_run(
        _run(
            run_id="run-index",
            key=key,
            candles=(_candle(key, volume=None, semantics="not_applicable"),),
        )
    )

    assert store.query_mvp_candles(key)[0].volume is None

    with pytest.raises(StorageError, match="must be NULL"):
        _candle(key, volume=1.0, semantics="not_applicable")


def test_mvp_upsert_is_idempotent_and_replaces_same_identity(store: KlineStore) -> None:
    key = _key()
    first = store.commit_mvp_run(
        _run(run_id="run-one", key=key, candles=(_candle(key, close=101),))
    )
    second = store.commit_mvp_run(
        _run(run_id="run-two", key=key, candles=(_candle(key, close=202),))
    )

    assert first.candle_count == second.candle_count == 1
    assert len(store.query_mvp_candles(key)) == 1
    assert store.query_mvp_candles(key)[0].close == 202
    assert (
        store.commit_mvp_run(_run(run_id="run-two", key=key, candles=(_candle(key, close=999),)))
        == second
    )
    assert store.query_mvp_candles(key)[0].close == 202


def test_mvp_duplicate_input_is_rejected_before_transaction(store: KlineStore) -> None:
    key = _key()
    candle = _candle(key)
    with pytest.raises(StorageError, match="duplicate candle identity"):
        store.commit_mvp_run(_run(run_id="run-duplicate", key=key, candles=(candle, candle)))

    assert store.query_mvp_candles(key) == []
    assert store.mvp_storage_health()["runs"] == 0


def test_mvp_transaction_rolls_back_candles_receipts_and_watermarks(store: KlineStore) -> None:
    key = _key()
    write = replace(
        _run(run_id="run-rollback", key=key, candles=(_candle(key),)),
        quality_receipts=(
            QualityReceiptWrite(
                run_id="run-rollback",
                key=key,
                status="pass",
            ),
        ),
        watermarks=(
            WatermarkWrite(
                key=key,
                last_closed_timestamp="2026-08-31T20:00:00+00:00",
                cursor="cursor-1",
                run_id="run-rollback",
            ),
        ),
    )

    def fail(stage: str) -> None:
        if stage == "after_candles":
            raise RuntimeError("forced interruption")

    store._mvp_commit_failpoint = fail
    with pytest.raises(StorageError, match="forced interruption"):
        store.commit_mvp_run(write)

    assert store.query_mvp_candles(key) == []
    assert store.get_mvp_watermark(key) is None
    assert store.mvp_storage_health()["runs"] == 0


def test_mvp_persists_source_quality_transform_entitlement_and_backup_receipts(
    store: KlineStore,
) -> None:
    key = _key()
    run_id = "run-receipts"
    write = replace(
        _run(run_id=run_id, key=key, candles=(_candle(key),)),
        source_observations=(
            SourceObservationWrite(
                run_id=run_id,
                key=key,
                success=True,
                request_start="2026-08-30T00:00:00+00:00",
                request_end="2026-08-31T20:00:00+00:00",
                response_hash="b" * 64,
                policy={"retention": "receipt-only"},
                candle_count=1,
                latest_timestamp="2026-08-31T20:00:00+00:00",
            ),
        ),
        quality_receipts=(QualityReceiptWrite(run_id=run_id, key=key, status="pass"),),
        transform_receipts=(
            TransformReceiptWrite(
                run_id=run_id,
                manifest_version=key.manifest_version,
                instrument_id=key.instrument_id,
                source_id=key.source_id,
                output_timeframe="1w",
                input_timeframe="1d",
                aggregation_rule_version="mvp-aggregation-v1",
                input_start="2026-08-24",
                input_end="2026-08-28",
                input_hash="c" * 64,
                output_hash="d" * 64,
            ),
        ),
        entitlement_receipts=(
            EntitlementReceiptWrite(
                receipt_id="entitlement-source-a-v1",
                source_id=key.source_id,
                status="active",
                allowed_history={"days": 365},
                timeframe_permissions=("1d", "1w"),
                persistence_allowed=True,
                derived_allowed=True,
                non_display_allowed=True,
                valid_from="2026-08-01",
                valid_to=None,
                evidence_ref="operator://receipt/1",
                receipt_hash="e" * 64,
            ),
        ),
        backup_receipts=(
            BackupReceiptWrite(
                backup_id="backup-run-receipts",
                run_id=run_id,
                destination="nas://market-data/mvp.db",
                status="verified",
                checksum="f" * 64,
                size_bytes=1024,
                restore_verified=True,
                policy={"generations": 3},
            ),
        ),
    )

    receipt = store.commit_mvp_run(write)
    assert receipt.observation_count == 1
    assert receipt.quality_count == 1
    assert receipt.transform_count == 1
    health = store.mvp_storage_health()
    assert health == {
        "status": "ok",
        "candles": 1,
        "runs": 1,
        "watermarks": 0,
        "source_observations": 1,
        "quality_receipts": 1,
        "transform_receipts": 1,
        "entitlement_receipts": 1,
        "backup_receipts": 1,
    }
