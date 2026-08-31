"""Executable mapping and explicit fallback policy for the 16 cross-market assets."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from kline.market_calendar import CalendarError, resolve_calendar
from kline.mvp_manifest import ManifestError, MvpManifest, validate_manifest


EXPECTED_CROSS_MARKET_IDS = (
    "US.INDEX.SPX",
    "US.INDEX.NDX",
    "US.ETF.UUP",
    "US.ETF.SCHD",
    "US.INDEX.VIX",
    "CRYPTO.PERP.BTC",
    "CRYPTO.PERP.ETH",
    "CRYPTO.PERP.HYPE",
    "CN.INDEX.SHCOMP",
    "CN.INDEX.STAR50",
    "CN.INDEX.DIVIDEND",
    "JP.INDEX.N225",
    "KR.INDEX.KOSPI",
    "US.FUTURE.WTI",
    "US.FUTURE.GOLD",
    "US.FUTURE.SILVER",
)

_EXPECTED_BINDINGS = {
    "US.INDEX.SPX": ("SPX", "SPX", "index"),
    "US.INDEX.NDX": ("NDX", "NDX", "index"),
    "US.ETF.UUP": ("DXY", "UUP", "etf"),
    "US.ETF.SCHD": ("SCHD", "SCHD", "etf"),
    "US.INDEX.VIX": ("VIX", "VIX", "index"),
    "CRYPTO.PERP.BTC": ("BTC", "BTC", "crypto_perpetual"),
    "CRYPTO.PERP.ETH": ("ETH", "ETH", "crypto_perpetual"),
    "CRYPTO.PERP.HYPE": ("HYPE", "HYPE", "crypto_perpetual"),
    "CN.INDEX.SHCOMP": ("sh000001", "sh000001", "index"),
    "CN.INDEX.STAR50": ("sh000688", "sh000688", "index"),
    "CN.INDEX.DIVIDEND": ("sh000015", "sh000015", "index"),
    "JP.INDEX.N225": ("^N225", "^N225", "index"),
    "KR.INDEX.KOSPI": ("^KS11", "^KS11", "index"),
    "US.FUTURE.WTI": ("CL=F", "CL=F", "continuous_future"),
    "US.FUTURE.GOLD": ("GC=F", "GC=F", "continuous_future"),
    "US.FUTURE.SILVER": ("SI=F", "SI=F", "continuous_future"),
}
_EXPLICIT_FALLBACKS = {
    "CN.INDEX.SHCOMP": ("sina_index",),
    "CN.INDEX.STAR50": ("sina_index",),
    "CN.INDEX.DIVIDEND": ("sina_index",),
}


@dataclass(frozen=True)
class CrossMarketMapping:
    instrument_id: str
    display_symbol: str
    source_id: str
    provider_symbol: str
    asset_class: str
    security_type: str
    required_timeframes: tuple[str, ...]
    not_applicable_timeframes: tuple[str, ...]
    blocked_timeframes: tuple[str, ...]
    calendar_id: str
    timezone: str
    session_policy: str
    volume_semantics: str
    adjustment_basis: str
    metadata: Mapping[str, Any]

    @property
    def fallback_sources(self) -> tuple[str, ...]:
        values = self.metadata.get("fallback_sources", [])
        return tuple(values) if isinstance(values, list) else ()


def validate_cross_market(
    manifest: MvpManifest | Mapping[str, Any],
) -> tuple[CrossMarketMapping, ...]:
    """Validate exact IDs, identity semantics, calendars, and explicit fallbacks."""

    detached = validate_manifest(manifest)
    items = [item for item in detached.instruments if item.universe == "cross_market"]
    if tuple(item.instrument_id for item in items) != EXPECTED_CROSS_MARKET_IDS:
        raise ManifestError(
            "cross-market manifest IDs/order do not match the approved 16-instrument roster"
        )
    mappings: list[CrossMarketMapping] = []
    for item in items:
        expected = _EXPECTED_BINDINGS[item.instrument_id]
        if (item.display_symbol, item.provider_symbol, item.security_type) != expected:
            raise ManifestError(f"{item.instrument_id} has an unexpected provider/identity mapping")
        if item.security_type == "index" and item.volume_semantics != "not_applicable":
            raise ManifestError(f"{item.instrument_id} index volume must be not_applicable")
        if item.instrument_id in {"US.INDEX.SPX", "US.INDEX.NDX"}:
            if item.metadata.get("index_identity") != "actual_index":
                raise ManifestError(f"{item.instrument_id} must declare actual_index identity")
            if item.provider_symbol in {"SPY", "QQQ"}:
                raise ManifestError(f"{item.instrument_id} cannot use an ETF proxy")
        if item.instrument_id == "US.ETF.UUP" and item.metadata.get("proxy_for") != "DXY":
            raise ManifestError("DXY mapping must explicitly identify UUP as its proxy")
        if item.security_type == "crypto_perpetual":
            if item.metadata.get("contract_type") != "perpetual":
                raise ManifestError(f"{item.instrument_id} must declare perpetual contract policy")
        if item.security_type == "continuous_future":
            if item.metadata.get("roll_policy") != "provider_continuous_contract":
                raise ManifestError(f"{item.instrument_id} must declare continuous roll policy")
        fallbacks = item.metadata.get("fallback_sources", [])
        if not isinstance(fallbacks, list) or any(
            not isinstance(source, str) or not source.strip() for source in fallbacks
        ):
            raise ManifestError(
                f"{item.instrument_id} fallback_sources must be a list of source IDs"
            )
        expected_fallbacks = _EXPLICIT_FALLBACKS.get(item.instrument_id, ())
        if tuple(fallbacks) != expected_fallbacks:
            raise ManifestError(
                f"{item.instrument_id} fallback policy is not explicit and approved"
            )
        if fallbacks and item.metadata.get("fallback_policy") != "explicit_only":
            raise ManifestError(f"{item.instrument_id} fallback policy must be explicit_only")
        try:
            resolve_calendar(item)
        except CalendarError as exc:
            raise ManifestError(f"{item.instrument_id} calendar mapping is invalid") from exc
        mappings.append(
            CrossMarketMapping(
                instrument_id=item.instrument_id,
                display_symbol=item.display_symbol,
                source_id=item.source_id,
                provider_symbol=item.provider_symbol,
                asset_class=item.asset_class,
                security_type=item.security_type,
                required_timeframes=item.required_timeframes,
                not_applicable_timeframes=item.not_applicable_timeframes,
                blocked_timeframes=item.blocked_timeframes,
                calendar_id=item.calendar_id,
                timezone=item.timezone,
                session_policy=item.session_policy,
                volume_semantics=item.volume_semantics,
                adjustment_basis=item.adjustment_basis,
                metadata=dict(item.metadata),
            )
        )
    return tuple(mappings)


class CrossMarketRegistry:
    """Read-only mapping registry; fallback selection is always caller-explicit."""

    def __init__(self, manifest: MvpManifest | Mapping[str, Any]) -> None:
        self._mappings = validate_cross_market(manifest)
        self._by_id = {mapping.instrument_id: mapping for mapping in self._mappings}

    def get(self, instrument_id: str) -> CrossMarketMapping:
        try:
            return self._by_id[instrument_id]
        except KeyError as exc:
            raise ManifestError(f"unknown cross-market instrument: {instrument_id}") from exc

    def fallback_sources(self, instrument_id: str) -> tuple[str, ...]:
        return self.get(instrument_id).fallback_sources

    def resolve_source(self, instrument_id: str, requested_source: str) -> str:
        mapping = self.get(instrument_id)
        if requested_source == mapping.source_id:
            return requested_source
        if requested_source in mapping.fallback_sources:
            return requested_source
        raise ManifestError(
            f"source {requested_source} is not an explicit fallback for {instrument_id}"
        )

    def request_plan(self) -> tuple[dict[str, Any], ...]:
        return tuple(
            {
                "instrument_id": mapping.instrument_id,
                "source_id": mapping.source_id,
                "provider_symbol": mapping.provider_symbol,
                "timeframes": list(mapping.required_timeframes),
                "blocked_timeframes": list(mapping.blocked_timeframes),
            }
            for mapping in self._mappings
        )
