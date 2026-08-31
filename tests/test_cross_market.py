from __future__ import annotations

from pathlib import Path

import pytest

from kline.cross_market import (
    CrossMarketRegistry,
    validate_cross_market,
)
from kline.mvp_manifest import ManifestError, load_manifest


MANIFEST_PATH = Path(__file__).parents[1] / "configs" / "mvp_manifest.json"


def test_cross_market_roster_has_exact_16_identity_and_session_mappings() -> None:
    manifest = load_manifest(MANIFEST_PATH)
    mappings = validate_cross_market(manifest)

    assert len(mappings) == 16
    by_id = {mapping.instrument_id: mapping for mapping in mappings}
    assert by_id["US.INDEX.SPX"].provider_symbol == "SPX"
    assert by_id["US.INDEX.NDX"].provider_symbol == "NDX"
    assert by_id["US.INDEX.SPX"].security_type == "index"
    assert by_id["US.INDEX.NDX"].security_type == "index"
    assert by_id["US.ETF.UUP"].metadata["proxy_for"] == "DXY"
    assert by_id["CRYPTO.PERP.BTC"].metadata["contract_type"] == "perpetual"
    assert by_id["US.FUTURE.GOLD"].metadata["roll_policy"] == "provider_continuous_contract"
    assert by_id["US.FUTURE.GOLD"].timezone == "America/Chicago"
    assert by_id["CN.INDEX.SHCOMP"].fallback_sources == ("sina_index",)
    assert all(
        mapping.volume_semantics == "not_applicable"
        for mapping in mappings
        if mapping.security_type == "index"
    )


def test_cross_market_fallback_is_opt_in_and_auditable() -> None:
    registry = CrossMarketRegistry(load_manifest(MANIFEST_PATH))

    assert registry.resolve_source("CN.INDEX.SHCOMP", "tencent_kline") == "tencent_kline"
    assert registry.resolve_source("CN.INDEX.SHCOMP", "sina_index") == "sina_index"
    with pytest.raises(ManifestError, match="not an explicit fallback"):
        registry.resolve_source("CN.INDEX.SHCOMP", "yahoo_finance")
    with pytest.raises(ManifestError, match="not an explicit fallback"):
        registry.resolve_source("US.INDEX.SPX", "sina_index")


def test_cross_market_rejects_proxy_or_fallback_drift() -> None:
    manifest = load_manifest(MANIFEST_PATH)

    payload = manifest.to_dict()
    spx = next(item for item in payload["instruments"] if item["instrument_id"] == "US.INDEX.SPX")
    spx["provider_symbol"] = "SPY"
    with pytest.raises(ManifestError, match="unexpected provider"):
        validate_cross_market(payload)

    payload = manifest.to_dict()
    shcomp = next(
        item for item in payload["instruments"] if item["instrument_id"] == "CN.INDEX.SHCOMP"
    )
    shcomp["metadata"]["fallback_sources"] = ["yahoo_finance"]
    with pytest.raises(ManifestError, match="fallback policy"):
        validate_cross_market(payload)
