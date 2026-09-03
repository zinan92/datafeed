"""Identity-aware, read-only serving adapter for the Market Data Database."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import threading
from typing import Any

from kline.config import get_settings
from kline.models import AssetClass, Candle, Timeframe, TimeframeTransform
from kline.mvp_manifest import ManifestInstrument
from kline.storage import CandleSeriesKey
from kline.store import KlineReadOnlyStore
from kline.watchlist_manifest import WatchlistManifest, load_watchlist_manifest


QUERY_BACKEND_ENV = "KLINE_QUERY_BACKEND"
MARKET_DB_ENV = "KLINE_MARKET_DB_PATH"
MARKET_FIRST = "market_first"
LEGACY = "legacy"

_LEGACY_ASSET_CLASS_COMPATIBILITY = {
    (AssetClass.US_STOCK.value, "UUP"): "WATCH.CROSS.DXY",
    (AssetClass.US_STOCK.value, "SPY"): "WATCH.CROSS.SPX",
    (AssetClass.US_STOCK.value, "QQQ"): "WATCH.CROSS.NDX",
    (AssetClass.US_STOCK.value, "SCHD"): "WATCH.CROSS.SCHD",
    (AssetClass.US_STOCK.value, "^VIX"): "WATCH.CROSS.VIX",
}

# Yahoo intermittently returns UUP's 2026-08-28 row.  Until a later quality
# ticket fixes and re-verifies that historical gap, every DXY-proxy request
# stays on the explicit legacy path rather than serving a divergent series.
_FORCED_LEGACY_IDENTITIES = {"WATCH.CROSS.DXY": "market_window_unverified"}


@dataclass(frozen=True)
class MarketQueryResult:
    hit: bool
    miss_reason: str | None = None
    candles: tuple[Candle, ...] = ()
    instrument_id: str | None = None
    provider_symbol: str | None = None
    source_id: str | None = None
    stored_asset_class: str | None = None
    source_identity: dict[str, Any] | None = None
    timeframe_transform: TimeframeTransform | None = None


class MarketQueryReader:
    """Resolve public query identities to exact Watchlist series and read them safely."""

    def __init__(
        self,
        manifest_path: str | Path,
        database_path: str | Path,
        *,
        minimum_series_rows: int = 600,
    ) -> None:
        if minimum_series_rows < 1:
            raise ValueError("minimum_series_rows must be positive")
        self.manifest: WatchlistManifest = load_watchlist_manifest(manifest_path)
        self.store = KlineReadOnlyStore(str(database_path))
        self.minimum_series_rows = minimum_series_rows
        self._series_counts = {
            (
                str(row.get("source_id")),
                str(row.get("instrument_id")),
                str(row.get("timeframe")),
                str(row.get("adjustment_basis")),
                str(row.get("manifest_version")),
            ): int(row.get("row_count", 0))
            for row in self.store.mvp_latest_closed_bars()
        }
        self._source_identities = {
            (
                str(row.get("source_id")),
                str(row.get("instrument_id")),
                str(row.get("timeframe")),
                str(row.get("manifest_version")),
            ): dict(row.get("policy", {}).get("source_identity", {}))
            for row in self.store.latest_mvp_source_observations()
            if isinstance(row.get("policy"), dict)
            and isinstance(row.get("policy", {}).get("source_identity"), dict)
        }
        self._lock = threading.Lock()
        self._market_hits = 0
        self._market_misses = 0
        self._legacy_fallbacks = 0
        self._miss_reasons: Counter[str] = Counter()
        self._legacy_cells: set[str] = set()

    def _class_compatible(
        self,
        item: ManifestInstrument,
        *,
        asset_class: AssetClass,
        ticker: str,
    ) -> bool:
        if item.asset_class == asset_class.value:
            return True
        expected = _LEGACY_ASSET_CLASS_COMPATIBILITY.get(
            (asset_class.value, ticker.upper())
        )
        return expected == item.instrument_id

    def resolve_identity(
        self,
        *,
        asset_class: AssetClass,
        ticker: str,
        requested_source: str,
    ) -> ManifestInstrument | None:
        normalized = ticker.upper().strip()
        display_matches = [
            item
            for item in self.manifest.instruments
            if item.display_symbol.upper() == normalized
        ]
        provider_matches = [
            item
            for item in self.manifest.instruments
            if item.provider_symbol.upper() == normalized
        ]
        for item in (*display_matches, *provider_matches):
            if not self._class_compatible(item, asset_class=asset_class, ticker=ticker):
                continue
            if requested_source != "auto" and item.source_id != requested_source:
                continue
            return item
        return None

    def _miss(self, reason: str) -> MarketQueryResult:
        with self._lock:
            self._market_misses += 1
            self._miss_reasons[reason] += 1
        return MarketQueryResult(hit=False, miss_reason=reason)

    @staticmethod
    def _exclusive_end(value: str | None) -> str | None:
        """Convert the public provider-style exclusive end into a DB bound."""

        if value is None:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return (parsed.astimezone(timezone.utc) - timedelta(microseconds=1)).isoformat()

    def read(
        self,
        *,
        asset_class: AssetClass,
        ticker: str,
        timeframe: Timeframe,
        requested_source: str,
        limit: int,
        start: str | None = None,
        end: str | None = None,
    ) -> MarketQueryResult:
        item = self.resolve_identity(
            asset_class=asset_class,
            ticker=ticker,
            requested_source=requested_source,
        )
        if item is None:
            return self._miss("identity_not_found")
        if timeframe.value not in item.required_timeframes:
            return self._miss("timeframe_not_persisted")
        if item.volume_semantics == "not_applicable":
            return self._miss("volume_semantics_incompatible")
        forced_legacy_reason = _FORCED_LEGACY_IDENTITIES.get(item.instrument_id)
        if forced_legacy_reason is not None:
            return self._miss(forced_legacy_reason)
        key = CandleSeriesKey(
            instrument_id=item.instrument_id,
            display_symbol=item.display_symbol,
            provider_symbol=item.provider_symbol,
            source_id=item.source_id,
            asset_class=item.asset_class,
            timeframe=timeframe.value,
            adjustment_basis=item.adjustment_basis,
            manifest_version=self.manifest.version,
        )
        available_rows = self._series_counts.get(
            (
                key.source_id,
                key.instrument_id,
                key.timeframe,
                key.adjustment_basis,
                key.manifest_version,
            ),
            0,
        )
        if available_rows < max(self.minimum_series_rows, limit):
            return self._miss("insufficient_market_history")
        rows = self.store.query_mvp_candles(
            key,
            start=start,
            end=self._exclusive_end(end),
            limit=limit,
        )
        if not rows:
            return self._miss("market_rows_missing")
        watermark = self.store.get_mvp_watermark(key)
        if watermark is None:
            return self._miss("market_watermark_missing")
        preserve_timestamp = item.source_id == "hyperliquid_perpetual_public"
        candles = tuple(
            Candle(
                timestamp=(row.timestamp if preserve_timestamp else row.timestamp[:10]),
                open=row.open,
                high=row.high,
                low=row.low,
                close=row.close,
                volume=float(row.volume or 0),
                amount=row.amount,
            )
            for row in rows
        )
        metadata = {
            key: item.metadata[key]
            for key in ("identity_role", "proxy_for")
            if key in item.metadata
        }
        persisted_identity = self._source_identities.get(
            (
                key.source_id,
                key.instrument_id,
                key.timeframe,
                key.manifest_version,
            ),
            {},
        )
        source_identity = {
            **persisted_identity,
            "served_from": "market_data_database",
            "query_served_from": "market_data_database",
            "instrument_id": item.instrument_id,
            "display_symbol": item.display_symbol,
            "provider_symbol": item.provider_symbol,
            "market_data_source_id": item.source_id,
            "manifest_version": self.manifest.version,
            "adjustment_basis": item.adjustment_basis,
            "volume_semantics": item.volume_semantics,
            "stored_asset_class": item.asset_class,
            "requested_asset_class": asset_class.value,
            **metadata,
        }
        with self._lock:
            self._market_hits += 1
        return MarketQueryResult(
            hit=True,
            candles=candles,
            instrument_id=item.instrument_id,
            provider_symbol=item.provider_symbol,
            source_id=item.source_id,
            stored_asset_class=item.asset_class,
            source_identity=source_identity,
            timeframe_transform=TimeframeTransform(
                raw_timeframe=timeframe,
                timeframe_origin="native",
                aggregation={"kind": "none", "rule": "native_passthrough"},
            ),
        )

    def record_legacy_fallback(
        self,
        *,
        asset_class: AssetClass,
        ticker: str,
        timeframe: Timeframe,
        miss_reason: str,
    ) -> None:
        cell = f"{asset_class.value}:{ticker}:{timeframe.value}"
        with self._lock:
            self._legacy_fallbacks += 1
            self._legacy_cells.add(cell)
            self._miss_reasons.setdefault(miss_reason, 0)

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "mode": MARKET_FIRST,
                "market_database_path": "redacted",
                "manifest_version": self.manifest.version,
                "minimum_series_rows": self.minimum_series_rows,
                "market_hits": self._market_hits,
                "market_misses": self._market_misses,
                "legacy_fallbacks": self._legacy_fallbacks,
                "legacy_unique_cells": len(self._legacy_cells),
                "miss_reasons": dict(self._miss_reasons),
            }


_reader_lock = threading.Lock()
_reader_cache: dict[tuple[str, str, int], MarketQueryReader] = {}


def query_backend_mode() -> str:
    value = get_settings().query_backend.strip().casefold()
    if value not in {LEGACY, MARKET_FIRST}:
        raise RuntimeError(f"unsupported {QUERY_BACKEND_ENV}")
    return value


def get_market_query_reader() -> MarketQueryReader | None:
    if query_backend_mode() != MARKET_FIRST:
        return None
    settings = get_settings()
    database = settings.market_db_path.strip()
    if not database:
        raise RuntimeError(f"{MARKET_DB_ENV} is required for market_first")
    manifest = Path(__file__).resolve().parents[2] / "configs" / "watchlist_manifest.json"
    cache_key = (
        str(manifest.resolve()),
        str(Path(database).expanduser().resolve()),
        settings.market_min_rows,
    )
    with _reader_lock:
        if cache_key not in _reader_cache:
            try:
                _reader_cache[cache_key] = MarketQueryReader(
                    cache_key[0],
                    cache_key[1],
                    minimum_series_rows=cache_key[2],
                )
            except Exception as error:
                raise RuntimeError("market query backend unavailable") from error
        return _reader_cache[cache_key]


def query_backend_status() -> dict[str, Any]:
    try:
        reader = get_market_query_reader()
    except RuntimeError as error:
        return {
            "mode": "invalid",
            "status": "failed",
            "detail": type(error).__name__,
            "market_database_path": "redacted",
            "market_hits": 0,
            "market_misses": 0,
            "legacy_fallbacks": 0,
            "legacy_unique_cells": 0,
            "miss_reasons": {},
        }
    if reader is None:
        return {
            "mode": LEGACY,
            "status": "ready",
            "market_database_path": "not_configured",
            "market_hits": 0,
            "market_misses": 0,
            "legacy_fallbacks": 0,
            "legacy_unique_cells": 0,
            "miss_reasons": {},
        }
    return {"status": "ready", **reader.status()}
