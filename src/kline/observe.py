"""What the 8100 log needs to answer "when, how long, and what was blocked" (#202).

The 2026-11-09 gold outage (#194, #196, #198, #200) was found by timing curl calls by hand: the log had no
timestamps, no durations, and nothing said the event loop was blocked. This module adds exactly those, with
stdlib ``logging`` only:

- one UTC timestamp format for the ``kline`` and ``uvicorn`` loggers;
- a WARNING for each request slower than ``KLINE_SLOW_REQUEST_SECONDS`` (default 1.0);
- a WARNING when the event loop wakes more than ``KLINE_LOOP_LAG_WARN_SECONDS`` late (default 1.0);
- a startup timeline (begin, registry init, complete, first request served).

Logging never raises into a request: the ``logging`` module swallows handler errors, and the middleware
measures around the app without changing what it returns.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qsl, urlencode

LOGGER = logging.getLogger("kline.observe")
FORMAT = "%(asctime)s.%(msecs)03dZ %(levelname)s %(name)s: %(message)s"
DATEFMT = "%Y-%m-%dT%H:%M:%S"
REDACTED = "[redacted]"
_SECRET_KEY = re.compile(r"secret|token|passw|api[_-]?key|private[_-]?key|signature|auth", re.I)
_TIMESTAMPED = "_kline_timestamped"
# When this module was imported: uvicorn imports the app factory right after it starts, and Starlette only
# builds the middleware on the first request, so the middleware cannot take its own start time.
PROCESS_STARTED = time.monotonic()


def _seconds(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def slow_request_seconds() -> float:
    return _seconds("KLINE_SLOW_REQUEST_SECONDS", 1.0)


def loop_lag_warn_seconds() -> float:
    return _seconds("KLINE_LOOP_LAG_WARN_SECONDS", 1.0)


def formatter() -> logging.Formatter:
    utc = logging.Formatter(FORMAT, DATEFMT)
    utc.converter = time.gmtime
    return utc


def configure_logging() -> None:
    """Timestamp every line on the ``kline`` and ``uvicorn`` loggers. Safe to call more than once.

    uvicorn sets up its own handlers before it loads the app factory, so their formatters are replaced
    here. ``kline`` gets a stderr handler of its own only if nothing above it handles records yet.
    """
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        for handler in logging.getLogger(name).handlers:
            handler.setFormatter(formatter())
    kline = logging.getLogger("kline")
    if not any(getattr(h, _TIMESTAMPED, False) for h in kline.handlers) and not logging.getLogger().handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(formatter())
        setattr(handler, _TIMESTAMPED, True)
        kline.addHandler(handler)
    if kline.level == logging.NOTSET:
        kline.setLevel(logging.WARNING)  # providers log every fetch at INFO; that stays out of the file
    if LOGGER.level == logging.NOTSET:
        LOGGER.setLevel(logging.INFO)  # the startup timeline is INFO


def redacted_target(path: str, query: bytes | str) -> str:
    """The path and query for the log line, with values under secret-like keys replaced."""
    text = query.decode("latin-1") if isinstance(query, bytes) else query
    if not text:
        return path
    pairs = [(key, REDACTED if _SECRET_KEY.search(key) else value) for key, value in parse_qsl(text, keep_blank_values=True)]
    return f"{path}?{urlencode(pairs, safe='/:,')}"


class Timing:
    """Pure ASGI middleware: a WARNING per slow request, and one line for the first request served."""

    def __init__(self, app: Callable[..., Awaitable[None]], *, started_at: float | None = None) -> None:
        self.app = app
        self.started_at = started_at if started_at is not None else PROCESS_STARTED
        self.first_served = False

    async def __call__(self, scope: dict[str, Any], receive: Callable, send: Callable) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        began, status = time.monotonic(), 0

        async def watch(message: dict[str, Any]) -> None:
            nonlocal status
            if message.get("type") == "http.response.start":
                status = int(message.get("status", 0))
            await send(message)

        try:
            await self.app(scope, receive, watch)
        finally:
            done = time.monotonic()
            took = done - began
            target = redacted_target(scope.get("path", ""), scope.get("query_string", b""))
            if not self.first_served:
                self.first_served = True
                LOGGER.info("first request served %.0f ms after the app was loaded: %s %s %s in %.0f ms",
                            (done - self.started_at) * 1000, scope.get("method", ""), target, status or "-", took * 1000)
            if took > slow_request_seconds():
                LOGGER.warning("slow request %s %s status=%s took %.0f ms",
                               scope.get("method", ""), target, status or "-", took * 1000)


async def watch_loop_lag(interval: float = 0.5) -> None:
    """Warn when the loop wakes much later than asked: something ran on it without yielding."""
    while True:
        asked = time.monotonic()
        await asyncio.sleep(interval)
        lag = time.monotonic() - asked - interval
        if lag > loop_lag_warn_seconds():
            LOGGER.warning("event loop blocked: woke %.0f ms late", lag * 1000)
