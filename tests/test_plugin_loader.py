"""Tests for config and package entry-point adapter discovery."""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from typing import AsyncIterator

import pytest

from kline.models import AssetClass, Candle, InstrumentDefinition, Timeframe
from kline.plugin_loader import load_configured_adapters
from kline.ports import ProviderMeta, SourceManifest
from kline.providers.base import ProviderError


class ConfiguredAdapter:
    def __init__(self, config: dict) -> None:
        self.config = config
        self.manifest = SourceManifest(
            source_id="configured_broker",
            asset_class=AssetClass.COMMODITY,
            meta=ProviderMeta(
                name="configured_broker",
                source_mode="configured_broker",
                quality_flags=("broker_adapter",),
                continuous=False,
            ),
        )

    @property
    def last_raw_response(self):
        return None

    def canonical_ticker(self, ticker: str) -> str:
        return ticker

    def supported_timeframes(self) -> list[Timeframe]:
        return [Timeframe.MIN_1]

    async def fetch_candles(self, *_args, **_kwargs) -> list[Candle]:
        return []

    async def stream_candles(self, *_args, **_kwargs) -> AsyncIterator[Candle]:
        if False:
            yield Candle(timestamp="", open=0, high=0, low=0, close=0, volume=0)

    async def fetch_instrument_definition(self, _ticker: str) -> InstrumentDefinition:
        raise ProviderError("not implemented by fixture")


@pytest.fixture
def adapter_module(monkeypatch):
    module = types.ModuleType("test_external_adapter")
    module.create_adapter = lambda config: ConfiguredAdapter(config)
    monkeypatch.setitem(sys.modules, module.__name__, module)
    return module


def test_configured_adapter_loads_with_environment_credentials(
    tmp_path: Path, monkeypatch, adapter_module
):
    monkeypatch.setenv("TEST_BROKER_TOKEN", "secret-from-env")
    path = tmp_path / "adapters.json"
    path.write_text(
        json.dumps(
            {
                "adapters": [
                    {
                        "factory": f"{adapter_module.__name__}:create_adapter",
                        "config": {
                            "account": "paper",
                            "token": "${TEST_BROKER_TOKEN}",
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    adapters = load_configured_adapters(str(path))
    assert [adapter.manifest.source_id for adapter in adapters] == ["configured_broker"]
    assert adapters[0].config == {"account": "paper", "token": "secret-from-env"}


def test_missing_credential_fails_closed(tmp_path: Path, monkeypatch, adapter_module):
    monkeypatch.delenv("MISSING_BROKER_TOKEN", raising=False)
    path = tmp_path / "adapters.json"
    path.write_text(
        json.dumps(
            {
                "adapters": [
                    {
                        "factory": f"{adapter_module.__name__}:create_adapter",
                        "config": {"token": "${MISSING_BROKER_TOKEN}"},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ProviderError, match="MISSING_BROKER_TOKEN"):
        load_configured_adapters(str(path))


def test_disabled_adapter_is_not_loaded(tmp_path: Path):
    path = tmp_path / "adapters.json"
    path.write_text(
        json.dumps(
            {"adapters": [{"factory": "does.not.exist:create", "enabled": False}]}
        ),
        encoding="utf-8",
    )
    assert load_configured_adapters(str(path)) == []
