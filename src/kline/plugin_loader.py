"""Discover market-data adapters without editing datafeed core routing."""

from __future__ import annotations

import json
import os
import re
from importlib import import_module, metadata
from pathlib import Path
from typing import Any, Callable

from kline.ports import MarketDataPort
from kline.providers.base import ProviderError

ENTRYPOINT_GROUP = "kline.market_data_adapters"
_ENV_PATTERN = re.compile(r"^\$\{([A-Z][A-Z0-9_]*)\}$")


def _resolve_env(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _resolve_env(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_env(item) for item in value]
    if not isinstance(value, str):
        return value
    match = _ENV_PATTERN.match(value)
    if not match:
        return value
    env_name = match.group(1)
    resolved = os.getenv(env_name)
    if resolved is None:
        raise ProviderError(
            f"Adapter credential/config environment variable is missing: {env_name}",
            suggestions=[f"Set {env_name} before starting datafeed"],
        )
    return resolved


def _validate_adapter(adapter: Any, origin: str) -> MarketDataPort:
    required = (
        "manifest",
        "last_raw_response",
        "canonical_ticker",
        "supported_timeframes",
        "fetch_candles",
        "stream_candles",
        "fetch_instrument_definition",
    )
    missing = [name for name in required if not hasattr(adapter, name)]
    if missing:
        raise ProviderError(
            f"Invalid market-data adapter from {origin}: missing {', '.join(missing)}"
        )
    source_id = adapter.manifest.source_id
    if not source_id or source_id != source_id.strip().lower():
        raise ProviderError(
            f"Invalid source_id from {origin}: use a non-empty lowercase id"
        )
    return adapter


def _factory(reference: str) -> Callable[[dict[str, Any]], MarketDataPort]:
    module_name, separator, attribute = reference.partition(":")
    if not separator or not module_name or not attribute:
        raise ProviderError(
            f"Invalid adapter factory '{reference}'",
            suggestions=["Use the format package.module:create_adapter"],
        )
    try:
        factory = getattr(import_module(module_name), attribute)
    except (ImportError, AttributeError) as error:
        raise ProviderError(f"Cannot load adapter factory {reference}: {error}") from error
    if not callable(factory):
        raise ProviderError(f"Adapter factory is not callable: {reference}")
    return factory


def load_configured_adapters(config_path: str) -> list[MarketDataPort]:
    if not config_path:
        return []
    path = Path(config_path).expanduser()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ProviderError(f"Cannot load adapter config {path}: {error}") from error

    entries = payload.get("adapters") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        raise ProviderError("Adapter config must contain an 'adapters' list")

    adapters: list[MarketDataPort] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ProviderError(f"Adapter config entry {index} must be an object")
        if entry.get("enabled", True) is False:
            continue
        reference = str(entry.get("factory") or "")
        settings = _resolve_env(entry.get("config") or {})
        if not isinstance(settings, dict):
            raise ProviderError(f"Adapter config entry {index} config must be an object")
        adapter = _factory(reference)(settings)
        adapters.append(_validate_adapter(adapter, reference))
    return adapters


def load_entrypoint_adapters() -> list[MarketDataPort]:
    adapters: list[MarketDataPort] = []
    try:
        discovered = metadata.entry_points(group=ENTRYPOINT_GROUP)
    except TypeError:  # Python 3.10 compatibility for older importlib metadata behavior
        discovered = metadata.entry_points().select(group=ENTRYPOINT_GROUP)
    for entrypoint in discovered:
        loaded = entrypoint.load()
        adapter = loaded() if callable(loaded) else loaded
        adapters.append(_validate_adapter(adapter, f"entrypoint:{entrypoint.name}"))
    return adapters
