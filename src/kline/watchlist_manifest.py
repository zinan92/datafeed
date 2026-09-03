"""Independent manifest contract for Wendy's judgment-curated Watchlist."""


from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from kline.models import AssetClass, Timeframe
from kline.mvp_manifest import ALLOWED_TIMEFRAMES, ManifestError, ManifestInstrument
from kline.provenance import source_manifest


WATCHLIST_MANIFEST_VERSION = "watchlist_universe_v1"
_SECURITY_TYPES = frozenset(
    {
        "common_stock",
        "adr",
        "foreign_common",
        "etf",
        "index",
        "crypto_perpetual",
        "continuous_future",
    }
)
_VOLUME_SEMANTICS = frozenset({"traded", "quote_derived", "not_applicable"})
_SOURCE_STATUS = frozenset({"configured", "blocked_for_entitlement"})
_DAILY_NOT_APPLICABLE = frozenset(set(ALLOWED_TIMEFRAMES) - {"1d"})

_CROSS_MARKET_EXPECTATIONS: dict[str, tuple[str, str, str, str]] = {
    "SPX": ("etf", "etf", "yahoo_finance_etf", "SPY"),
    "NDX": ("etf", "etf", "yahoo_finance_etf", "QQQ"),
    "DXY": ("etf", "etf", "yahoo_finance_etf", "UUP"),
    "SCHD": ("etf", "etf", "yahoo_finance_etf", "SCHD"),
    "VIX": ("index", "index", "yahoo_finance_index", "^VIX"),
    "BTC": ("crypto", "crypto_perpetual", "hyperliquid_perpetual_public", "BTC"),
    "ETH": ("crypto", "crypto_perpetual", "hyperliquid_perpetual_public", "ETH"),
    "HYPE": ("crypto", "crypto_perpetual", "hyperliquid_perpetual_public", "HYPE"),
    "sh000001": ("index", "index", "tencent_kline", "sh000001"),
    "sh000688": ("index", "index", "tencent_kline", "sh000688"),
    "sh000015": ("index", "index", "tencent_kline", "sh000015"),
    "^N225": ("index", "index", "yahoo_finance_index", "^N225"),
    "^KS11": ("index", "index", "yahoo_finance_index", "^KS11"),
    "CL=F": ("commodity", "continuous_future", "yahoo_finance_futures", "CL=F"),
    "GC=F": ("commodity", "continuous_future", "yahoo_finance_futures", "GC=F"),
    "SI=F": ("commodity", "continuous_future", "yahoo_finance_futures", "SI=F"),
}
_PROXY_TARGETS = {
    "SPX": "S&P 500 Index",
    "NDX": "Nasdaq-100 Index",
    "DXY": "DXY",
}


@dataclass(frozen=True)
class WatchlistManifest:
    version: str
    selection_as_of: str
    effective_at: str | None
    membership_policy: str
    excluded_symbols: tuple[str, ...]
    instruments: tuple[ManifestInstrument, ...]
    registry: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "version": self.version,
            "selection_as_of": self.selection_as_of,
            "effective_at": self.effective_at,
            "membership_policy": self.membership_policy,
            "excluded_symbols": list(self.excluded_symbols),
            "instruments": [item.to_dict() for item in self.instruments],
        }
        if self.registry:
            payload["registry"] = dict(self.registry)
        return payload

    def validated_digest(self) -> str:
        return watchlist_manifest_digest(self)


def _required_text(payload: Mapping[str, Any], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f"watchlist.{name} must be a non-empty string")
    return value.strip()


def _validate_instrument(item: ManifestInstrument, *, index: int) -> None:
    if item.universe != "watchlist":
        raise ManifestError(f"instrument[{index}] has unknown universe: {item.universe}")
    if not item.instrument_id.startswith("WATCH."):
        raise ManifestError(f"instrument[{index}].instrument_id must use WATCH namespace")
    if item.security_type not in _SECURITY_TYPES:
        raise ManifestError(f"instrument[{index}] has unknown security_type: {item.security_type}")
    if item.volume_semantics not in _VOLUME_SEMANTICS:
        raise ManifestError(f"instrument[{index}] has unknown volume_semantics")
    if item.source_status not in _SOURCE_STATUS:
        raise ManifestError(f"instrument[{index}] has unknown source_status")
    required = set(item.required_timeframes)
    not_applicable = set(item.not_applicable_timeframes)
    blocked = set(item.blocked_timeframes)
    if required != {"1d"} or not_applicable != _DAILY_NOT_APPLICABLE or blocked:
        raise ManifestError(f"instrument[{index}] must be daily-only")
    if required | not_applicable | blocked != set(ALLOWED_TIMEFRAMES):
        raise ManifestError(f"instrument[{index}] must classify every MVP timeframe")
    expected: tuple[str | tuple[str, ...], str] | None = None
    if item.display_symbol in _CROSS_MARKET_EXPECTATIONS:
        expected_asset_class, expected_type, expected_source, expected_symbol = (
            _CROSS_MARKET_EXPECTATIONS[item.display_symbol]
        )
        if item.asset_class != expected_asset_class:
            raise ManifestError(
                f"instrument[{index}] cross-market asset_class must be {expected_asset_class}"
            )
        if (
            item.security_type,
            item.source_id,
            item.provider_symbol,
        ) != (expected_type, expected_source, expected_symbol):
            raise ManifestError(
                f"instrument[{index}] cross-market source/provider identity is invalid"
            )
        if item.display_symbol in _PROXY_TARGETS:
            expected_proxy_for = _PROXY_TARGETS[item.display_symbol]
            if (
                item.metadata.get("identity_role") != "proxy"
                or item.metadata.get("proxy_for") != expected_proxy_for
            ):
                raise ManifestError(
                    f"instrument[{index}] {item.display_symbol} must declare uniform proxy metadata"
                )
        elif "identity_role" in item.metadata or "proxy_for" in item.metadata:
            raise ManifestError(
                f"instrument[{index}] {item.display_symbol} cannot declare a proxy identity"
            )
    elif item.asset_class == AssetClass.A_SHARE.value:
        expected = ("common_stock", "tencent_stock_free")
    elif item.asset_class == AssetClass.ETF.value:
        expected = ("etf", "tencent_etf_free")
    elif item.asset_class == AssetClass.US_STOCK.value:
        expected = (("common_stock", "adr", "foreign_common"), "yahoo_finance")
    elif item.asset_class == AssetClass.HK_STOCK.value:
        expected = (("common_stock", "foreign_common"), "yahoo_finance_hk")
    else:
        raise ManifestError(f"instrument[{index}] has unsupported asset_class")
    if expected is not None:
        expected_type, expected_source = expected
        allowed_types = {expected_type} if isinstance(expected_type, str) else set(expected_type)
        if item.security_type not in allowed_types or item.source_id != expected_source:
            raise ManifestError(f"instrument[{index}] source/security identity is invalid")
    try:
        registered = source_manifest(item.source_id, AssetClass(item.asset_class))
    except (KeyError, ValueError) as exc:
        raise ManifestError(f"instrument[{index}] source is not registered") from exc
    if item.source_status == "configured" and not registered.supports_timeframe(
        item.provider_symbol, Timeframe.DAY
    ):
        raise ManifestError(f"instrument[{index}] source does not support 1d")


def validate_watchlist_manifest(
    payload: Mapping[str, Any] | WatchlistManifest,
) -> WatchlistManifest:
    raw = payload.to_dict() if isinstance(payload, WatchlistManifest) else dict(payload)
    version = _required_text(raw, "version")
    if version != WATCHLIST_MANIFEST_VERSION:
        raise ManifestError(f"unsupported Watchlist manifest version: {version}")
    instruments_raw = raw.get("instruments")
    if not isinstance(instruments_raw, list):
        raise ManifestError("watchlist.instruments must be a list")
    instruments = tuple(
        ManifestInstrument.from_dict(item, index=index)
        for index, item in enumerate(instruments_raw)
    )
    for index, item in enumerate(instruments):
        _validate_instrument(item, index=index)
    ids = [item.instrument_id.casefold() for item in instruments]
    symbols = [item.display_symbol.upper() for item in instruments]
    provider_keys = [
        (item.source_id, item.provider_symbol.upper(), item.asset_class) for item in instruments
    ]
    if len(ids) != len(set(ids)):
        raise ManifestError("Watchlist instrument_id values must be unique")
    if len(provider_keys) != len(set(provider_keys)):
        raise ManifestError("Watchlist provider identities must be unique")
    if len(symbols) < 1:
        raise ManifestError("Watchlist membership must contain at least one instrument")
    excluded = raw.get("excluded_symbols")
    if not isinstance(excluded, list) or "051505" not in excluded:
        raise ManifestError("Watchlist must keep unresolved 051505 explicitly excluded")
    membership_policy = _required_text(raw, "membership_policy")
    if membership_policy not in {
        "wendy_direct_judgment_no_freeze",
        "park_exposure_registry_pinned",
    }:
        raise ManifestError("Watchlist membership_policy is unsupported")
    effective_at = raw.get("effective_at")
    if effective_at is not None and not isinstance(effective_at, str):
        raise ManifestError("watchlist.effective_at must be a string or null")
    registry = raw.get("registry", {})
    if not isinstance(registry, Mapping):
        raise ManifestError("watchlist.registry must be an object")
    if registry:
        repository = registry.get("repository")
        commit = registry.get("commit")
        source_sha256 = registry.get("source_sha256")
        if repository != "zinan92/watchlist":
            raise ManifestError("watchlist.registry.repository is invalid")
        if not isinstance(commit, str) or len(commit) != 40:
            raise ManifestError("watchlist.registry.commit must be a 40-character SHA")
        if not isinstance(source_sha256, str) or len(source_sha256) != 64:
            raise ManifestError("watchlist.registry.source_sha256 must be SHA-256")
    return WatchlistManifest(
        version=version,
        selection_as_of=_required_text(raw, "selection_as_of"),
        effective_at=effective_at,
        membership_policy=membership_policy,
        excluded_symbols=tuple(str(value) for value in excluded),
        instruments=instruments,
        registry=dict(registry),
    )


def load_watchlist_manifest(path: str | Path) -> WatchlistManifest:
    manifest_path = Path(path)
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ManifestError(f"manifest not found: {manifest_path}") from exc
    except json.JSONDecodeError as exc:
        raise ManifestError(f"manifest JSON is invalid: {manifest_path}") from exc
    return validate_watchlist_manifest(payload)


def watchlist_manifest_digest(manifest: WatchlistManifest | Mapping[str, Any]) -> str:
    validated = validate_watchlist_manifest(manifest)
    encoded = json.dumps(
        validated.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
