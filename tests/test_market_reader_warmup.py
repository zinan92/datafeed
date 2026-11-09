"""#204: building the market query reader (~18 s on production) never runs on the event loop."""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from kline import api, market_query
from kline.store import KlineStore


class SlowReader:
    """Stands in for MarketQueryReader, whose constructor scans the market database."""

    builds = 0

    def __init__(self, manifest_path, database_path, *, minimum_series_rows: int = 600) -> None:
        SlowReader.builds += 1
        time.sleep(0.6)

    def status(self) -> dict:
        return {"mode": market_query.MARKET_FIRST, "market_database_path": "redacted", "market_hits": 0,
                "market_misses": 0, "legacy_fallbacks": 0, "legacy_unique_cells": 0, "miss_reasons": {}}


def _market_first(monkeypatch, tmp_path: Path) -> None:
    legacy, market = tmp_path / "legacy.db", tmp_path / "market.db"
    KlineStore(str(legacy))
    KlineStore(str(market))
    monkeypatch.setenv("KLINE_DB_PATH", str(legacy))
    monkeypatch.setenv("KLINE_MARKET_DB_PATH", str(market))
    monkeypatch.setenv("KLINE_QUERY_BACKEND", "market_first")
    monkeypatch.setenv("KLINE_LOAD_ENTRYPOINT_ADAPTERS", "false")
    monkeypatch.setattr(market_query, "MarketQueryReader", SlowReader)
    SlowReader.builds = 0


def test_health_says_warming_and_never_builds_the_reader(monkeypatch, tmp_path: Path) -> None:
    _market_first(monkeypatch, tmp_path)
    started = time.monotonic()
    status = market_query.query_backend_status()
    assert time.monotonic() - started < 0.2
    assert status["mode"] == "market_first" and status["status"] == "warming"
    assert SlowReader.builds == 0


def test_the_startup_warm_up_builds_it_in_a_thread_and_logs_how_long(monkeypatch, tmp_path: Path, caplog) -> None:
    from kline import app as app_module

    _market_first(monkeypatch, tmp_path)
    caplog.set_level(logging.INFO, logger="kline")

    async def scenario():
        lags: list[float] = []

        async def probe() -> None:  # the loop must stay free while the reader builds
            for _ in range(15):
                asked = time.monotonic()
                await asyncio.sleep(0.05)
                lags.append(time.monotonic() - asked - 0.05)

        await asyncio.gather(app_module.warm_market_query_reader(), probe())
        return lags, market_query.query_backend_status()

    lags, status = asyncio.run(scenario())
    assert max(lags) < 0.2, lags
    assert SlowReader.builds == 1 and status["status"] == "ready"
    assert any(r.getMessage().startswith("market query reader ready in") for r in caplog.records)


def test_a_candle_read_during_warm_up_waits_off_the_loop(monkeypatch, tmp_path: Path) -> None:
    _market_first(monkeypatch, tmp_path)

    async def scenario():
        lags: list[float] = []

        async def probe() -> None:
            for _ in range(15):
                asked = time.monotonic()
                await asyncio.sleep(0.05)
                lags.append(time.monotonic() - asked - 0.05)

        async def candle_path() -> object:  # what get_candles does to get the reader
            return market_query.cached_market_query_reader() or await asyncio.to_thread(market_query.get_market_query_reader)

        reader, _ = await asyncio.gather(candle_path(), probe())
        return reader, lags

    reader, lags = asyncio.run(scenario())
    assert isinstance(reader, SlowReader)
    assert max(lags) < 0.2, lags
    assert market_query.cached_market_query_reader() is reader


def test_a_failed_build_is_reported_not_left_warming(monkeypatch, tmp_path: Path) -> None:
    _market_first(monkeypatch, tmp_path)

    class Broken(SlowReader):
        def __init__(self, *args, **kwargs) -> None:
            raise OSError("unable to open database file")

    monkeypatch.setattr(market_query, "MarketQueryReader", Broken)
    try:
        market_query.get_market_query_reader()
    except RuntimeError:
        pass
    status = market_query.query_backend_status()
    assert status["mode"] == "invalid" and status["status"] == "failed"


def test_get_candles_does_not_build_the_reader_on_the_loop() -> None:
    source = Path(api.__file__).read_text()
    assert "cached_market_query_reader() or await asyncio.to_thread(get_market_query_reader)" in source
    assert "market_reader = get_market_query_reader()" not in source
