"""#202: the 8100 log says when, how long, and what blocked the event loop."""
from __future__ import annotations

import asyncio
import logging
import re
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kline import observe


def _app(*, blocking: float = 0.0, sleeping: float = 0.0) -> FastAPI:
    app = FastAPI()

    @app.get("/slow")
    async def slow() -> dict:
        await asyncio.sleep(sleeping)
        return {"ok": True}

    @app.get("/block")
    async def block() -> dict:
        time.sleep(blocking)  # what /api/health did on the event loop before #194
        return {"ok": True}

    @app.get("/fast")
    async def fast() -> dict:
        return {"ok": True}

    app.add_middleware(observe.Timing, started_at=time.monotonic())
    return app


def _warnings(caplog, text: str) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "kline.observe" and r.levelno == logging.WARNING and text in r.getMessage()]


def test_a_slow_request_logs_one_warning_with_path_status_and_duration(caplog, monkeypatch) -> None:
    monkeypatch.setenv("KLINE_SLOW_REQUEST_SECONDS", "0.1")
    caplog.set_level(logging.INFO, logger="kline")
    client = TestClient(_app(sleeping=0.2))
    assert client.get("/fast").status_code == 200
    assert client.get("/slow?ticker=GOLD&timeframe=4h").status_code == 200
    slow = _warnings(caplog, "slow request")
    assert len(slow) == 1, slow
    assert "GET /slow?ticker=GOLD&timeframe=4h status=200 took" in slow[0]
    assert int(re.search(r"took (\d+) ms", slow[0]).group(1)) >= 200


def test_the_first_request_is_logged_once(caplog) -> None:
    caplog.set_level(logging.INFO, logger="kline")
    client = TestClient(_app())
    client.get("/fast")
    client.get("/fast")
    first = [r.getMessage() for r in caplog.records if "first request served" in r.getMessage()]
    assert len(first) == 1 and "GET /fast 200" in first[0], first


def test_secret_like_query_values_are_redacted() -> None:
    target = observe.redacted_target("/api/x", b"ticker=GOLD&api_key=abc123&token=t0k&limit=3")
    assert "abc123" not in target and "t0k" not in target
    assert target == "/api/x?ticker=GOLD&api_key=%5Bredacted%5D&token=%5Bredacted%5D&limit=3"
    assert observe.redacted_target("/api/health", b"") == "/api/health"


def test_a_blocked_event_loop_is_reported(caplog, monkeypatch) -> None:
    monkeypatch.setenv("KLINE_LOOP_LAG_WARN_SECONDS", "0.2")
    caplog.set_level(logging.INFO, logger="kline")

    async def scenario() -> None:
        watchdog = asyncio.create_task(observe.watch_loop_lag(interval=0.05))
        await asyncio.sleep(0.1)
        time.sleep(0.5)  # a synchronous call on the loop
        await asyncio.sleep(0.1)
        watchdog.cancel()

    asyncio.run(scenario())
    blocked = _warnings(caplog, "event loop blocked")
    assert len(blocked) == 1, blocked
    assert int(re.search(r"woke (\d+) ms late", blocked[0]).group(1)) >= 300


def test_a_quiet_event_loop_reports_nothing(caplog, monkeypatch) -> None:
    monkeypatch.setenv("KLINE_LOOP_LAG_WARN_SECONDS", "0.2")
    caplog.set_level(logging.INFO, logger="kline")

    async def scenario() -> None:
        watchdog = asyncio.create_task(observe.watch_loop_lag(interval=0.05))
        await asyncio.sleep(0.3)
        watchdog.cancel()

    asyncio.run(scenario())
    assert _warnings(caplog, "event loop blocked") == []


def test_the_app_logs_its_startup_timeline_in_order(caplog, monkeypatch) -> None:
    from kline import app as app_module

    monkeypatch.setattr(app_module, "init", lambda: time.sleep(0.05))
    caplog.set_level(logging.INFO, logger="kline")
    with TestClient(app_module.create_app()) as client:
        client.get("/docs")
    lines = [r.getMessage() for r in caplog.records if r.name == "kline.observe"]
    order = [next(i for i, line in enumerate(lines) if line.startswith(prefix))
             for prefix in ("startup begin", "registry init took", "startup complete", "first request served")]
    assert order == sorted(order), lines
    assert int(re.search(r"registry init took (\d+) ms", lines[order[1]]).group(1)) >= 50


def test_log_lines_carry_a_utc_timestamp() -> None:
    record = logging.LogRecord("kline.observe", logging.WARNING, __file__, 1, "slow request", None, None)
    record.created = 1_762_660_000.123  # 2025-11-09T03:46:40.123Z
    record.msecs = 123
    line = observe.formatter().format(record)
    assert line.startswith("2025-11-09T03:46:40.123Z WARNING kline.observe: slow request"), line


def test_configure_logging_timestamps_uvicorn_handlers() -> None:
    access = logging.getLogger("uvicorn.access")
    handler = logging.StreamHandler()
    access.addHandler(handler)
    try:
        observe.configure_logging()
        assert handler.formatter is not None and handler.formatter._fmt == observe.FORMAT
    finally:
        access.removeHandler(handler)


def test_provider_fetch_lines_stay_out_but_the_timeline_stays_in(monkeypatch) -> None:
    kline, observer = logging.getLogger("kline"), observe.LOGGER
    monkeypatch.setattr(kline, "level", logging.NOTSET)
    monkeypatch.setattr(observer, "level", logging.NOTSET)
    observe.configure_logging()
    assert not logging.getLogger("kline.providers.okx_swap").isEnabledFor(logging.INFO)
    assert logging.getLogger("kline.providers.okx_swap").isEnabledFor(logging.WARNING)
    assert observer.isEnabledFor(logging.INFO)


def _failing_app() -> FastAPI:
    from fastapi import HTTPException

    app = FastAPI()

    @app.get("/defs")
    async def defs(slow: float = 0.0) -> dict:
        await asyncio.sleep(slow)
        raise HTTPException(status_code=502, detail={
            "error": "instrument_definition_unavailable",
            "detail": "OKX request failed: ConnectTimeout api_key=abc123 token: t0k",
            "is_synthetic": False,
        })

    @app.get("/plain")
    async def plain() -> dict:
        raise HTTPException(status_code=503, detail="market query reader failed")

    @app.get("/boom")
    async def boom() -> dict:
        raise RuntimeError("never shown")

    @app.get("/fine")
    async def fine() -> dict:
        return {"ok": True}

    app.add_middleware(observe.Timing, started_at=time.monotonic())
    return app


def test_a_fast_failure_is_logged_with_its_reason_and_the_body_is_unchanged(caplog) -> None:
    caplog.set_level(logging.INFO, logger="kline")
    response = TestClient(_failing_app()).get("/defs?ticker=XAG")
    assert response.status_code == 502
    assert response.json()["detail"]["error"] == "instrument_definition_unavailable"  # passed through
    failed = _warnings(caplog, "failed request")
    assert len(failed) == 1, failed
    assert "GET /defs?ticker=XAG status=502 took" in failed[0]
    assert "reason=instrument_definition_unavailable OKX request failed: ConnectTimeout" in failed[0]
    assert "abc123" not in failed[0] and "t0k" not in failed[0] and "[redacted]" in failed[0]


def test_a_slow_failure_is_logged_once(caplog, monkeypatch) -> None:
    monkeypatch.setenv("KLINE_SLOW_REQUEST_SECONDS", "0.1")
    caplog.set_level(logging.INFO, logger="kline")
    TestClient(_failing_app()).get("/defs?slow=0.2")
    lines = [r.getMessage() for r in caplog.records if r.name == "kline.observe" and r.levelno == logging.WARNING]
    assert len(lines) == 1 and lines[0].startswith("failed request") and "/defs" in lines[0], lines


def test_a_string_detail_and_an_unhandled_error_give_a_reason(caplog) -> None:
    caplog.set_level(logging.INFO, logger="kline")
    client = TestClient(_failing_app(), raise_server_exceptions=False)
    client.get("/plain")
    assert client.get("/boom").status_code == 500
    failed = _warnings(caplog, "failed request")
    assert any("/plain status=503" in line and "reason=market query reader failed" in line for line in failed), failed
    assert any("/boom status=500" in line and "reason=unhandled RuntimeError" in line for line in failed), failed
    assert not any("never shown" in line for line in failed)


def test_a_success_logs_no_failure(caplog) -> None:
    caplog.set_level(logging.INFO, logger="kline")
    TestClient(_failing_app()).get("/fine")
    assert _warnings(caplog, "failed request") == []


def test_failure_reason_falls_back_to_text() -> None:
    assert observe.failure_reason(b"Internal Server Error") == "Internal Server Error"
    assert observe.failure_reason(b"") == "-"
    assert len(observe.failure_reason(b"x" * 5000)) == observe.REASON_CHARS
