"""FastAPI application."""

import asyncio
import contextlib
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from kline import __version__
from kline.api import router
from kline.health_ui import router as health_ui_router
from kline.observe import LOGGER, Timing, configure_logging, watch_loop_lag
from kline.market_query import get_market_query_reader
from kline.registry import init


@asynccontextmanager
async def lifespan(app: FastAPI):
    began = time.monotonic()
    LOGGER.info("startup begin (kline %s)", __version__)
    init()
    LOGGER.info("registry init took %.0f ms", (time.monotonic() - began) * 1000)
    watchdog = asyncio.create_task(watch_loop_lag())
    warm = asyncio.create_task(warm_market_query_reader())
    LOGGER.info("startup complete in %.0f ms", (time.monotonic() - began) * 1000)
    try:
        yield
    finally:
        for task in (watchdog, warm):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


async def warm_market_query_reader() -> None:
    """Build the market query reader in a worker thread so no request builds it on the loop (#204)."""
    began = time.monotonic()
    try:
        reader = await asyncio.to_thread(get_market_query_reader)
    except Exception as error:  # a request will report it (503); here we only say so
        LOGGER.warning("market query reader failed after %.0f ms: %s", (time.monotonic() - began) * 1000, type(error).__name__)
        return
    if reader is not None:
        LOGGER.info("market query reader ready in %.0f ms", (time.monotonic() - began) * 1000)


def create_app() -> FastAPI:
    configure_logging()
    app = FastAPI(
        title="kline",
        description="Multi-asset K-line data. in ticker + timeframe → out OHLCV candles.",
        version=__version__,
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET"],
        allow_headers=["*"],
    )
    app.add_middleware(Timing)  # outermost: times the whole request, CORS included

    app.include_router(router, prefix="/api")
    app.include_router(health_ui_router)

    return app
