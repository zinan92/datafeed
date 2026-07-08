"""FastAPI routes — the entire public API."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from kline.models import AssetClass, Candle, CandleResponse, ErrorResponse, Timeframe
from kline.providers.base import ProviderError
from kline.provenance import ProviderMeta, freshness, provider_meta
from kline.registry import get_provider, get_store

router = APIRouter()


def _build_response(
    ticker: str,
    asset_class: AssetClass,
    timeframe: Timeframe,
    candles: list[Candle],
    meta: ProviderMeta,
    *,
    served_from: str,
) -> CandleResponse:
    """Stamp provenance on each candle and wrap them in the trust envelope."""
    stamped = [
        candle.model_copy(update={"provider": meta.name, "quality_flags": list(meta.quality_flags)})
        for candle in candles
    ]
    latest = stamped[-1].timestamp if stamped else None
    age_seconds = max_age_seconds = None
    fresh = None
    if latest is not None:
        age_seconds, max_age_seconds, fresh = freshness(latest, meta, timeframe)
    return CandleResponse(
        ticker=ticker,
        asset_class=asset_class,
        timeframe=timeframe,
        count=len(stamped),
        schema_version="kline-candles-v1",
        provider=meta.name,
        source_mode=meta.source_mode,
        quality_flags=list(meta.quality_flags),
        is_synthetic=False,
        served_from=served_from,
        fresh=fresh,
        latest_timestamp=latest,
        age_seconds=age_seconds,
        max_age_seconds=max_age_seconds,
        candles=stamped,
    )


@router.get(
    "/candles/{asset_class}/{ticker}",
    response_model=CandleResponse,
    responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)
async def get_candles(
    asset_class: AssetClass,
    ticker: str,
    timeframe: Timeframe = Query(default=Timeframe.DAY),
    start: str | None = Query(default=None, description="Start date: YYYY-MM-DD"),
    end: str | None = Query(default=None, description="End date: YYYY-MM-DD"),
    limit: int = Query(default=500, ge=1, le=2000),
    refresh: bool = Query(default=False, description="Force fetch from source"),
) -> CandleResponse:
    """
    Get K-line candles for any asset.

    - **asset_class**: a_share, us_stock, crypto, commodity
    - **ticker**: Symbol (e.g., 000001, AAPL, BTC, GOLD)
    - **timeframe**: 1m, 5m, 30m, 1h, 1d, 1w
    - **refresh**: Force re-fetch from upstream source
    """
    store = get_store()
    meta = provider_meta(asset_class)

    # Try local store first (unless refresh requested).
    # NOTE: this serves cached candles without re-checking the upstream. The
    # response's served_from="cache" + fresh/age_seconds make that visible so a
    # consumer can reject stale data; automatic refetch-on-stale is roadmap.
    if not refresh:
        cached = store.query(ticker, asset_class, timeframe, start=start, end=end, limit=limit)
        if cached:
            return _build_response(ticker, asset_class, timeframe, cached, meta, served_from="cache")

    # Fetch from upstream provider
    provider = get_provider(asset_class)
    try:
        candles = await provider.fetch(ticker, timeframe, start=start, end=end, limit=limit)
    except ProviderError as e:
        raise HTTPException(
            status_code=404 if "No data" in str(e) else 400,
            detail=ErrorResponse(
                error=str(e),
                suggestions=e.suggestions,
            ).model_dump(),
        )

    # Save to local store
    if candles:
        store.save(ticker, asset_class, timeframe, candles)

    return _build_response(ticker, asset_class, timeframe, candles, meta, served_from="upstream")


@router.get("/tickers")
async def list_tickers(
    asset_class: AssetClass | None = Query(default=None),
) -> dict:
    """List all tickers with stored data."""
    store = get_store()
    tickers = store.list_tickers(asset_class)
    return {"count": len(tickers), "tickers": tickers}


@router.get("/health")
async def health() -> dict:
    """Health check."""
    from kline import __version__

    return {"status": "ok", "service": "kline", "version": __version__}
