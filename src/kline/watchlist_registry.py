"""Compile the pinned Park Exposure Registry into a daily K-line manifest."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping

from kline.models import AssetClass
from kline.session_freshness import (
    SESSION_DATE_AT_LOCAL_MIDNIGHT,
    SESSION_DATE_AT_UTC_MIDNIGHT,
)


APPROVED_WATCHLIST_COMMIT = "29ce3c0ad6c6d5f822c860c42ae5ccd251c240d2"
REGISTRY_REPOSITORY = "zinan92/watchlist"
REGISTRY_SNAPSHOT_SCHEMA = "park-exposure-registry-snapshot-v1"
MANIFEST_VERSION = "watchlist_universe_v1"


class RegistryError(ValueError):
    """The pinned registry snapshot cannot produce a trustworthy manifest."""


@dataclass(frozen=True)
class RegistryAsset:
    asset_id: str
    kind: str
    market: str
    name: str


@dataclass(frozen=True)
class RegistryTarget:
    target_id: str
    target_type: str
    market: str | None
    name: str
    listed: bool
    reason: str
    reasons: tuple[str, ...]
    ticker: str | None
    memberships: tuple[dict[str, str], ...]


@dataclass(frozen=True)
class RegistrySnapshot:
    schema_version: str
    upstream_commit: str
    source_sha256: str
    registry_version: int
    updated: str
    assets: tuple[RegistryAsset, ...]
    targets: tuple[RegistryTarget, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "upstream": {
                "repository": REGISTRY_REPOSITORY,
                "commit": self.upstream_commit,
                "source_sha256": self.source_sha256,
                "version": self.registry_version,
                "updated": self.updated,
            },
            "assets": [
                {
                    "id": asset.asset_id,
                    "kind": asset.kind,
                    "market": asset.market,
                    "name": asset.name,
                }
                for asset in self.assets
            ],
            "targets": [
                {
                    "id": target.target_id,
                    "type": target.target_type,
                    "market": target.market,
                    "name": target.name,
                    "listed": target.listed,
                    "reason": target.reason,
                    "reasons": list(target.reasons),
                    "ticker": target.ticker,
                    "memberships": [dict(item) for item in target.memberships],
                }
                for target in self.targets
            ],
        }


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RegistryError(f"{field_name} must be a non-empty string")
    return value.strip()


def _sha256(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{64}", value))


def _target_from_dict(raw: Mapping[str, Any], *, index: int) -> RegistryTarget:
    target_id = _required_text(raw.get("id"), f"target[{index}].id")
    target_type = _required_text(raw.get("type"), f"target[{index}].type")
    market = raw.get("market")
    if market is not None:
        market = _required_text(market, f"target[{index}].market")
    name = str(raw.get("name") or target_id).strip()
    listed = raw.get("listed")
    if target_type == "asset" and listed is None:
        listed = False
    if not isinstance(listed, bool):
        raise RegistryError(f"target[{index}].listed must be boolean")
    reason = _required_text(raw.get("reason"), f"target[{index}].reason")
    reasons_raw = raw.get("reasons", [reason])
    if not isinstance(reasons_raw, list) or not reasons_raw:
        raise RegistryError(f"target[{index}].reasons must be a non-empty list")
    reasons = tuple(
        _required_text(value, f"target[{index}].reasons[{reason_index}]")
        for reason_index, value in enumerate(reasons_raw)
    )
    ticker = raw.get("ticker")
    if ticker is not None:
        ticker = _required_text(ticker, f"target[{index}].ticker")
    memberships_raw = raw.get("memberships", [])
    if not isinstance(memberships_raw, list):
        raise RegistryError(f"target[{index}].memberships must be a list")
    memberships: list[dict[str, str]] = []
    for membership_index, membership in enumerate(memberships_raw):
        if not isinstance(membership, Mapping):
            raise RegistryError(
                f"target[{index}].memberships[{membership_index}] must be an object"
            )
        memberships.append(
            {
                "sector_id": _required_text(
                    membership.get("sector_id"),
                    f"target[{index}].memberships[{membership_index}].sector_id",
                ),
                "sector_name": _required_text(
                    membership.get("sector_name"),
                    f"target[{index}].memberships[{membership_index}].sector_name",
                ),
                "macro_id": _required_text(
                    membership.get("macro_id"),
                    f"target[{index}].memberships[{membership_index}].macro_id",
                ),
            }
        )
    return RegistryTarget(
        target_id=target_id,
        target_type=target_type,
        market=market,
        name=name,
        listed=listed,
        reason=reason,
        reasons=reasons,
        ticker=ticker,
        memberships=tuple(memberships),
    )


def _snapshot_from_dict(raw: Mapping[str, Any]) -> RegistrySnapshot:
    schema_version = _required_text(raw.get("schema_version"), "schema_version")
    if schema_version != REGISTRY_SNAPSHOT_SCHEMA:
        raise RegistryError(f"unsupported registry snapshot schema: {schema_version}")
    upstream = raw.get("upstream")
    if not isinstance(upstream, Mapping):
        raise RegistryError("upstream metadata is required")
    repository = _required_text(upstream.get("repository"), "upstream.repository")
    if repository != REGISTRY_REPOSITORY:
        raise RegistryError("registry snapshot repository is not approved")
    commit = _required_text(upstream.get("commit"), "upstream.commit")
    if commit != APPROVED_WATCHLIST_COMMIT:
        raise RegistryError("registry snapshot is not the approved pinned commit")
    source_sha256 = _required_text(upstream.get("source_sha256"), "upstream.source_sha256")
    if not _sha256(source_sha256):
        raise RegistryError("upstream.source_sha256 must be a lowercase SHA-256 digest")
    version = upstream.get("version")
    if not isinstance(version, int) or version < 1:
        raise RegistryError("upstream.version must be a positive integer")
    updated = _required_text(upstream.get("updated"), "upstream.updated")

    assets_raw = raw.get("assets")
    if not isinstance(assets_raw, list) or not assets_raw:
        raise RegistryError("assets must be a non-empty list")
    assets: list[RegistryAsset] = []
    seen_assets: set[str] = set()
    for index, asset in enumerate(assets_raw):
        if not isinstance(asset, Mapping):
            raise RegistryError(f"asset[{index}] must be an object")
        asset_id = _required_text(asset.get("id"), f"asset[{index}].id")
        key = asset_id.casefold()
        if key in seen_assets:
            raise RegistryError(f"duplicate asset id: {asset_id}")
        seen_assets.add(key)
        assets.append(
            RegistryAsset(
                asset_id=asset_id,
                kind=_required_text(asset.get("kind"), f"asset[{index}].kind"),
                market=_required_text(asset.get("market"), f"asset[{index}].market"),
                name=_required_text(asset.get("name"), f"asset[{index}].name"),
            )
        )

    targets_raw = raw.get("targets")
    if not isinstance(targets_raw, list):
        raise RegistryError("targets must be a list")
    targets: list[RegistryTarget] = []
    seen_targets: set[str] = set()
    for index, target in enumerate(targets_raw):
        if not isinstance(target, Mapping):
            raise RegistryError(f"target[{index}] must be an object")
        parsed = _target_from_dict(target, index=index)
        key = parsed.target_id.casefold()
        if key in seen_targets:
            raise RegistryError(f"duplicate target id: {parsed.target_id}")
        seen_targets.add(key)
        targets.append(parsed)
    return RegistrySnapshot(
        schema_version=schema_version,
        upstream_commit=commit,
        source_sha256=source_sha256,
        registry_version=version,
        updated=updated,
        assets=tuple(assets),
        targets=tuple(targets),
    )


def load_registry_snapshot(path: str | Path) -> RegistrySnapshot:
    snapshot_path = Path(path)
    try:
        raw = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise RegistryError(f"registry snapshot not found: {snapshot_path}") from error
    except json.JSONDecodeError as error:
        raise RegistryError(f"registry snapshot JSON is invalid: {snapshot_path}") from error
    if not isinstance(raw, Mapping):
        raise RegistryError("registry snapshot must be an object")
    return _snapshot_from_dict(raw)


def _asset_metadata(asset: RegistryAsset) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "registry_asset_id": asset.asset_id,
        "registry_market": asset.market,
        "registry_asset_kind": asset.kind,
    }
    if asset.asset_id in {"SPX", "NDX", "DXY"}:
        metadata.update(
            {
                "identity_role": "proxy",
                "proxy_for": {
                    "SPX": "S&P 500 Index",
                    "NDX": "Nasdaq-100 Index",
                    "DXY": "DXY",
                }[asset.asset_id],
            }
        )
    if asset.asset_id == "VIX":
        metadata["index_identity"] = "actual_index"
    if asset.asset_id in {"SSE", "STAR50", "SSE_DIV"}:
        metadata.update(
            {
                "index_identity": "actual_index",
                "fallback_sources": ["sina_index"],
                "fallback_policy": "explicit_only",
                "daily_timestamp_convention": SESSION_DATE_AT_UTC_MIDNIGHT,
            }
        )
    if asset.asset_id in {"BTC", "ETH", "HYPE"}:
        metadata.update(
            {"contract_type": "perpetual", "venue": "hyperliquid", "settlement": "USDC"}
        )
    if asset.asset_id in {"WTI", "XAU", "XAG"}:
        provider_symbol = {"WTI": "CL=F", "XAU": "GC=F", "XAG": "SI=F"}[asset.asset_id]
        metadata.update(
            {
                "contract_type": "continuous_future",
                "roll_policy": "provider_continuous_contract",
                "roll_identity": provider_symbol,
            }
        )
    return metadata


_ASSET_BINDINGS: dict[str, dict[str, Any]] = {
    "SPX": {"instrument_id": "WATCH.CROSS.SPX", "display_symbol": "SPX", "display_name": "S&P 500 Index（SPY proxy）", "asset_class": "etf", "security_type": "etf", "source_id": "yahoo_finance_etf", "provider_symbol": "SPY", "calendar_id": "us_equities", "timezone": "America/New_York", "venue": "NYSE Arca", "volume_semantics": "traded", "adjustment_basis": "raw_unadjusted"},
    "NDX": {"instrument_id": "WATCH.CROSS.NDX", "display_symbol": "NDX", "display_name": "Nasdaq-100 Index（QQQ proxy）", "asset_class": "etf", "security_type": "etf", "source_id": "yahoo_finance_etf", "provider_symbol": "QQQ", "calendar_id": "us_equities", "timezone": "America/New_York", "venue": "NASDAQ", "volume_semantics": "traded", "adjustment_basis": "raw_unadjusted"},
    "DXY": {"instrument_id": "WATCH.CROSS.DXY", "display_symbol": "DXY", "display_name": "DXY proxy (UUP)", "asset_class": "etf", "security_type": "etf", "source_id": "yahoo_finance_etf", "provider_symbol": "UUP", "calendar_id": "us_equities", "timezone": "America/New_York", "venue": "NYSE Arca", "volume_semantics": "traded", "adjustment_basis": "raw_unadjusted"},
    "SCHD": {"instrument_id": "WATCH.CROSS.SCHD", "display_symbol": "SCHD", "display_name": "Schwab US Dividend Equity ETF", "asset_class": "etf", "security_type": "etf", "source_id": "yahoo_finance_etf", "provider_symbol": "SCHD", "calendar_id": "us_equities", "timezone": "America/New_York", "venue": "NYSE Arca", "volume_semantics": "traded", "adjustment_basis": "raw_unadjusted"},
    "VIX": {"instrument_id": "WATCH.CROSS.VIX", "display_symbol": "VIX", "display_name": "CBOE Volatility Index", "asset_class": "index", "security_type": "index", "source_id": "yahoo_finance_index", "provider_symbol": "^VIX", "calendar_id": "us_equities", "timezone": "America/Chicago", "venue": "Cboe", "volume_semantics": "not_applicable", "adjustment_basis": "raw_index_level"},
    "BTC": {"instrument_id": "WATCH.CROSS.BTC", "display_symbol": "BTC", "display_name": "BTC perpetual", "asset_class": "crypto", "security_type": "crypto_perpetual", "source_id": "hyperliquid_perpetual_public", "provider_symbol": "BTC", "calendar_id": "crypto_24x7", "timezone": "UTC", "venue": "Hyperliquid", "volume_semantics": "traded", "adjustment_basis": "raw_unadjusted"},
    "ETH": {"instrument_id": "WATCH.CROSS.ETH", "display_symbol": "ETH", "display_name": "ETH perpetual", "asset_class": "crypto", "security_type": "crypto_perpetual", "source_id": "hyperliquid_perpetual_public", "provider_symbol": "ETH", "calendar_id": "crypto_24x7", "timezone": "UTC", "venue": "Hyperliquid", "volume_semantics": "traded", "adjustment_basis": "raw_unadjusted"},
    "HYPE": {"instrument_id": "WATCH.CROSS.HYPE", "display_symbol": "HYPE", "display_name": "HYPE perpetual", "asset_class": "crypto", "security_type": "crypto_perpetual", "source_id": "hyperliquid_perpetual_public", "provider_symbol": "HYPE", "calendar_id": "crypto_24x7", "timezone": "UTC", "venue": "Hyperliquid", "volume_semantics": "traded", "adjustment_basis": "raw_unadjusted"},
    "SSE": {"instrument_id": "WATCH.CROSS.SHCOMP", "display_symbol": "sh000001", "display_name": "Shanghai Composite", "asset_class": "index", "security_type": "index", "source_id": "tencent_kline", "provider_symbol": "sh000001", "calendar_id": "cn_a", "timezone": "Asia/Shanghai", "venue": "SSE", "volume_semantics": "not_applicable", "adjustment_basis": "raw_index_level"},
    "STAR50": {"instrument_id": "WATCH.CROSS.STAR50", "display_symbol": "sh000688", "display_name": "STAR 50", "asset_class": "index", "security_type": "index", "source_id": "tencent_kline", "provider_symbol": "sh000688", "calendar_id": "cn_a", "timezone": "Asia/Shanghai", "venue": "SSE", "volume_semantics": "not_applicable", "adjustment_basis": "raw_index_level"},
    "SSE_DIV": {"instrument_id": "WATCH.CROSS.DIVIDEND", "display_symbol": "sh000015", "display_name": "Shanghai Dividend", "asset_class": "index", "security_type": "index", "source_id": "tencent_kline", "provider_symbol": "sh000015", "calendar_id": "cn_a", "timezone": "Asia/Shanghai", "venue": "SSE", "volume_semantics": "not_applicable", "adjustment_basis": "raw_index_level"},
    "N225": {"instrument_id": "WATCH.CROSS.N225", "display_symbol": "^N225", "display_name": "Nikkei 225", "asset_class": "index", "security_type": "index", "source_id": "yahoo_finance_index", "provider_symbol": "^N225", "calendar_id": "jp_equities", "timezone": "Asia/Tokyo", "venue": "JPX", "volume_semantics": "not_applicable", "adjustment_basis": "raw_index_level"},
    "KOSPI": {"instrument_id": "WATCH.CROSS.KOSPI", "display_symbol": "^KS11", "display_name": "KOSPI", "asset_class": "index", "security_type": "index", "source_id": "yahoo_finance_index", "provider_symbol": "^KS11", "calendar_id": "kr_equities", "timezone": "Asia/Seoul", "venue": "KRX", "volume_semantics": "not_applicable", "adjustment_basis": "raw_index_level"},
    "WTI": {"instrument_id": "WATCH.CROSS.WTI", "display_symbol": "CL=F", "display_name": "WTI continuous future", "asset_class": "commodity", "security_type": "continuous_future", "source_id": "yahoo_finance_futures", "provider_symbol": "CL=F", "calendar_id": "us_futures", "timezone": "America/Chicago", "session_policy": "commodity_session", "venue": "Yahoo Finance", "volume_semantics": "traded", "adjustment_basis": "continuous_roll_pending"},
    "XAU": {"instrument_id": "WATCH.CROSS.GOLD", "display_symbol": "GC=F", "display_name": "Gold continuous future", "asset_class": "commodity", "security_type": "continuous_future", "source_id": "yahoo_finance_futures", "provider_symbol": "GC=F", "calendar_id": "us_futures", "timezone": "America/Chicago", "session_policy": "commodity_session", "venue": "Yahoo Finance", "volume_semantics": "traded", "adjustment_basis": "continuous_roll_pending"},
    "XAG": {"instrument_id": "WATCH.CROSS.SILVER", "display_symbol": "SI=F", "display_name": "Silver continuous future", "asset_class": "commodity", "security_type": "continuous_future", "source_id": "yahoo_finance_futures", "provider_symbol": "SI=F", "calendar_id": "us_futures", "timezone": "America/Chicago", "session_policy": "commodity_session", "venue": "Yahoo Finance", "volume_semantics": "traded", "adjustment_basis": "continuous_roll_pending"},
}


def _company_identity(target: RegistryTarget) -> dict[str, Any]:
    market = (target.market or "").upper()
    if market == "CN":
        symbol = target.ticker or target.target_id
        if not re.fullmatch(r"\d{6}", symbol):
            raise RegistryError(f"CN ticker must be six digits: {symbol}")
        asset_class = AssetClass.A_SHARE.value
        source_id = "tencent_stock_free"
        instrument_id = f"WATCH.CN.A.{symbol}"
        display_symbol = symbol
        provider_symbol = symbol
        calendar_id, timezone_name, venue, adjustment = (
            "cn_a", "Asia/Shanghai", "SSE" if symbol.startswith(("6",)) else "SZSE", "qfq"
        )
        security_type = "common_stock"
    elif market == "US":
        symbol = (target.ticker or target.target_id).upper()
        if not re.fullmatch(r"[A-Z][A-Z0-9.\-]*", symbol):
            raise RegistryError(f"US ticker is invalid: {symbol}")
        asset_class, source_id = AssetClass.US_STOCK.value, "yahoo_finance"
        instrument_id = f"WATCH.US.{symbol}"
        display_symbol = provider_symbol = symbol
        calendar_id, timezone_name, venue, adjustment = (
            "us_equities", "America/New_York", "Yahoo Finance", "raw_unadjusted"
        )
        security_type = "foreign_common" if symbol in {"TSM", "ASML"} else "common_stock"
    elif market == "HK":
        symbol = target.ticker or target.target_id
        if not re.fullmatch(r"\d{5}", symbol):
            raise RegistryError(f"HK ticker must be five digits: {symbol}")
        provider_symbol = f"{int(symbol):04d}.HK"
        asset_class, source_id = AssetClass.HK_STOCK.value, "yahoo_finance_hk"
        instrument_id = f"WATCH.HK.{symbol}"
        display_symbol = symbol
        calendar_id, timezone_name, venue, adjustment = (
            "hk_equities", "Asia/Hong_Kong", "HKEX", "raw_unadjusted"
        )
        security_type = "foreign_common"
    elif market == "KR":
        symbol = (target.ticker or target.target_id).upper()
        if not re.fullmatch(r"\d{6}\.KS", symbol):
            raise RegistryError(f"KR ticker must use six digits plus .KS: {symbol}")
        asset_class, source_id = AssetClass.US_STOCK.value, "yahoo_finance"
        instrument_id = f"WATCH.KR.{symbol[:6]}"
        display_symbol = provider_symbol = symbol
        calendar_id, timezone_name, venue, adjustment = (
            "kr_equities", "Asia/Seoul", "KRX", "raw_unadjusted"
        )
        security_type = "foreign_common"
    else:
        raise RegistryError(f"listed company has unsupported market: {market}")
    metadata = {
        "registry_target_id": target.target_id,
        "registry_target_type": target.target_type,
        "registry_market": market,
        "registry_reason": target.reason,
        "registry_reasons": list(target.reasons),
        "registry_memberships": [dict(item) for item in target.memberships],
        "registry_commit": APPROVED_WATCHLIST_COMMIT,
    }
    if market == "CN":
        metadata["daily_timestamp_convention"] = SESSION_DATE_AT_LOCAL_MIDNIGHT
    return {
        "universe": "watchlist",
        "instrument_id": instrument_id,
        "display_symbol": display_symbol,
        "display_name": target.name,
        "asset_class": asset_class,
        "security_type": security_type,
        "source_id": source_id,
        "provider_symbol": provider_symbol,
        "required_timeframes": ["1d"],
        "not_applicable_timeframes": ["15m", "1h", "4h", "1w"],
        "blocked_timeframes": [],
        "calendar_id": calendar_id,
        "timezone": timezone_name,
        "session_policy": "regular_session",
        "volume_semantics": "traded",
        "adjustment_basis": adjustment,
        "aggregation_rule_version": "native_daily_v1",
        "source_status": "configured",
        "venue": venue,
        "metadata": metadata,
    }


def compile_watchlist_manifest(snapshot: RegistrySnapshot | Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(snapshot, RegistrySnapshot):
        snapshot = _snapshot_from_dict(snapshot)
    if len(snapshot.assets) != 16:
        raise RegistryError(f"registry must contain exactly 16 assets, got {len(snapshot.assets)}")
    bindings = []
    for asset in snapshot.assets:
        try:
            binding = dict(_ASSET_BINDINGS[asset.asset_id])
        except KeyError as error:
            raise RegistryError(f"asset has no K-line binding: {asset.asset_id}") from error
        binding.update(
            {
                "universe": "watchlist",
                "required_timeframes": ["1d"],
                "not_applicable_timeframes": ["15m", "1h", "4h", "1w"],
                "blocked_timeframes": [],
                "session_policy": binding.get(
                    "session_policy",
                    "continuous_24x7"
                    if binding["calendar_id"] == "crypto_24x7"
                    else "regular_session",
                ),
                "aggregation_rule_version": "native_daily_v1",
                "source_status": "configured",
                "metadata": _asset_metadata(asset),
            }
        )
        binding["metadata"].update(
            {
                "registry_commit": snapshot.upstream_commit,
                "registry_source_sha256": snapshot.source_sha256,
            }
        )
        bindings.append(binding)

    companies = [target for target in snapshot.targets if target.target_type == "company" and target.listed]
    company_rows = [_company_identity(target) for target in companies]
    for row in company_rows:
        row["metadata"].update(
            {
                "registry_source_sha256": snapshot.source_sha256,
            }
        )
    instruments = bindings + company_rows
    instruments.sort(key=lambda item: (item["instrument_id"].casefold(), item["display_symbol"].casefold()))
    if len(company_rows) != 91:
        raise RegistryError(f"registry must contain exactly 91 listed companies, got {len(company_rows)}")
    if len(instruments) != 107:
        raise RegistryError(f"compiled Price Universe must contain 107 instruments, got {len(instruments)}")
    ids = [item["instrument_id"].casefold() for item in instruments]
    if len(ids) != len(set(ids)):
        raise RegistryError("compiled instrument identities are not unique")
    return {
        "version": MANIFEST_VERSION,
        "selection_as_of": snapshot.updated,
        "effective_at": f"{snapshot.updated}T00:00:00Z",
        "membership_policy": "park_exposure_registry_pinned",
        "excluded_symbols": ["051505"],
        "registry": {
            "repository": REGISTRY_REPOSITORY,
            "commit": snapshot.upstream_commit,
            "source_sha256": snapshot.source_sha256,
            "version": snapshot.registry_version,
            "updated": snapshot.updated,
        },
        "instruments": instruments,
    }


def snapshot_digest(snapshot: RegistrySnapshot) -> str:
    encoded = json.dumps(
        snapshot.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
