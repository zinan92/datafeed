"""Executable manifest contract for the bounded Market Data Database MVP."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
import hashlib
import json
from pathlib import Path
from copy import deepcopy
from typing import Any, Mapping


MVP_MANIFEST_VERSION = "mvp_universe_v1"
ALLOWED_TIMEFRAMES = ("15m", "1h", "4h", "1d", "1w")
EXPECTED_COUNTS = {"a_share": 100, "us_stock": 100, "cross_market": 16}
LEGACY_EXCLUSIONS = {
    "treasury_symbols": ["DGS2", "DGS10", "T10Y2Y"],
    "timeframes": ["30m"],
}
_UNIVERSES = frozenset(EXPECTED_COUNTS)
_VOLUME_SEMANTICS = frozenset({"traded", "quote_derived", "not_applicable"})
_SECURITY_TYPES = frozenset(
    {
        "common_stock",
        "adr",
        "ads",
        "foreign_common",
        "index",
        "etf",
        "crypto_perpetual",
        "continuous_future",
    }
)
_FORBIDDEN_STOCK_TYPES = frozenset(
    {"etf", "fund", "index", "warrant", "preferred_stock", "preferred"}
)
_SOURCE_STATUS = frozenset({"configured", "blocked_for_entitlement"})
_RESERVE_STATUS = frozenset({"eligible", "quarantined"})
_RESERVE_IDENTITY_FIELDS = (
    "universe",
    "instrument_id",
    "display_symbol",
    "display_name",
    "asset_class",
    "security_type",
    "source_id",
    "provider_symbol",
    "required_timeframes",
    "not_applicable_timeframes",
    "blocked_timeframes",
    "calendar_id",
    "timezone",
    "session_policy",
    "volume_semantics",
    "adjustment_basis",
    "aggregation_rule_version",
    "source_status",
    "share_class",
    "issuer_id",
    "adr_ratio",
    "venue",
    "venue_valid_from",
    "venue_valid_to",
    "ticker_aliases",
    "ticker_alias_validity",
)
SELECTION_RECEIPT_FIELDS = (
    "manifest_version",
    "manifest_hash",
    "status",
    "selection_as_of",
    "effective_at",
    "universe",
    "snapshot_url",
    "snapshot_hash",
    "window",
    "thresholds",
    "selected",
    "rejected",
    "replacements",
    "reason",
)
ACTIVATION_RECEIPT_FIELDS = ("manifest_version", "manifest_hash", "effective_at")
RUN_RECEIPT_FIELDS = (
    "run_id",
    "status",
    "manifest_version",
    "manifest_hash",
    "completed_at",
    "coverage",
    "quality",
    "storage",
    "sources",
)


class ManifestError(ValueError):
    """The executable MVP manifest violates its public contract."""


def _parse_temporal(value: str, *, field_name: str, index: int) -> datetime:
    text = value.strip()
    try:
        if len(text) == 10:
            parsed = datetime.combine(date.fromisoformat(text), time.min)
        else:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ManifestError(f"instrument[{index}].{field_name} must be an ISO date/time") from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _canonical_identity(value: str) -> str:
    """Normalize identity values before uniqueness and replacement checks."""

    return value.strip().casefold()


def _source_registry() -> Mapping[str, Any]:
    """Return the live source registry, including registered plugin manifests."""

    try:
        from kline.provenance import all_source_manifests
    except ImportError:
        return {}
    return all_source_manifests()


def _source_asset_class(source: Any) -> str | None:
    value = getattr(source, "asset_class", None)
    return getattr(value, "value", value)


def _source_supports_timeframe(source: Any, provider_symbol: str, timeframe: str) -> bool:
    try:
        from kline.models import Timeframe

        return bool(source.supports_timeframe(provider_symbol, Timeframe(timeframe)))
    except (AttributeError, ValueError, TypeError):
        return False


def _reserve_status(record: Mapping[str, Any]) -> str:
    status = record.get("status")
    if status is None:
        return "eligible" if record.get("pre_screened") is True else "quarantined"
    return str(status).strip().casefold()


def _instrument_identity(item: ManifestInstrument | Mapping[str, Any]) -> dict[str, Any]:
    raw = item.to_dict() if isinstance(item, ManifestInstrument) else dict(item)
    return {field_name: raw.get(field_name) for field_name in _RESERVE_IDENTITY_FIELDS}


def _canonical_sequence(values: Any) -> tuple[str, ...] | None:
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        return None
    return tuple(_canonical_identity(value) for value in values)


def _identity_values_match(expected: Any, actual: Any, *, field_name: str) -> bool:
    if field_name in {
        "required_timeframes",
        "not_applicable_timeframes",
        "blocked_timeframes",
        "ticker_aliases",
    }:
        return _canonical_sequence(expected) == _canonical_sequence(actual)
    if field_name == "ticker_alias_validity":
        if not isinstance(expected, Mapping) or not isinstance(actual, Mapping):
            return False
        expected_keys = {_canonical_identity(str(key)) for key in expected}
        actual_keys = {_canonical_identity(str(key)) for key in actual}
        if expected_keys != actual_keys:
            return False
        return all(
            expected.get(expected_key) == actual.get(actual_key)
            for expected_key, actual_key in zip(
                sorted(expected, key=_canonical_identity),
                sorted(actual, key=_canonical_identity),
            )
        )
    if isinstance(expected, str) and isinstance(actual, str):
        return _canonical_identity(expected) == _canonical_identity(actual)
    return expected == actual


def _parse_alias_validity(raw: Any, *, index: int) -> dict[str, dict[str, str | None]]:
    if not isinstance(raw, Mapping):
        raise ManifestError(f"instrument[{index}].ticker_alias_validity must be an object")
    parsed: dict[str, dict[str, str | None]] = {}
    seen_aliases: set[str] = set()
    for alias, validity in raw.items():
        if not isinstance(alias, str) or not alias.strip() or not isinstance(validity, Mapping):
            raise ManifestError(f"instrument[{index}].ticker_alias_validity is invalid")
        alias_key = _canonical_identity(alias)
        if alias_key in seen_aliases:
            raise ManifestError(
                f"instrument[{index}].ticker_alias_validity contains duplicate aliases"
            )
        seen_aliases.add(alias_key)
        values: dict[str, str | None] = {}
        for field_name in ("valid_from", "valid_to"):
            value = validity.get(field_name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ManifestError(
                    f"instrument[{index}].ticker_alias_validity.{field_name} is invalid"
                )
            values[field_name] = value.strip() if isinstance(value, str) else None
            if values[field_name] is not None:
                _parse_temporal(values[field_name] or "", field_name=field_name, index=index)
        if values["valid_from"] and values["valid_to"]:
            if _parse_temporal(
                values["valid_from"], field_name="valid_from", index=index
            ) > _parse_temporal(values["valid_to"], field_name="valid_to", index=index):
                raise ManifestError(f"instrument[{index}].ticker_alias_validity has reversed dates")
        parsed[alias.strip()] = values
    return parsed


@dataclass(frozen=True)
class ManifestInstrument:
    universe: str
    instrument_id: str
    display_symbol: str
    display_name: str
    asset_class: str
    security_type: str
    source_id: str
    provider_symbol: str
    required_timeframes: tuple[str, ...]
    not_applicable_timeframes: tuple[str, ...]
    blocked_timeframes: tuple[str, ...]
    calendar_id: str
    timezone: str
    session_policy: str
    volume_semantics: str
    adjustment_basis: str
    aggregation_rule_version: str
    source_status: str
    share_class: str | None = None
    issuer_id: str | None = None
    adr_ratio: str | None = None
    venue: str | None = None
    venue_valid_from: str | None = None
    venue_valid_to: str | None = None
    ticker_aliases: tuple[str, ...] = ()
    ticker_alias_validity: dict[str, dict[str, str | None]] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any], *, index: int) -> "ManifestInstrument":
        if not isinstance(raw, Mapping):
            raise ManifestError(f"instrument[{index}] must be an object")

        def required_text(name: str) -> str:
            value = raw.get(name)
            if not isinstance(value, str) or not value.strip():
                raise ManifestError(f"instrument[{index}].{name} must be a non-empty string")
            return value.strip()

        def optional_text(name: str) -> str | None:
            value = raw.get(name)
            if value is None:
                return None
            if not isinstance(value, str) or not value.strip():
                raise ManifestError(
                    f"instrument[{index}].{name} must be a non-empty string or null"
                )
            return value.strip()

        def timeframe_list(name: str) -> tuple[str, ...]:
            value = raw.get(name)
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise ManifestError(f"instrument[{index}].{name} must be a list of strings")
            if len(set(value)) != len(value):
                raise ManifestError(f"instrument[{index}].{name} contains duplicates")
            return tuple(value)

        aliases = raw.get("ticker_aliases", [])
        if not isinstance(aliases, list) or not all(
            isinstance(item, str) and item.strip() for item in aliases
        ):
            raise ManifestError(
                f"instrument[{index}].ticker_aliases must be a list of non-empty strings"
            )
        alias_keys = [_canonical_identity(item) for item in aliases]
        if len(alias_keys) != len(set(alias_keys)):
            raise ManifestError(f"instrument[{index}].ticker_aliases contains duplicates")
        metadata = raw.get("metadata", {})
        if not isinstance(metadata, Mapping):
            raise ManifestError(f"instrument[{index}].metadata must be an object")

        return cls(
            universe=required_text("universe"),
            instrument_id=required_text("instrument_id"),
            display_symbol=required_text("display_symbol"),
            display_name=required_text("display_name"),
            asset_class=required_text("asset_class"),
            security_type=required_text("security_type"),
            source_id=required_text("source_id"),
            provider_symbol=required_text("provider_symbol"),
            required_timeframes=timeframe_list("required_timeframes"),
            not_applicable_timeframes=timeframe_list("not_applicable_timeframes"),
            blocked_timeframes=timeframe_list("blocked_timeframes"),
            calendar_id=required_text("calendar_id"),
            timezone=required_text("timezone"),
            session_policy=required_text("session_policy"),
            volume_semantics=required_text("volume_semantics"),
            adjustment_basis=required_text("adjustment_basis"),
            aggregation_rule_version=required_text("aggregation_rule_version"),
            source_status=required_text("source_status"),
            share_class=optional_text("share_class"),
            issuer_id=optional_text("issuer_id"),
            adr_ratio=optional_text("adr_ratio"),
            venue=optional_text("venue"),
            venue_valid_from=optional_text("venue_valid_from"),
            venue_valid_to=optional_text("venue_valid_to"),
            ticker_aliases=tuple(item.strip() for item in aliases),
            ticker_alias_validity=_parse_alias_validity(
                raw.get("ticker_alias_validity", {}), index=index
            ),
            metadata=dict(metadata),
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "universe": self.universe,
            "instrument_id": self.instrument_id,
            "display_symbol": self.display_symbol,
            "display_name": self.display_name,
            "asset_class": self.asset_class,
            "security_type": self.security_type,
            "source_id": self.source_id,
            "provider_symbol": self.provider_symbol,
            "required_timeframes": list(self.required_timeframes),
            "not_applicable_timeframes": list(self.not_applicable_timeframes),
            "blocked_timeframes": list(self.blocked_timeframes),
            "calendar_id": self.calendar_id,
            "timezone": self.timezone,
            "session_policy": self.session_policy,
            "volume_semantics": self.volume_semantics,
            "adjustment_basis": self.adjustment_basis,
            "aggregation_rule_version": self.aggregation_rule_version,
            "source_status": self.source_status,
            "share_class": self.share_class,
            "issuer_id": self.issuer_id,
            "adr_ratio": self.adr_ratio,
            "venue": self.venue,
            "venue_valid_from": self.venue_valid_from,
            "venue_valid_to": self.venue_valid_to,
            "ticker_aliases": list(self.ticker_aliases),
            "ticker_alias_validity": {
                alias: dict(validity)
                for alias, validity in sorted(self.ticker_alias_validity.items())
            },
            "metadata": deepcopy(self.metadata),
        }
        return result


@dataclass(frozen=True)
class MvpManifest:
    version: str
    selection_as_of: str
    effective_at: str | None
    instruments: tuple[ManifestInstrument, ...]
    reserves: dict[str, tuple[str, ...]]
    reserve_records: dict[str, tuple[dict[str, Any], ...]]
    legacy_exclusions: dict[str, list[str]]
    selection_policy: dict[str, Any]
    legacy_namespace: dict[str, Any] = field(
        default_factory=lambda: {"name": "legacy", "read_only": True, "write_enabled": False}
    )
    selection_receipt_schema: tuple[str, ...] = ()

    @property
    def counts(self) -> dict[str, int]:
        counts = {key: 0 for key in EXPECTED_COUNTS}
        for item in self.instruments:
            counts[item.universe] = counts.get(item.universe, 0) + 1
        return counts

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "selection_as_of": self.selection_as_of,
            "effective_at": self.effective_at,
            "instruments": [item.to_dict() for item in self.instruments],
            "reserves": {key: list(value) for key, value in sorted(self.reserves.items())},
            "reserve_records": {
                key: [deepcopy(record) for record in value]
                for key, value in sorted(self.reserve_records.items())
            },
            "legacy_exclusions": {
                "treasury_symbols": list(self.legacy_exclusions["treasury_symbols"]),
                "timeframes": list(self.legacy_exclusions["timeframes"]),
            },
            "selection_policy": deepcopy(self.selection_policy),
            "legacy_namespace": deepcopy(self.legacy_namespace),
            "selection_receipt_schema": list(self.selection_receipt_schema),
        }


def _validate_instrument(item: ManifestInstrument, *, index: int) -> None:
    if item.universe not in _UNIVERSES:
        raise ManifestError(f"instrument[{index}] has unknown universe: {item.universe}")
    if item.security_type not in _SECURITY_TYPES:
        raise ManifestError(f"instrument[{index}] has unknown security_type: {item.security_type}")
    if item.volume_semantics not in _VOLUME_SEMANTICS:
        raise ManifestError(
            f"instrument[{index}] has unknown volume_semantics: {item.volume_semantics}"
        )
    if item.source_status not in _SOURCE_STATUS:
        raise ManifestError(f"instrument[{index}] has unknown source_status: {item.source_status}")
    if item.source_status == "blocked_for_entitlement" and not item.source_id:
        raise ManifestError(f"instrument[{index}] blocked source is missing source_id")
    if item.source_status == "blocked_for_entitlement" and item.required_timeframes:
        raise ManifestError(
            f"instrument[{index}] blocked_for_entitlement cannot declare required timeframes"
        )
    unsupported_required = sorted(set(item.required_timeframes) - set(ALLOWED_TIMEFRAMES))
    if unsupported_required:
        raise ManifestError(
            f"instrument[{index}] has unsupported required timeframe: {', '.join(unsupported_required)}"
        )
    unsupported_na = sorted(set(item.not_applicable_timeframes) - set(ALLOWED_TIMEFRAMES))
    if unsupported_na:
        raise ManifestError(
            f"instrument[{index}] has unsupported not_applicable timeframe: {', '.join(unsupported_na)}"
        )
    if set(item.required_timeframes).intersection(item.not_applicable_timeframes):
        raise ManifestError(f"instrument[{index}] has overlapping timeframe states")
    timeframe_states = (
        set(item.required_timeframes)
        | set(item.not_applicable_timeframes)
        | set(item.blocked_timeframes)
    )
    if set(item.required_timeframes).intersection(item.blocked_timeframes):
        raise ManifestError(
            f"instrument[{index}] has overlapping required/blocked timeframe states"
        )
    if set(item.not_applicable_timeframes).intersection(item.blocked_timeframes):
        raise ManifestError(
            f"instrument[{index}] has overlapping not_applicable/blocked timeframe states"
        )
    if timeframe_states != set(ALLOWED_TIMEFRAMES):
        raise ManifestError(f"instrument[{index}] must classify every MVP timeframe")
    excluded_symbols = {symbol.upper() for symbol in LEGACY_EXCLUSIONS["treasury_symbols"]}
    identity_symbols = {
        item.display_symbol.upper(),
        item.provider_symbol.upper(),
        *(alias.upper() for alias in item.ticker_aliases),
    }
    if excluded_symbols.intersection(identity_symbols):
        raise ManifestError(f"instrument[{index}] includes excluded Treasury symbol")
    if item.universe in {"a_share", "us_stock"} and item.security_type in _FORBIDDEN_STOCK_TYPES:
        raise ManifestError(f"instrument[{index}] stock universe contains forbidden security type")
    if item.universe == "a_share" and item.security_type != "common_stock":
        raise ManifestError(f"instrument[{index}] A-share must be common_stock")
    if item.universe == "us_stock" and item.security_type not in {
        "common_stock",
        "adr",
        "ads",
        "foreign_common",
    }:
        raise ManifestError(f"instrument[{index}] US stock has invalid security type")
    if item.security_type in {"adr", "ads"} and not item.adr_ratio:
        raise ManifestError(f"instrument[{index}] ADR/ADS requires adr_ratio")
    if "30m" in item.required_timeframes or "30m" in item.not_applicable_timeframes:
        raise ManifestError(f"instrument[{index}] uses forbidden 30m timeframe")
    if "30m" in item.blocked_timeframes:
        raise ManifestError(f"instrument[{index}] uses forbidden 30m timeframe")
    alias_keys = {_canonical_identity(alias) for alias in item.ticker_aliases}
    validity_keys = {_canonical_identity(alias) for alias in item.ticker_alias_validity}
    if validity_keys - alias_keys:
        raise ManifestError(f"instrument[{index}] has validity for an undeclared ticker alias")
    venue_from = None
    venue_to = None
    if item.venue_valid_from is not None:
        venue_from = _parse_temporal(
            item.venue_valid_from, field_name="venue_valid_from", index=index
        )
    if item.venue_valid_to is not None:
        venue_to = _parse_temporal(item.venue_valid_to, field_name="venue_valid_to", index=index)
    if venue_from is not None and venue_to is not None and venue_from > venue_to:
        raise ManifestError(f"instrument[{index}].venue_valid_from is after venue_valid_to")
    if item.universe == "a_share" and item.asset_class != "a_share":
        raise ManifestError(f"instrument[{index}] A-share asset_class mismatch")
    if item.universe == "us_stock" and item.asset_class != "us_stock":
        raise ManifestError(f"instrument[{index}] US stock asset_class mismatch")
    if item.universe == "cross_market" and item.asset_class not in {
        "index",
        "etf",
        "crypto",
        "commodity",
    }:
        raise ManifestError(f"instrument[{index}] cross-market asset_class is invalid")
    if item.source_status == "configured":
        source = _source_registry().get(item.source_id)
        if source is None:
            raise ManifestError(
                f"instrument[{index}] configured source is not registered: {item.source_id}"
            )
        source_asset_class = _source_asset_class(source)
        if source_asset_class != item.asset_class:
            raise ManifestError(
                f"instrument[{index}] source {item.source_id} serves {source_asset_class}, "
                f"not {item.asset_class}"
            )
        for timeframe in item.required_timeframes:
            if not _source_supports_timeframe(source, item.provider_symbol, timeframe):
                raise ManifestError(
                    f"instrument[{index}] source {item.source_id} does not support "
                    f"{item.provider_symbol} at {timeframe}"
                )


def validate_manifest(payload: Mapping[str, Any] | MvpManifest) -> MvpManifest:
    """Validate and detach an executable manifest from an input mapping."""

    if isinstance(payload, MvpManifest):
        return validate_manifest(payload.to_dict())
    if not isinstance(payload, Mapping):
        raise ManifestError("manifest must be an object")
    version = payload.get("version")
    if not isinstance(version, str) or not (
        version == MVP_MANIFEST_VERSION
        or (version.startswith(f"{MVP_MANIFEST_VERSION}.") and version.rsplit(".", 1)[-1].isdigit())
    ):
        raise ManifestError(f"manifest version must start with {MVP_MANIFEST_VERSION}")
    selection_as_of = payload.get("selection_as_of")
    if not isinstance(selection_as_of, str) or not selection_as_of.strip():
        raise ManifestError("selection_as_of must be a non-empty string")
    _parse_temporal(selection_as_of, field_name="selection_as_of", index=-1)
    effective_at = payload.get("effective_at")
    if effective_at is not None and (not isinstance(effective_at, str) or not effective_at.strip()):
        raise ManifestError("effective_at must be a non-empty string or null")
    if effective_at is not None:
        _parse_temporal(effective_at, field_name="effective_at", index=-1)
    raw_instruments = payload.get("instruments")
    if not isinstance(raw_instruments, list):
        raise ManifestError("instruments must be a list")

    instruments = tuple(
        ManifestInstrument.from_dict(raw, index=index) for index, raw in enumerate(raw_instruments)
    )
    if len(instruments) != sum(EXPECTED_COUNTS.values()):
        raise ManifestError(f"manifest must contain {sum(EXPECTED_COUNTS.values())} instruments")
    for index, item in enumerate(instruments):
        _validate_instrument(item, index=index)
    ids = [item.instrument_id for item in instruments]
    if len(ids) != len(set(ids)):
        raise ManifestError("instrument_id values must be unique")
    scoped_symbols = [
        (item.universe, _canonical_identity(item.display_symbol)) for item in instruments
    ]
    if len(scoped_symbols) != len(set(scoped_symbols)):
        raise ManifestError("display symbols must be unique within each universe")
    provider_keys = [
        (
            _canonical_identity(item.source_id),
            _canonical_identity(item.provider_symbol),
            _canonical_identity(item.asset_class),
        )
        for item in instruments
    ]
    if len(provider_keys) != len(set(provider_keys)):
        raise ManifestError("provider source/symbol/asset_class keys must be unique")
    identity_owners: dict[tuple[str, str], str] = {}
    for item in instruments:
        values = (item.display_symbol, item.provider_symbol, *item.ticker_aliases)
        for value in values:
            key = (item.universe, _canonical_identity(value))
            previous = identity_owners.get(key)
            if previous is not None and previous != item.instrument_id:
                raise ManifestError(
                    f"instrument identity collides across display/provider/alias values: {value}"
                )
            identity_owners[key] = item.instrument_id
    counts = {key: sum(item.universe == key for item in instruments) for key in EXPECTED_COUNTS}
    if counts != EXPECTED_COUNTS:
        raise ManifestError(f"manifest counts must be {EXPECTED_COUNTS}, got {counts}")

    raw_reserves = payload.get("reserves")
    if not isinstance(raw_reserves, Mapping):
        raise ManifestError("reserves must be an object")
    reserves: dict[str, tuple[str, ...]] = {}
    active_symbols = {
        (item.universe, _canonical_identity(item.display_symbol)) for item in instruments
    }
    for universe in ("a_share", "us_stock"):
        values = raw_reserves.get(universe)
        if not isinstance(values, list) or not all(
            isinstance(value, str) and value.strip() for value in values
        ):
            raise ManifestError(f"reserves.{universe} must be a list of non-empty strings")
        normalized = tuple(value.strip() for value in values)
        if len(normalized) < 20:
            raise ManifestError(f"reserves.{universe} must contain at least 20 candidates")
        normalized_keys = [_canonical_identity(value) for value in normalized]
        if len(set(normalized_keys)) != len(normalized_keys):
            raise ManifestError(f"reserves.{universe} contains duplicates")
        if any((universe, _canonical_identity(value)) in active_symbols for value in normalized):
            raise ManifestError(f"reserves.{universe} overlaps active manifest")
        reserves[universe] = normalized

    raw_reserve_records = payload.get("reserve_records")
    if not isinstance(raw_reserve_records, Mapping):
        raise ManifestError("reserve_records must be an object")
    reserve_records: dict[str, tuple[dict[str, Any], ...]] = {}
    for universe in ("a_share", "us_stock"):
        raw_records = raw_reserve_records.get(universe)
        if not isinstance(raw_records, list) or len(raw_records) != len(reserves[universe]):
            raise ManifestError(f"reserve_records.{universe} must match reserves in length")
        parsed_records: list[dict[str, Any]] = []
        reserve_identity_ids: set[str] = set()
        reserve_identity_owners: dict[str, str] = {}
        for rank, raw_record in enumerate(raw_records, start=1):
            if not isinstance(raw_record, Mapping):
                raise ManifestError(f"reserve_records.{universe}[{rank - 1}] must be an object")
            required = (
                "display_symbol",
                "display_name",
                "provider_symbol",
                "source_id",
                "selection_rank",
            )
            if any(
                not isinstance(raw_record.get(field_name), str)
                or not raw_record[field_name].strip()
                for field_name in required[:-1]
            ):
                raise ManifestError(f"reserve_records.{universe}[{rank - 1}] has missing identity")
            raw_identity = raw_record.get("identity")
            if not isinstance(raw_identity, Mapping) or any(
                field_name not in raw_identity for field_name in _RESERVE_IDENTITY_FIELDS
            ):
                raise ManifestError(
                    f"reserve_records.{universe}[{rank - 1}] identity is incomplete"
                )
            identity = dict(raw_identity)
            if any(
                not isinstance(identity[field_name], str) or not identity[field_name].strip()
                for field_name in (
                    "instrument_id",
                    "universe",
                    "display_symbol",
                    "display_name",
                    "asset_class",
                    "security_type",
                    "source_id",
                    "provider_symbol",
                    "calendar_id",
                    "timezone",
                    "session_policy",
                    "volume_semantics",
                    "adjustment_basis",
                    "aggregation_rule_version",
                    "source_status",
                )
            ):
                raise ManifestError(
                    f"reserve_records.{universe}[{rank - 1}] identity has invalid text"
                )
            if identity["display_symbol"] != raw_record["display_symbol"]:
                raise ManifestError(
                    f"reserve_records.{universe}[{rank - 1}] identity display_symbol differs"
                )
            if identity["display_name"] != raw_record["display_name"]:
                raise ManifestError(
                    f"reserve_records.{universe}[{rank - 1}] identity display_name differs"
                )
            if identity["provider_symbol"] != raw_record["provider_symbol"]:
                raise ManifestError(
                    f"reserve_records.{universe}[{rank - 1}] identity provider_symbol differs"
                )
            if identity["source_id"] != raw_record["source_id"]:
                raise ManifestError(
                    f"reserve_records.{universe}[{rank - 1}] identity source_id differs"
                )
            if identity["asset_class"] != universe:
                raise ManifestError(
                    f"reserve_records.{universe}[{rank - 1}] identity asset_class differs"
                )
            if identity["universe"] != universe:
                raise ManifestError(
                    f"reserve_records.{universe}[{rank - 1}] identity universe differs"
                )
            for timeframe_field in (
                "required_timeframes",
                "not_applicable_timeframes",
                "blocked_timeframes",
            ):
                values = _canonical_sequence(identity[timeframe_field])
                if (
                    values is None
                    or "30m" in values
                    or not set(values).issubset(ALLOWED_TIMEFRAMES)
                ):
                    raise ManifestError(
                        f"reserve_records.{universe}[{rank - 1}] identity has invalid timeframes"
                    )
            identity_timeframes = (
                set(identity["required_timeframes"])
                | set(identity["not_applicable_timeframes"])
                | set(identity["blocked_timeframes"])
            )
            if (
                identity_timeframes != set(ALLOWED_TIMEFRAMES)
                or set(identity["required_timeframes"]).intersection(
                    identity["not_applicable_timeframes"]
                )
                or set(identity["required_timeframes"]).intersection(identity["blocked_timeframes"])
                or set(identity["not_applicable_timeframes"]).intersection(
                    identity["blocked_timeframes"]
                )
            ):
                raise ManifestError(
                    f"reserve_records.{universe}[{rank - 1}] identity timeframe states overlap"
                )
            if identity["source_status"] not in _SOURCE_STATUS:
                raise ManifestError(
                    f"reserve_records.{universe}[{rank - 1}] identity source_status is invalid"
                )
            if (
                identity["source_status"] == "blocked_for_entitlement"
                and identity["required_timeframes"]
            ):
                raise ManifestError(
                    f"reserve_records.{universe}[{rank - 1}] blocked identity cannot be required"
                )
            aliases = _canonical_sequence(identity["ticker_aliases"])
            if aliases is None or len(aliases) != len(set(aliases)):
                raise ManifestError(
                    f"reserve_records.{universe}[{rank - 1}] identity aliases are invalid"
                )
            _parse_alias_validity(identity["ticker_alias_validity"], index=rank - 1)
            for date_field in ("venue_valid_from", "venue_valid_to"):
                if identity[date_field] is not None:
                    if (
                        not isinstance(identity[date_field], str)
                        or not identity[date_field].strip()
                    ):
                        raise ManifestError(
                            f"reserve_records.{universe}[{rank - 1}] identity has invalid venue dates"
                        )
                    _parse_temporal(identity[date_field], field_name=date_field, index=rank - 1)
            if identity["venue_valid_from"] and identity["venue_valid_to"]:
                if _parse_temporal(
                    identity["venue_valid_from"], field_name="venue_valid_from", index=rank - 1
                ) > _parse_temporal(
                    identity["venue_valid_to"], field_name="venue_valid_to", index=rank - 1
                ):
                    raise ManifestError(
                        f"reserve_records.{universe}[{rank - 1}] identity has reversed venue dates"
                    )
            try:
                reserve_item = ManifestInstrument.from_dict(identity, index=rank - 1)
                _validate_instrument(reserve_item, index=rank - 1)
            except ManifestError as exc:
                raise ManifestError(
                    f"reserve_records.{universe}[{rank - 1}] identity violates instrument contract"
                ) from exc
            identity_id = _canonical_identity(identity["instrument_id"])
            if identity_id in reserve_identity_ids:
                raise ManifestError(
                    f"reserve_records.{universe} identity instrument_id values must be unique"
                )
            reserve_identity_ids.add(identity_id)
            for value in (
                identity["display_symbol"],
                identity["provider_symbol"],
                *identity["ticker_aliases"],
            ):
                identity_key = _canonical_identity(value)
                previous_owner = reserve_identity_owners.get(identity_key)
                if previous_owner is not None and previous_owner != identity["instrument_id"]:
                    raise ManifestError(
                        f"reserve_records.{universe} identity values must be unique"
                    )
                if (universe, identity_key) in identity_owners:
                    raise ManifestError(
                        f"reserve_records.{universe} identity overlaps active manifest"
                    )
                reserve_identity_owners[identity_key] = identity["instrument_id"]
            if raw_record.get("selection_rank") != rank:
                raise ManifestError(f"reserve_records.{universe} selection_rank must be contiguous")
            pre_screened = raw_record.get("pre_screened")
            if not isinstance(pre_screened, bool):
                raise ManifestError(
                    f"reserve_records.{universe}[{rank - 1}].pre_screened must be boolean"
                )
            status = _reserve_status(raw_record)
            if status not in _RESERVE_STATUS:
                raise ManifestError(f"reserve_records.{universe}[{rank - 1}] has invalid status")
            if status == "eligible" and pre_screened is not True:
                raise ManifestError(
                    f"reserve_records.{universe}[{rank - 1}] eligible records must be pre_screened"
                )
            if status == "quarantined":
                if pre_screened is not False:
                    raise ManifestError(
                        f"reserve_records.{universe}[{rank - 1}] quarantined records must not be pre_screened"
                    )
                quarantine_reason = raw_record.get("quarantine_reason")
                if not isinstance(quarantine_reason, str) or not quarantine_reason.strip():
                    raise ManifestError(
                        f"reserve_records.{universe}[{rank - 1}] quarantined records need a reason"
                    )
            if _canonical_identity(raw_record["display_symbol"]) != _canonical_identity(
                reserves[universe][rank - 1]
            ):
                raise ManifestError(f"reserve_records.{universe} order differs from reserves")
            if (
                parsed_records
                and _reserve_status(parsed_records[-1]) == "quarantined"
                and status == "eligible"
            ):
                raise ManifestError(
                    f"reserve_records.{universe} eligible order cannot follow quarantined records"
                )
            parsed_records.append(dict(raw_record))
        reserve_records[universe] = tuple(parsed_records)

    exclusions = payload.get("legacy_exclusions")
    if exclusions != LEGACY_EXCLUSIONS:
        raise ManifestError(f"legacy_exclusions must equal {LEGACY_EXCLUSIONS}")
    legacy_namespace = payload.get("legacy_namespace")
    if legacy_namespace != {"name": "legacy", "read_only": True, "write_enabled": False}:
        raise ManifestError("legacy_namespace must be an explicit read-only namespace")
    selection_policy = payload.get("selection_policy")
    if not isinstance(selection_policy, Mapping):
        raise ManifestError("selection_policy must be an object")
    required_policy = {"freeze_days_after_first_success", "live_snapshot_required", "rotation"}
    if not required_policy.issubset(selection_policy):
        raise ManifestError("selection_policy is missing required freeze/snapshot/rotation fields")
    receipt_schema = payload.get("selection_receipt_schema")
    if (
        not isinstance(receipt_schema, list)
        or not receipt_schema
        or not all(isinstance(item, str) and item.strip() for item in receipt_schema)
    ):
        raise ManifestError("selection_receipt_schema must list receipt fields")
    if tuple(receipt_schema) != SELECTION_RECEIPT_FIELDS:
        raise ManifestError("selection_receipt_schema does not match the public receipt contract")
    return MvpManifest(
        version=version,
        selection_as_of=selection_as_of.strip(),
        effective_at=effective_at.strip() if isinstance(effective_at, str) else None,
        instruments=instruments,
        reserves=reserves,
        reserve_records=reserve_records,
        legacy_exclusions={
            "treasury_symbols": list(LEGACY_EXCLUSIONS["treasury_symbols"]),
            "timeframes": list(LEGACY_EXCLUSIONS["timeframes"]),
        },
        selection_policy=dict(selection_policy),
        legacy_namespace=dict(legacy_namespace),
        selection_receipt_schema=tuple(receipt_schema),
    )


def build_selection_receipt(
    manifest: MvpManifest | Mapping[str, Any],
    *,
    universe: str,
    snapshot_url: str,
    snapshot_hash: str,
    window: str,
    thresholds: Mapping[str, Any],
    selected: list[str],
    rejected: list[str],
    replacements: list[Mapping[str, Any]],
    reason: str,
    status: str = "success",
) -> dict[str, Any]:
    """Build the stable receipt shape used by a manifest selection run."""

    detached = validate_manifest(manifest)
    if universe not in ("a_share", "us_stock"):
        raise ManifestError("selection receipt universe must be a_share or us_stock")
    text_fields = {
        "snapshot_url": snapshot_url,
        "snapshot_hash": snapshot_hash,
        "window": window,
        "reason": reason,
    }
    if any(not isinstance(value, str) or not value.strip() for value in text_fields.values()):
        raise ManifestError("selection receipt text fields must be non-empty strings")
    if len(snapshot_hash) != 64 or any(
        char not in "0123456789abcdefABCDEF" for char in snapshot_hash
    ):
        raise ManifestError("selection receipt snapshot_hash must be a SHA-256 hex digest")
    if not isinstance(thresholds, Mapping):
        raise ManifestError("selection receipt thresholds must be an object")
    if status != "success":
        raise ManifestError("selection receipt status must be success")
    if not all(isinstance(value, str) and value.strip() for value in selected + rejected):
        raise ManifestError("selection receipt selected/rejected values must be strings")
    if not isinstance(replacements, list) or not all(
        isinstance(item, Mapping) for item in replacements
    ):
        raise ManifestError("selection receipt replacements must be a list of objects")
    return {
        "manifest_version": detached.version,
        "manifest_hash": manifest_digest(detached),
        "status": status,
        "selection_as_of": detached.selection_as_of,
        "effective_at": detached.effective_at,
        "universe": universe,
        "snapshot_url": snapshot_url.strip(),
        "snapshot_hash": snapshot_hash.lower(),
        "window": window.strip(),
        "thresholds": dict(thresholds),
        "selected": list(selected),
        "rejected": list(rejected),
        "replacements": [dict(item) for item in replacements],
        "reason": reason.strip(),
    }


def _validate_selection_receipt(
    manifest: MvpManifest, receipt: Mapping[str, Any], *, require_complete: bool = False
) -> dict[str, Any]:
    """Validate that a selection receipt is evidence for this exact manifest."""

    if not isinstance(receipt, Mapping):
        raise ManifestError("selection_receipt must be an object")
    missing = [field for field in SELECTION_RECEIPT_FIELDS if field not in receipt]
    if missing:
        raise ManifestError(f"selection_receipt is missing fields: {', '.join(missing)}")
    expected_hash = manifest_digest(manifest)
    if receipt.get("manifest_version") != manifest.version:
        raise ManifestError("selection_receipt manifest_version does not match manifest")
    if receipt.get("manifest_hash") != expected_hash:
        raise ManifestError("selection_receipt manifest_hash does not match manifest")
    if receipt.get("status") != "success":
        raise ManifestError("selection_receipt must have status=success before activation")
    if receipt.get("selection_as_of") != manifest.selection_as_of:
        raise ManifestError("selection_receipt selection_as_of does not match manifest")
    if receipt.get("effective_at") != manifest.effective_at:
        raise ManifestError("selection_receipt effective_at does not match manifest")
    universe = receipt.get("universe")
    if universe not in ("a_share", "us_stock"):
        raise ManifestError("selection_receipt universe must be a_share or us_stock")
    text_fields = ("snapshot_url", "snapshot_hash", "window", "reason")
    if any(
        not isinstance(receipt.get(field), str) or not receipt[field].strip()
        for field in text_fields
    ):
        raise ManifestError("selection_receipt text fields must be non-empty strings")
    snapshot_hash = str(receipt["snapshot_hash"])
    if len(snapshot_hash) != 64 or any(
        char not in "0123456789abcdefABCDEF" for char in snapshot_hash
    ):
        raise ManifestError("selection_receipt snapshot_hash must be a SHA-256 hex digest")
    if not isinstance(receipt.get("thresholds"), Mapping):
        raise ManifestError("selection_receipt thresholds must be an object")
    for receipt_field in ("selected", "rejected", "replacements"):
        value = receipt.get(receipt_field)
        if not isinstance(value, list):
            raise ManifestError(f"selection_receipt {receipt_field} must be a list")
    if not all(
        isinstance(value, str) and value.strip()
        for value in receipt["selected"] + receipt["rejected"]
    ):
        raise ManifestError("selection_receipt selected/rejected values must be strings")
    selected_keys = [_canonical_identity(value) for value in receipt["selected"]]
    rejected_keys = [_canonical_identity(value) for value in receipt["rejected"]]
    if len(selected_keys) != len(set(selected_keys)) or len(rejected_keys) != len(
        set(rejected_keys)
    ):
        raise ManifestError("selection_receipt selected/rejected values must be unique")
    if set(selected_keys).intersection(rejected_keys):
        raise ManifestError("selection_receipt selected/rejected values must be disjoint")
    known_keys = {
        _canonical_identity(item.display_symbol)
        for item in manifest.instruments
        if item.universe == universe
    }
    known_keys.update(_canonical_identity(value) for value in manifest.reserves[universe])
    if any(value not in known_keys for value in selected_keys + rejected_keys):
        raise ManifestError("selection_receipt contains an unknown candidate")
    if not receipt["selected"]:
        raise ManifestError("selection_receipt must contain at least one selected candidate")
    if require_complete:
        active_keys = {
            _canonical_identity(item.display_symbol)
            for item in manifest.instruments
            if item.universe == universe
        }
        if set(selected_keys) != active_keys:
            raise ManifestError(
                "selection_receipt selected candidates must cover the complete active universe"
            )
    if not all(isinstance(value, Mapping) for value in receipt["replacements"]):
        raise ManifestError("selection_receipt replacements must be a list of objects")
    for replacement in receipt["replacements"]:
        failed = replacement.get("failed")
        selected = replacement.get("replacement")
        reason = replacement.get("reason")
        if not all(
            isinstance(value, str) and value.strip() for value in (failed, selected, reason)
        ):
            raise ManifestError("selection_receipt replacement identity is invalid")
        if (
            _canonical_identity(failed) not in known_keys
            or _canonical_identity(selected) not in known_keys
        ):
            raise ManifestError("selection_receipt replacement references an unknown candidate")
        if _canonical_identity(failed) not in rejected_keys:
            raise ManifestError("selection_receipt replacement failed member must be rejected")
        if _canonical_identity(selected) not in selected_keys:
            raise ManifestError("selection_receipt replacement member must be selected")
    return dict(receipt)


def _validate_run_receipt(manifest: MvpManifest, receipt: Mapping[str, Any]) -> None:
    """Validate the evidence-bearing success receipt emitted by the ingestion runner."""

    if not isinstance(receipt, Mapping):
        raise ManifestError("run_receipt must be an object")
    missing = [field for field in RUN_RECEIPT_FIELDS if field not in receipt]
    if missing:
        raise ManifestError(f"run_receipt is missing fields: {', '.join(missing)}")
    run_id = receipt.get("run_id")
    if not isinstance(run_id, str) or not run_id.strip():
        raise ManifestError("run_receipt run_id must be a non-empty string")
    if receipt.get("status") != "success":
        raise ManifestError("run_receipt must have status=success before activation")
    if receipt.get("manifest_version") != manifest.version:
        raise ManifestError("run_receipt manifest_version does not match manifest")
    if receipt.get("manifest_hash") != manifest_digest(manifest):
        raise ManifestError("run_receipt manifest_hash does not match manifest")
    completed_at = receipt.get("completed_at")
    if not isinstance(completed_at, str) or not completed_at.strip():
        raise ManifestError("run_receipt completed_at must be a non-empty ISO timestamp")
    _parse_temporal(completed_at, field_name="completed_at", index=-1)
    coverage = receipt.get("coverage")
    if not isinstance(coverage, Mapping):
        raise ManifestError("run_receipt coverage must be an object")
    if coverage.get("instrument_count") != len(manifest.instruments):
        raise ManifestError("run_receipt coverage instrument_count does not match manifest")
    if coverage.get("required_cells") != sum(
        len(item.required_timeframes) for item in manifest.instruments
    ):
        raise ManifestError("run_receipt coverage required_cells does not match manifest")
    for field_name in ("required_cells", "persisted_rows"):
        value = coverage.get(field_name)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ManifestError(f"run_receipt coverage {field_name} must be a positive integer")
    if coverage.get("closed_bars_only") is not True:
        raise ManifestError("run_receipt coverage must confirm closed bars only")
    selection_snapshot_hash = coverage.get("selection_snapshot_hash")
    if (
        not isinstance(selection_snapshot_hash, str)
        or len(selection_snapshot_hash) != 64
        or any(char not in "0123456789abcdefABCDEF" for char in selection_snapshot_hash)
    ):
        raise ManifestError("run_receipt coverage selection_snapshot_hash must be SHA-256")
    quality = receipt.get("quality")
    if not isinstance(quality, Mapping) or quality.get("status") != "pass":
        raise ManifestError("run_receipt quality must have status=pass")
    for field_name in ("gaps", "duplicates", "invalid_rows", "blocked_cells"):
        value = quality.get(field_name)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ManifestError(f"run_receipt quality {field_name} must be a non-negative integer")
    if quality.get("blocked_cells") != 0:
        raise ManifestError("run_receipt quality cannot contain blocked cells")
    storage = receipt.get("storage")
    if not isinstance(storage, Mapping) or any(
        storage.get(field_name) is not True
        for field_name in ("atomic_commit", "watermark_advanced", "receipts_persisted")
    ):
        raise ManifestError("run_receipt storage must confirm atomic commit and receipts")
    sources = receipt.get("sources")
    if (
        not isinstance(sources, list)
        or not sources
        or not all(isinstance(item, Mapping) for item in sources)
    ):
        raise ManifestError("run_receipt sources must contain source receipts")
    for source in sources:
        source_id = source.get("source_id")
        if not isinstance(source_id, str) or not source_id.strip():
            raise ManifestError("run_receipt source receipts need source_id")
        if source.get("status") != "success":
            raise ManifestError("run_receipt source receipts must have status=success")
        instrument_count = source.get("instrument_count")
        if (
            not isinstance(instrument_count, int)
            or isinstance(instrument_count, bool)
            or instrument_count < 1
        ):
            raise ManifestError("run_receipt source instrument_count must be positive")
    expected_sources = {
        item.source_id for item in manifest.instruments if item.source_status == "configured"
    }
    actual_sources = {
        source["source_id"] for source in sources if isinstance(source.get("source_id"), str)
    }
    if len(actual_sources) != len(sources):
        raise ManifestError("run_receipt source receipts must have unique source_id values")
    if expected_sources - actual_sources:
        raise ManifestError("run_receipt sources do not cover every configured source")
    expected_source_counts = {
        source_id: sum(
            item.source_status == "configured" and item.source_id == source_id
            for item in manifest.instruments
        )
        for source_id in expected_sources
    }
    actual_source_counts = {source["source_id"]: source["instrument_count"] for source in sources}
    if actual_source_counts != expected_source_counts:
        raise ManifestError("run_receipt source instrument counts do not match manifest")


def activate_manifest(
    manifest: MvpManifest | Mapping[str, Any],
    *,
    effective_at: str,
    selection_receipt: Mapping[str, Any] | None = None,
    run_receipt: Mapping[str, Any],
) -> tuple[MvpManifest, dict[str, str]]:
    """Activate only after a full, evidence-bearing first run succeeds."""

    detached = validate_manifest(manifest)
    if detached.selection_policy.get("status") != "ready":
        raise ManifestError("activation requires selection_policy.status=ready")
    blocked = [
        item.instrument_id
        for item in detached.instruments
        if item.source_status == "blocked_for_entitlement" or item.blocked_timeframes
    ]
    if blocked:
        raise ManifestError(
            "activation is blocked while entitlement/timeframe cells remain blocked"
        )
    if selection_receipt is None:
        raise ManifestError("activation requires a validated selection_receipt")
    validated_selection = _validate_selection_receipt(
        detached, selection_receipt, require_complete=True
    )
    _validate_run_receipt(detached, run_receipt)
    if run_receipt["coverage"]["selection_snapshot_hash"] != validated_selection["snapshot_hash"]:
        raise ManifestError("run_receipt selection snapshot is not bound to selection_receipt")
    if not isinstance(effective_at, str) or not effective_at.strip():
        raise ManifestError("effective_at must be a non-empty string")
    _parse_temporal(effective_at, field_name="effective_at", index=-1)
    payload = detached.to_dict()
    payload["effective_at"] = effective_at.strip()
    payload["selection_policy"]["status"] = "active"
    activated = validate_manifest(payload)
    receipt = {
        "manifest_version": activated.version,
        "manifest_hash": manifest_digest(activated),
        "effective_at": activated.effective_at or "",
    }
    return activated, receipt


def replace_candidate(
    manifest: MvpManifest | Mapping[str, Any],
    *,
    universe: str,
    failed_symbol: str,
    replacement: ManifestInstrument,
    effective_at: str,
    selection_receipt: Mapping[str, Any],
) -> tuple[MvpManifest, dict[str, Any]]:
    """Replace the next eligible reserve member and version the manifest."""

    detached = validate_manifest(manifest)
    if universe not in ("a_share", "us_stock"):
        raise ManifestError("replacement universe must be a_share or us_stock")
    if not isinstance(failed_symbol, str) or not failed_symbol.strip():
        raise ManifestError("failed_symbol must be non-empty")
    if not isinstance(effective_at, str) or not effective_at.strip():
        raise ManifestError("effective_at must be a non-empty string")
    _parse_temporal(effective_at, field_name="effective_at", index=-1)
    _validate_instrument(replacement, index=-1)
    if replacement.universe != universe:
        raise ManifestError("replacement universe does not match target")
    payload = detached.to_dict()
    active = payload["instruments"]
    failed_index = next(
        (
            index
            for index, item in enumerate(active)
            if item["universe"] == universe
            and _canonical_identity(item["display_symbol"]) == _canonical_identity(failed_symbol)
        ),
        None,
    )
    if failed_index is None:
        raise ManifestError("failed_symbol is not an active manifest member")
    evidence = _validate_selection_receipt(detached, selection_receipt)
    if evidence["universe"] != universe:
        raise ManifestError("selection_receipt universe does not match replacement")
    if not any(
        _canonical_identity(value) == _canonical_identity(failed_symbol)
        for value in evidence["rejected"]
    ):
        raise ManifestError("selection_receipt must record the failed candidate as rejected")
    if not any(
        _canonical_identity(value) == _canonical_identity(replacement.display_symbol)
        for value in evidence["selected"]
    ):
        raise ManifestError("selection_receipt must record the replacement as selected")

    reserve_records = [dict(record) for record in payload["reserve_records"][universe]]
    eligible_records = [
        record for record in reserve_records if _reserve_status(record) == "eligible"
    ]
    if not eligible_records:
        raise ManifestError("reserve has no eligible replacement candidate")
    expected = eligible_records[0]
    if _canonical_identity(replacement.display_symbol) != _canonical_identity(
        expected["display_symbol"]
    ):
        raise ManifestError("replacement must use the next eligible reserve candidate")
    for field_name, replacement_value in (
        ("display_name", replacement.display_name),
        ("provider_symbol", replacement.provider_symbol),
        ("source_id", replacement.source_id),
    ):
        if _canonical_identity(replacement_value) != _canonical_identity(expected[field_name]):
            raise ManifestError(f"replacement {field_name} does not match its reserve identity")
    expected_identity = expected["identity"]
    replacement_identity = _instrument_identity(replacement)
    for field_name in _RESERVE_IDENTITY_FIELDS:
        if not _identity_values_match(
            expected_identity.get(field_name),
            replacement_identity.get(field_name),
            field_name=field_name,
        ):
            raise ManifestError(f"replacement {field_name} does not match its reserve identity")
    if any(
        _canonical_identity(item["display_symbol"])
        == _canonical_identity(replacement.display_symbol)
        for item in active
    ):
        raise ManifestError("replacement already exists in active manifest")

    failed = active[failed_index]
    active[failed_index] = replacement.to_dict()
    remaining_reserves = [
        symbol
        for symbol in payload["reserves"][universe]
        if _canonical_identity(symbol) != _canonical_identity(replacement.display_symbol)
    ]
    reserve_records = [
        record
        for record in reserve_records
        if _canonical_identity(record["display_symbol"])
        != _canonical_identity(replacement.display_symbol)
    ]
    remaining_reserves.append(failed_symbol)
    reserve_records.append(
        {
            "display_symbol": failed_symbol,
            "display_name": failed["display_name"],
            "provider_symbol": failed["provider_symbol"],
            "source_id": failed["source_id"],
            "selection_rank": len(reserve_records) + 1,
            "pre_screened": False,
            "status": "quarantined",
            "quarantine_reason": "failed_candidate",
            "selection_reason": "replaced_member_quarantined_for_audit",
            "identity": _instrument_identity(failed),
        }
    )
    for rank, record in enumerate(reserve_records, start=1):
        record["selection_rank"] = rank
    payload["reserves"][universe] = remaining_reserves
    payload["reserve_records"][universe] = reserve_records
    payload["effective_at"] = effective_at.strip()
    payload["version"] = _next_manifest_version(detached.version)
    replaced = validate_manifest(payload)
    receipt = build_selection_receipt(
        replaced,
        universe=universe,
        snapshot_url=evidence["snapshot_url"],
        snapshot_hash=evidence["snapshot_hash"],
        window=evidence["window"],
        thresholds=evidence["thresholds"],
        selected=evidence["selected"],
        rejected=evidence["rejected"],
        replacements=[
            *evidence["replacements"],
            {
                "failed": failed_symbol,
                "replacement": replacement.display_symbol,
                "reason": "explicit",
            },
        ],
        reason=evidence["reason"],
    )
    return replaced, receipt


def _next_manifest_version(version: str) -> str:
    if "." in version:
        prefix, suffix = version.rsplit(".", 1)
        if suffix.isdigit():
            return f"{prefix}.{int(suffix) + 1}"
    return f"{version}.1"


def load_manifest(path: str | Path) -> MvpManifest:
    """Load and validate a JSON MVP manifest."""

    manifest_path = Path(path)
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ManifestError(f"manifest not found: {manifest_path}") from exc
    except json.JSONDecodeError as exc:
        raise ManifestError(f"manifest JSON is invalid: {manifest_path}") from exc
    return validate_manifest(payload)


def manifest_digest(manifest: MvpManifest | Mapping[str, Any]) -> str:
    """Return a stable SHA-256 over the detached manifest content."""

    detached = validate_manifest(manifest)
    encoded = json.dumps(
        detached.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
