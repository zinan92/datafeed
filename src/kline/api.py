"""FastAPI routes — the entire public API."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect

from kline.models import (
    AssetClass,
    CachePolicy,
    Candle,
    CandleResponse,
    ErrorResponse,
    FallbackPolicy,
    QualityPolicy,
    Timeframe,
    InstrumentDefinition,
)
from kline.ports import MarketDataPort
from kline.providers.base import ProviderError
from kline.provenance import (
    ProviderMeta,
    canonical_ticker_for_source,
    normalize_source,
    source_meta,
)
from kline.quality import QualityReport, analyze_candles
from kline.registry import get_adapter_for_source, get_store, provider_status

router = APIRouter()


@dataclass(frozen=True)
class RequestPolicy:
    requested_source: str
    source: str
    cache_policy: CachePolicy
    quality_policy: QualityPolicy
    fallback_policy: FallbackPolicy
    require_execution_venue: bool

    @property
    def strict_quality(self) -> bool:
        return self.quality_policy == QualityPolicy.STRICT


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _build_response(
    ticker: str,
    asset_class: AssetClass,
    timeframe: Timeframe,
    candles: list[Candle],
    meta: ProviderMeta,
    *,
    served_from: str,
    policy: RequestPolicy | None = None,
    extra_quality_flags: list[str] | None = None,
    reject_reason: str | None = None,
    access_issues: list[str] | None = None,
) -> CandleResponse:
    """Stamp provenance on each candle and wrap them in the trust envelope."""
    strict = policy.strict_quality if policy else False
    report = analyze_candles(candles, timeframe, meta, strict=strict)
    quality_flags = _dedupe(
        [*meta.quality_flags, *report.quality_flags, *(extra_quality_flags or [])]
    )
    merged_issues = _dedupe([*report.access_issues, *(access_issues or [])])
    final_reject_reason = reject_reason or report.reject_reason
    stamped = [
        candle.model_copy(update={"provider": meta.name, "quality_flags": quality_flags})
        for candle in candles
    ]
    return CandleResponse(
        ticker=ticker,
        asset_class=asset_class,
        timeframe=timeframe,
        count=len(stamped),
        schema_version="kline-candles-v1",
        provider=meta.name,
        source_mode=meta.source_mode,
        requested_source=policy.requested_source if policy else meta.source_mode,
        cache_policy=policy.cache_policy if policy else CachePolicy.ALLOW,
        quality_policy=policy.quality_policy if policy else QualityPolicy.STANDARD,
        fallback_policy=policy.fallback_policy if policy else FallbackPolicy.NONE,
        require_execution_venue=policy.require_execution_venue if policy else False,
        quality_flags=quality_flags,
        is_synthetic=False,
        served_from=served_from,
        fresh=report.fresh,
        latest_timestamp=report.latest_timestamp,
        age_seconds=report.age_seconds,
        max_age_seconds=report.max_age_seconds,
        execution_venue=meta.execution_venue,
        reject_reason=final_reject_reason,
        access_issues=merged_issues,
        candles=stamped,
    )


def _error(
    *,
    status_code: int,
    error: str,
    meta: ProviderMeta,
    served_from: str,
    policy: RequestPolicy | None = None,
    detail: str | None = None,
    suggestions: list[str] | None = None,
    report: QualityReport | None = None,
    reject_reason: str | None = None,
    access_issues: list[str] | None = None,
) -> HTTPException:
    quality_flags = list(meta.quality_flags)
    if report:
        quality_flags = _dedupe([*quality_flags, *report.quality_flags])
    issues = _dedupe([*(report.access_issues if report else []), *(access_issues or [])])
    return HTTPException(
        status_code=status_code,
        detail=ErrorResponse(
            error=error,
            detail=detail,
            suggestions=suggestions,
            provider=meta.name,
            source_mode=meta.source_mode,
            requested_source=policy.requested_source if policy else meta.source_mode,
            cache_policy=policy.cache_policy if policy else None,
            quality_policy=policy.quality_policy if policy else None,
            fallback_policy=policy.fallback_policy if policy else None,
            require_execution_venue=policy.require_execution_venue if policy else False,
            served_from=served_from,
            is_synthetic=False,
            fresh=report.fresh if report else None,
            latest_timestamp=report.latest_timestamp if report else None,
            age_seconds=report.age_seconds if report else None,
            max_age_seconds=report.max_age_seconds if report else None,
            quality_flags=quality_flags,
            execution_venue=meta.execution_venue,
            reject_reason=reject_reason or (report.reject_reason if report else None),
            access_issues=issues,
        ).model_dump(),
    )


def _save_raw_if_available(
    *,
    adapter: MarketDataPort,
    ticker: str,
    asset_class: AssetClass,
    timeframe: Timeframe,
    meta: ProviderMeta,
    served_from: str,
) -> None:
    raw = adapter.last_raw_response
    if not raw:
        return
    store = get_store()
    store.save_raw_response(
        provider=meta.name,
        source_mode=meta.source_mode,
        ticker=ticker,
        asset_class=asset_class,
        timeframe=timeframe,
        served_from=served_from,
        execution_venue=meta.execution_venue,
        request_params=raw.get("request_params") or {},
        response_body=raw.get("response_body"),
        status_code=raw.get("status_code"),
        error=raw.get("error"),
    )


def _resolve_policy(
    *,
    asset_class: AssetClass,
    source: str,
    cache_policy: CachePolicy,
    quality: QualityPolicy,
    fallback_policy: FallbackPolicy,
    require_execution_venue: bool,
    profile: str | None,
    strict: bool,
    mode: str,
    refresh: bool,
) -> RequestPolicy:
    """Normalize compatibility aliases and explicit source/cache/quality policies."""
    requested_source = source
    resolved_source = source
    resolved_cache_policy = cache_policy
    resolved_quality = quality
    resolved_fallback_policy = fallback_policy
    resolved_require_execution_venue = require_execution_venue

    if refresh:
        resolved_cache_policy = CachePolicy.BYPASS

    if mode == "live" or strict:
        profile = profile or "execution_live"
    elif mode == "research" and profile is None:
        profile = "historical"

    if profile == "historical":
        pass
    elif profile == "realtime":
        resolved_cache_policy = CachePolicy.BYPASS
        resolved_quality = QualityPolicy.STRICT
        resolved_fallback_policy = FallbackPolicy.NONE
    elif profile == "execution_live":
        if resolved_source == "auto":
            resolved_source = "binance_usdm_futures"
        resolved_cache_policy = CachePolicy.BYPASS
        resolved_quality = QualityPolicy.STRICT
        resolved_fallback_policy = FallbackPolicy.NONE
        resolved_require_execution_venue = True
    elif profile is not None:
        raise ValueError(f"Unknown profile: {profile}")

    try:
        normalized_source = normalize_source(resolved_source, asset_class)
    except KeyError as e:
        raise ValueError(f"Unknown source: {resolved_source}") from e

    return RequestPolicy(
        requested_source=requested_source,
        source=normalized_source,
        cache_policy=resolved_cache_policy,
        quality_policy=resolved_quality,
        fallback_policy=resolved_fallback_policy,
        require_execution_venue=resolved_require_execution_venue,
    )


def _raise_if_execution_venue_required(meta: ProviderMeta, policy: RequestPolicy) -> None:
    if policy.require_execution_venue and not meta.execution_venue:
        raise _error(
            status_code=400,
            error="execution_venue_required",
            detail=f"Source {meta.source_mode} is not an execution venue",
            suggestions=["Use source=binance_usdm_futures for XAUUSDT execution live data"],
            meta=meta,
            served_from="upstream",
            policy=policy,
            reject_reason="not_execution_venue",
            access_issues=[f"{meta.source_mode} has execution_venue=false"],
        )


def _block_if_quality_rejected(
    *,
    report: QualityReport,
    meta: ProviderMeta,
    policy: RequestPolicy,
    served_from: str,
) -> None:
    if report.reject_reason:
        raise _error(
            status_code=503,
            error="data_blocked",
            detail=f"Rejected candles: {report.reject_reason}",
            meta=meta,
            served_from=served_from,
            policy=policy,
            report=report,
        )


async def _fetch_upstream_candles(
    *,
    asset_class: AssetClass,
    ticker: str,
    timeframe: Timeframe,
    start: str | None,
    end: str | None,
    limit: int,
    meta: ProviderMeta,
    policy: RequestPolicy,
) -> CandleResponse:
    """Fetch upstream under an explicit source/cache/quality policy."""
    try:
        adapter = get_adapter_for_source(policy.source, asset_class)
    except ProviderError as e:
        raise _error(
            status_code=400,
            error="provider_unavailable",
            detail=str(e),
            suggestions=e.suggestions,
            meta=meta,
            served_from="upstream",
            policy=policy,
            reject_reason="provider_unavailable",
            access_issues=[str(e)],
        ) from e
    try:
        candles = await adapter.fetch_candles(
            ticker,
            timeframe,
            start=start,
            end=end,
            limit=limit,
        )
    except ProviderError as e:
        _save_raw_if_available(
            adapter=adapter,
            ticker=ticker,
            asset_class=asset_class,
            timeframe=timeframe,
            meta=meta,
            served_from="upstream",
        )
        raise _error(
            status_code=502,
            error="upstream_error",
            detail=str(e),
            suggestions=e.suggestions,
            meta=meta,
            served_from="upstream",
            policy=policy,
            reject_reason="upstream_error",
            access_issues=[str(e)],
        ) from e

    _save_raw_if_available(
        adapter=adapter,
        ticker=ticker,
        asset_class=asset_class,
        timeframe=timeframe,
        meta=meta,
        served_from="upstream",
    )
    report = analyze_candles(candles, timeframe, meta, strict=policy.strict_quality)
    _block_if_quality_rejected(
        report=report,
        meta=meta,
        policy=policy,
        served_from="upstream",
    )

    if candles:
        get_store().save(ticker, asset_class, timeframe, candles)

    return _build_response(
        ticker,
        asset_class,
        timeframe,
        candles,
        meta,
        served_from="upstream",
        policy=policy,
    )


@router.get(
    "/candles/{asset_class}/{ticker}",
    response_model=CandleResponse,
    responses={
        400: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        502: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
    },
)
async def get_candles(
    asset_class: AssetClass,
    ticker: str,
    timeframe: Timeframe = Query(default=Timeframe.DAY),
    start: str | None = Query(default=None, description="Start date: YYYY-MM-DD"),
    end: str | None = Query(default=None, description="End date: YYYY-MM-DD"),
    limit: int = Query(default=500, ge=1, le=2000),
    refresh: bool = Query(default=False, description="Force fetch from source"),
    source: str = Query(default="auto", description="Source id, e.g. binance_usdm_futures"),
    cache_policy: CachePolicy = Query(default=CachePolicy.ALLOW),
    quality: QualityPolicy = Query(default=QualityPolicy.STANDARD),
    fallback_policy: FallbackPolicy = Query(default=FallbackPolicy.NONE),
    require_execution_venue: bool = Query(default=False),
    profile: str | None = Query(default=None, pattern="^(historical|realtime|execution_live)$"),
    strict: bool = Query(default=False, description="Compatibility shortcut for execution_live"),
    mode: str = Query(default="research", pattern="^(research|live)$"),
) -> CandleResponse:
    """
    Get K-line candles for any asset.

    - **asset_class**: a_share, us_stock, crypto, commodity
    - **ticker**: Symbol (e.g., 000001, AAPL, BTC, GOLD)
    - **timeframe**: 1m, 5m, 15m, 30m, 1h, 4h, 1d, 1w
    - **source**: auto, binance_spot_public, binance_usdm_futures, yahoo_finance...
    - **cache_policy**: allow, bypass, require
    - **quality**: standard or strict stale/gap/order checks
    - **profile**: historical, realtime, execution_live shortcut
    - **strict/mode=live**: Compatibility alias for execution_live
    """
    try:
        policy = _resolve_policy(
            asset_class=asset_class,
            source=source,
            cache_policy=cache_policy,
            quality=quality,
            fallback_policy=fallback_policy,
            require_execution_venue=require_execution_venue,
            profile=profile,
            strict=strict,
            mode=mode,
            refresh=refresh,
        )
        meta = source_meta(policy.source, asset_class)
    except ValueError as e:
        raise HTTPException(
            status_code=400,
            detail=ErrorResponse(
                error="invalid_policy",
                detail=str(e),
                requested_source=source,
                cache_policy=cache_policy,
                quality_policy=quality,
                fallback_policy=fallback_policy,
                require_execution_venue=require_execution_venue,
                reject_reason="invalid_policy",
                access_issues=[str(e)],
            ).model_dump(),
        ) from e

    _raise_if_execution_venue_required(meta, policy)
    ticker = canonical_ticker_for_source(policy.source, asset_class, ticker)

    store = get_store()

    if policy.cache_policy in (CachePolicy.ALLOW, CachePolicy.REQUIRE):
        cached = store.query(ticker, asset_class, timeframe, start=start, end=end, limit=limit)
        if cached:
            report = analyze_candles(cached, timeframe, meta, strict=policy.strict_quality)
            _block_if_quality_rejected(
                report=report,
                meta=meta,
                policy=policy,
                served_from="cache",
            )
            return _build_response(
                ticker,
                asset_class,
                timeframe,
                cached,
                meta,
                served_from="cache",
                policy=policy,
            )
        if policy.cache_policy == CachePolicy.REQUIRE:
            raise _error(
                status_code=404,
                error="cache_miss",
                detail="cache_policy=require but no cached candles were found",
                suggestions=["Use cache_policy=allow or cache_policy=bypass to fetch upstream"],
                meta=meta,
                served_from="cache",
                policy=policy,
                reject_reason="cache_miss",
                access_issues=["cache miss"],
            )

    return await _fetch_upstream_candles(
        asset_class=asset_class,
        ticker=ticker,
        timeframe=timeframe,
        start=start,
        end=end,
        limit=limit,
        meta=meta,
        policy=policy,
    )


@router.websocket("/ws/candles/{asset_class}/{ticker}")
async def stream_candles(
    websocket: WebSocket,
    asset_class: AssetClass,
    ticker: str,
    timeframe: Timeframe = Query(default=Timeframe.MIN_1),
    source: str = Query(default="binance_usdm_futures"),
    quality: QualityPolicy = Query(default=QualityPolicy.STRICT),
) -> None:
    """Stream realtime candle updates as standard candle envelopes."""
    await websocket.accept()
    try:
        normalized_source = normalize_source(source, asset_class)
        meta = source_meta(normalized_source, asset_class)
        adapter = get_adapter_for_source(normalized_source, asset_class)
    except (KeyError, ValueError, ProviderError) as e:
        await websocket.send_json(
            ErrorResponse(
                error="invalid_stream_source",
                detail=str(e),
                requested_source=source,
                served_from="websocket",
                is_synthetic=False,
                reject_reason="invalid_source",
                access_issues=[str(e)],
            ).model_dump()
        )
        await websocket.close(code=1008)
        return

    policy = RequestPolicy(
        requested_source=source,
        source=normalized_source,
        cache_policy=CachePolicy.BYPASS,
        quality_policy=quality,
        fallback_policy=FallbackPolicy.NONE,
        require_execution_venue=False,
    )
    ticker = canonical_ticker_for_source(normalized_source, asset_class, ticker)
    if not meta.realtime_supported:
        await websocket.send_json(
            ErrorResponse(
                error="stream_unavailable",
                provider=meta.name,
                source_mode=meta.source_mode,
                requested_source=source,
                cache_policy=policy.cache_policy,
                quality_policy=policy.quality_policy,
                fallback_policy=policy.fallback_policy,
                served_from="websocket",
                execution_venue=meta.execution_venue,
                reject_reason="stream_unavailable",
            ).model_dump()
        )
        await websocket.close(code=1011)


        return

    try:
        async for candle in adapter.stream_candles(ticker, timeframe):
            get_store().save(ticker, asset_class, timeframe, [candle])
            response = _build_response(
                ticker,
                asset_class,
                timeframe,
                [candle],
                meta,
                served_from="websocket",
                policy=policy,
            )
            await websocket.send_json(response.model_dump(mode="json"))
    except WebSocketDisconnect:
        return
    except ProviderError as e:
        await websocket.send_json(
            ErrorResponse(
                error="stream_error",
                detail=str(e),
                suggestions=e.suggestions,
                provider=meta.name,
                source_mode=meta.source_mode,
                requested_source=source,
                cache_policy=policy.cache_policy,
                quality_policy=policy.quality_policy,
                fallback_policy=policy.fallback_policy,
                served_from="websocket",
                is_synthetic=False,
                quality_flags=list(meta.quality_flags),
                execution_venue=meta.execution_venue,
                reject_reason="upstream_error",
                access_issues=[str(e)],
            ).model_dump()
        )
        await websocket.close(code=1011)


@router.get(
    "/instruments/{asset_class}/{ticker}",
    response_model=InstrumentDefinition,
    summary="Get an upstream execution instrument definition",
)
async def get_instrument_definition(
    asset_class: AssetClass,
    ticker: str,
    source: str = Query("auto"),
    require_execution_venue: bool = Query(False),
) -> InstrumentDefinition:
    try:
        normalized_source = normalize_source(source, asset_class)
        meta = source_meta(normalized_source, asset_class)
        if require_execution_venue and not meta.execution_venue:
            raise ProviderError(
                f"Source {normalized_source} is not an execution venue",
                suggestions=["Choose a source with execution_venue=true"],
            )
        adapter = get_adapter_for_source(normalized_source, asset_class)
        definition = await adapter.fetch_instrument_definition(ticker)
    except ProviderError as e:
        raise HTTPException(
            status_code=502,
            detail={
                "error": "instrument_definition_unavailable",
                "detail": str(e),
                "suggestions": e.suggestions,
                "source_mode": source,
                "is_synthetic": False,
                "reject_reason": "upstream_error",
            },
        ) from e
    return definition


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

    return {
        "status": "ok",
        "service": "kline",
        "version": __version__,
        "providers": provider_status(),
    }
