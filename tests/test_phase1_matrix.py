"""Contract tests for the canonical Phase 1 17-asset / 39-cell matrix."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from kline.api import get_candles, health, stream_candles
from kline.config import Settings
from kline.models import AssetClass, CachePolicy, Candle, FallbackPolicy, QualityPolicy, Timeframe
from kline.ports import ProviderBackedMarketDataAdapter
from kline.provenance import source_manifest
from kline.registry import init, provider_status
from kline.store import KlineStore
from ops.phase1_matrix import PHASE1_MATRIX_VERSION, PHASE1_POLICIES, required_cells, validate_matrix
from ops.verify_phase1_matrix import _cell_result, _health_contract_issues


def test_phase1_matrix_has_exactly_17_assets_and_39_unique_cells():
    validate_matrix()
    cells = required_cells()

    assert PHASE1_MATRIX_VERSION == "weekly-macro-phase1-39-cell-v1"
    assert len({cell["asset_key"] for cell in cells}) == 17
    assert len(cells) == 39
    assert len({(cell["asset_key"], cell["timeframe"]) for cell in cells}) == 39
    assert all(
        {key: cell[key] for key in PHASE1_POLICIES} == PHASE1_POLICIES
        for cell in cells
    )


def test_matrix_verifier_does_not_promote_wrong_policy_or_empty_200_to_ready():
    cell = required_cells()[0]
    wrong_policy = {
        "candles": [{"timestamp": "2026-08-19", "open": 1, "high": 1, "low": 1, "close": 1}],
        "requested_source": cell["source"],
        "selected_source": cell["source"],
        "cache_policy": "allow",
        "quality_policy": "strict",
        "fallback_policy": "none",
        "served_from": "cache",
        "attempted_sources": [cell["source"]],
        "provider": "provider",
        "provider_symbol": cell["ticker"],
        "source_identity": {"provider": "provider"},
    }
    result = _cell_result(cell, 200, wrong_policy)
    assert result["status"] == "blocked"
    assert result["reject_reason"].startswith("contract_mismatch:")

    empty = _cell_result(cell, 200, {"candles": [], "requested_source": cell["source"]})
    assert empty["status"] == "blocked"
    assert empty["reject_reason"] == "empty_data"


def test_phase1_matrix_sources_and_timeframes_match_local_health(tmp_path):
    init(Settings(db_path=str(tmp_path / "health.db"), load_entrypoint_adapters=False))
    sources = provider_status()["sources"]

    for cell in required_cells():
        source = sources[cell["source"]]
        assert source["available"] is False, cell
        assert source["configured"] is True, cell
        assert source["availability_basis"] == "not_live_probed"
        manifest = source_manifest(cell["source"], AssetClass(cell["asset_class"]))
        provider_symbol = manifest.canonical_ticker(cell["ticker"]).upper()
        by_symbol = source["supported_timeframes_by_symbol"]
        assert cell["timeframe"] in by_symbol[provider_symbol], cell


@pytest.mark.asyncio
async def test_bypass_matrix_policy_does_not_persist_cache_or_audit_rows(monkeypatch, tmp_path):
    class Provider:
        last_raw_response = {
            "request_params": {"symbol": "BTCUSDT"},
            "response_body": [["raw"]],
            "status_code": 200,
            "error": None,
        }

        def supported_timeframes(self):
            return [Timeframe.MIN_1]

        async def fetch(self, *_args, **_kwargs):
            timestamp = datetime.now(timezone.utc).replace(second=0, microsecond=0).isoformat()
            return [Candle(timestamp=timestamp, open=100, high=101, low=99, close=100.5, volume=10)]

    store = KlineStore(str(tmp_path / "bypass.db"))
    adapter = ProviderBackedMarketDataAdapter(
        source_manifest("binance_spot_public", AssetClass.CRYPTO),
        Provider(),
    )
    monkeypatch.setattr("kline.api.get_store", lambda: store)
    monkeypatch.setattr("kline.api.get_adapter_for_source", lambda *_args: adapter)
    before = (store.count_raw_responses(), len(store.latest_source_observations()))

    response = await get_candles(
        asset_class=AssetClass.CRYPTO,
        ticker="BTC",
        timeframe=Timeframe.MIN_1,
        start=None,
        end=None,
        limit=1,
        refresh=False,
        source="binance_spot_public",
        cache_policy=CachePolicy.BYPASS,
        quality=QualityPolicy.STRICT,
        fallback_policy=FallbackPolicy.NONE,
        require_execution_venue=False,
        profile=None,
        strict=False,
        mode="research",
    )

    after = (store.count_raw_responses(), len(store.latest_source_observations()))
    assert response.served_from == "upstream"
    assert before == after
    assert store.count("BTCUSDT", AssetClass.CRYPTO, Timeframe.MIN_1, source_id="binance_spot_public") == 0


@pytest.mark.asyncio
async def test_websocket_bypass_stream_does_not_persist_candles(monkeypatch, tmp_path):
    class StreamAdapter:
        async def stream_candles(self, *_args, **_kwargs):
            yield Candle(
                timestamp=datetime.now(timezone.utc).replace(second=0, microsecond=0).isoformat(),
                open=100,
                high=101,
                low=99,
                close=100.5,
                volume=10,
            )

    class WebSocket:
        def __init__(self):
            self.messages = []

        async def accept(self):
            return None

        async def send_json(self, payload):
            self.messages.append(payload)

        async def close(self, **_kwargs):
            return None

    store = KlineStore(str(tmp_path / "websocket.db"))
    monkeypatch.setattr("kline.api.get_store", lambda: store)
    monkeypatch.setattr("kline.api.get_adapter_for_source", lambda *_args: StreamAdapter())
    websocket = WebSocket()

    await stream_candles(
        websocket,
        AssetClass.CRYPTO,
        "BTC",
        timeframe=Timeframe.MIN_1,
        source="binance_spot_public",
        quality=QualityPolicy.STRICT,
    )

    assert websocket.messages[0]["cache_policy"] == "bypass"
    assert store.count("BTC", AssetClass.CRYPTO, Timeframe.MIN_1, source_id="binance_spot_public") == 0
    assert store.count_raw_responses() == 0
    assert store.latest_source_observations() == []


@pytest.mark.asyncio
async def test_health_exposes_runtime_and_registry_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("KLINE_BUILD_SHA", "test-phase1-sha")
    init(Settings(db_path=str(tmp_path / "health.db"), load_entrypoint_adapters=False))

    payload = await health()

    assert payload["runtime"]["build_sha"] == "test-phase1-sha"
    assert payload["runtime"]["registry_version"] == "weekly-macro-phase1-source-registry-v1"
    assert payload["runtime"]["identity_status"] == "declared"
    assert payload["runtime"]["database_path"].endswith("health.db")
    assert payload["providers"]["sources"]["tencent_kline"]["availability_basis"] == "not_live_probed"
    assert payload["providers"]["sources"]["treasury_official_csv"]["availability_basis"] == "not_live_probed"
    assert _health_contract_issues(200, payload) == []
