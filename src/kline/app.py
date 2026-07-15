"""FastAPI application."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from kline import __version__
from kline.api import router
from kline.health_ui import router as health_ui_router
from kline.registry import init


@asynccontextmanager
async def lifespan(app: FastAPI):
    init()
    yield


def create_app() -> FastAPI:
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

    app.include_router(router, prefix="/api")
    app.include_router(health_ui_router)

    return app
