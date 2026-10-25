"""HTTP routes for live market data and causal liquidity analysis."""

from __future__ import annotations

import asyncio
import base64
import html
import logging
import os
import re
from datetime import datetime, timezone
from typing import Annotated, get_args
from app.ai_service import generate_brief, interpret_analysis, gemma_health

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from app.liquidity_engine import analyse
from app.market_data import MarketDataService, UnsupportedSymbol, UnsupportedTimeframe
from app.schemas import (
    AnalysisResponse,
    CandlesResponse,
    EngineConfig,
    ErrorResponse,
    EventType,
    LiquidityEvent,
    MarketInfo,
    SummaryResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter()
_BRIEF_SNAPSHOT_CACHE: dict[str, str] = {}


def get_market_data(request: Request) -> MarketDataService:
    """Return the lifespan-managed market-data service."""
    service = getattr(request.app.state, "market_data", None)
    if service is None:
        raise HTTPException(status_code=503, detail="Market-data service is not initialized")
    return service


async def _supports_symbol(service: MarketDataService, symbol: str) -> bool:
    checker = getattr(service, "supports_symbol", None)
    if callable(checker):
        return bool(await checker(symbol))
    return symbol in service.supported_symbols


def _event_timeframes(service: MarketDataService) -> tuple[str, ...]:
    raw = os.getenv("HUNTER_EVENT_TIMEFRAMES", "15m,1h")
    values = tuple(dict.fromkeys(item.strip() for item in raw.split(",") if item.strip()))
    if not values:
        raise UnsupportedTimeframe("HUNTER_EVENT_TIMEFRAMES must contain at least one timeframe")
    unsupported = [item for item in values if item not in service.supported_timeframes]
    if unsupported:
        raise UnsupportedTimeframe(
            "Unsupported HUNTER_EVENT_TIMEFRAMES value(s): "
            + ", ".join(unsupported)
            + ". Supported: "
            + ", ".join(service.supported_timeframes)
        )
    return values


async def _compute_events(
    service: MarketDataService,
    limit: int = 500,
    symbols: tuple[str, ...] | None = None,
) -> list[LiquidityEvent]:
    """Analyze configured symbols plus optional validated symbols, isolating failures."""
    timeframes = _event_timeframes(service)
    selected_symbols = tuple(dict.fromkeys((*service.supported_symbols, *(symbols or ()))))
    jobs = [
        (symbol, timeframe)
        for symbol in selected_symbols
        for timeframe in timeframes
    ]

    async def analyze_pair(symbol: str, timeframe: str) -> list[LiquidityEvent]:
        candles = await service.get_candles(symbol, timeframe, limit=limit)
        _, events = analyse(candles, symbol, timeframe, EngineConfig())
        return events

    results = await asyncio.gather(
        *(analyze_pair(symbol, timeframe) for symbol, timeframe in jobs),
        return_exceptions=True,
    )
    events: list[LiquidityEvent] = []
    for (symbol, timeframe), result in zip(jobs, results):
        if isinstance(result, Exception):
            logger.warning(
                "Skipping event analysis for %s %s: %s",
                symbol,
                timeframe,
                result,
                exc_info=(type(result), result, result.__traceback__),
            )
            continue
        events.extend(result)
    return events


@router.get("/api/markets", response_model=list[MarketInfo])
async def get_markets(
    service: Annotated[MarketDataService, Depends(get_market_data)],
) -> list[MarketInfo]:
    return [
        MarketInfo(
            symbol=symbol,
            timeframes=list(service.supported_timeframes),
            exchange=service.exchange_id,
        )
        for symbol in service.supported_symbols
    ]




@router.get("/api/markets/search")
async def search_markets(
    service: Annotated[MarketDataService, Depends(get_market_data)],
    q: str | None = None,
    limit: int = 10,
) -> list[dict[str, str]]:
    if not isinstance(q, str) or not re.fullmatch(r"[A-Za-z0-9]{2,20}", q):
        return JSONResponse(
            status_code=400,
            content={"detail": "q must contain 2-20 letters or digits", "code": "INVALID_QUERY"},
        )
    if limit < 1:
        return JSONResponse(
            status_code=400,
            content={"detail": "limit must be at least 1", "code": "INVALID_LIMIT"},
        )
    return await service.search_markets(q, min(limit, 20))


@router.get("/api/candles", response_model=CandlesResponse)
async def get_candles(
    symbol: str,
    timeframe: str,
    limit: Annotated[int, Query(ge=1, le=1000)] = 500,
    service: MarketDataService = Depends(get_market_data),
) -> CandlesResponse:
    candles = await service.get_candles(symbol, timeframe, limit=limit)
    return CandlesResponse(
        symbol=symbol,
        timeframe=timeframe,
        exchange=service.exchange_id,
        data_source="live_exchange",
        generated_at=datetime.now(timezone.utc),
        candles=candles,
    )


@router.get("/api/analysis", response_model=AnalysisResponse)
async def get_analysis(
    symbol: str,
    timeframe: str,
    limit: Annotated[int, Query(ge=1, le=1000)] = 500,
    service: MarketDataService = Depends(get_market_data),
) -> AnalysisResponse:
    candles = await service.get_candles(symbol, timeframe, limit=limit)
    config = EngineConfig()
    levels, events = analyse(candles, symbol, timeframe, config)
    return AnalysisResponse(
        symbol=symbol,
        timeframe=timeframe,
        exchange=service.exchange_id,
        generated_at=datetime.now(timezone.utc),
        candles_analysed=len(candles),
        config=config,
        levels=levels,
        events=events,
    )


@router.get("/api/events", response_model=list[LiquidityEvent])
async def get_events(
    symbol: str | None = None,
    timeframe: str | None = None,
    event_type: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    symbols: str | None = None,
    service: MarketDataService = Depends(get_market_data),
) -> list[LiquidityEvent]:
    requested_symbols: tuple[str, ...] = ()
    if symbols is not None:
        raw_symbols = [item.strip() for item in symbols.split(",")]
        if not raw_symbols or any(not item for item in raw_symbols):
            raise UnsupportedSymbol("symbols must be a comma-separated list of symbols")
        if len(raw_symbols) > 10:
            return JSONResponse(
                status_code=400,
                content={"detail": "symbols accepts at most 10 symbols", "code": "TOO_MANY_SYMBOLS"},
            )
        requested_symbols = tuple(dict.fromkeys(raw_symbols))
        for requested in requested_symbols:
            if not await _supports_symbol(service, requested):
                raise UnsupportedSymbol(f"Unsupported symbol filter: {requested}")
    if symbol is not None and not await _supports_symbol(service, symbol):
        raise UnsupportedSymbol(f"Unsupported symbol filter: {symbol}")
    if timeframe is not None and timeframe not in service.supported_timeframes:
        raise UnsupportedTimeframe(f"Unsupported timeframe filter: {timeframe}")
    valid_event_types = get_args(EventType)
    if event_type is not None and event_type not in valid_event_types:
        return JSONResponse(
            status_code=400,
            content={
                "detail": f"Unsupported event_type filter: {event_type}",
                "code": "INVALID_EVENT_TYPE",
            },
        )

    events = await _compute_events(service, symbols=requested_symbols)
    if symbol is not None:
        events = [event for event in events if event.symbol == symbol]
    if timeframe is not None:
        events = [event for event in events if event.timeframe == timeframe]
    if event_type is not None:
        events = [event for event in events if event.event_type == event_type]
    events.sort(
        key=lambda event: (event.detected_at or event.timestamp, event.event_id),
        reverse=True,
    )
    return events[:limit]


@router.get("/api/events/{event_id}", response_model=LiquidityEvent, responses={404: {"model": ErrorResponse}})
async def get_event(
    event_id: str,
    service: MarketDataService = Depends(get_market_data),
) -> LiquidityEvent:
    events = await _compute_events(service)
    for event in events:
        if event.event_id == event_id:
            return event
    raise HTTPException(status_code=404, detail="Event not found")


@router.get("/api/summary", response_model=SummaryResponse)
async def get_summary(
    service: Annotated[MarketDataService, Depends(get_market_data)],
) -> SummaryResponse:
    events = await _compute_events(service)
    counts: dict[str, int] = {}
    for event in events:
        counts[event.event_type] = counts.get(event.event_type, 0) + 1
    return SummaryResponse(
        total_events=len(events),
        symbols=sorted({event.symbol for event in events}),
        event_counts=counts,
        data_source="live_exchange",
        generated_at=datetime.now(timezone.utc),
    )

    
@router.post("/api/ai/interpret")
async def interpret_live_analysis(
    symbol: str,
    timeframe: str,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    service: MarketDataService = Depends(get_market_data),
):
    candles = await service.get_candles(symbol, timeframe, limit=limit)
    config = EngineConfig()
    levels, events = analyse(candles, symbol, timeframe, config)

    analysis = AnalysisResponse(
        symbol=symbol,
        timeframe=timeframe,
        exchange=service.exchange_id,
        generated_at=datetime.now(timezone.utc),
        candles_analysed=len(candles),
        config=config,
        levels=levels,
        events=events,
    )

    return await interpret_analysis(analysis.model_dump(mode="json"))


# END OF FILE backend/app/routes.py

@router.get("/api/ai/health")
async def ai_health() -> dict:
    """Report whether the configured Gemma inference service confirms a loaded model."""
    return await gemma_health()


def _snapshot_svg(candles: list, event: dict) -> str:
    """Render a compact, self-contained SVG candle snapshot; no external service needed."""
    if not candles:
        return ""
    width, height = 720, 250
    left, right, top, bottom = 58, 12, 16, 28
    lows = [float(c.low) for c in candles]
    highs = [float(c.high) for c in candles]
    level = event.get("level_price")
    event_price = event.get("price")
    if isinstance(level, (int, float)):
        lows.append(float(level)); highs.append(float(level))
    if isinstance(event_price, (int, float)):
        lows.append(float(event_price)); highs.append(float(event_price))
    low, high = min(lows), max(highs)
    span = max(high - low, abs(high) * 0.0001, 1e-9)
    low -= span * .06; high += span * .06; span = high - low
    plot_w, plot_h = width-left-right, height-top-bottom
    def y(price): return top + (high-price)/span*plot_h
    step = plot_w / max(len(candles), 1)
    body_w = max(2.0, min(7.0, step * .58))
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
           '<rect width="100%" height="100%" fill="#0d1411"/>']
    for i in range(5):
        yy = top + i*plot_h/4
        val = high - i*span/4
        out.append(f'<line x1="{left}" y1="{yy:.1f}" x2="{width-right}" y2="{yy:.1f}" stroke="#26352d" stroke-width="1"/>')
        out.append(f'<text x="{left-7}" y="{yy+4:.1f}" fill="#a4b6aa" font-size="10" text-anchor="end">{val:.2f}</text>')
    for i,c in enumerate(candles):
        x=left+(i+.5)*step
        o, h, l, cl = float(c.open), float(c.high), float(c.low), float(c.close)
        color = '#69d69a' if cl >= o else '#e58b82'
        out.append(f'<line x1="{x:.1f}" y1="{y(h):.1f}" x2="{x:.1f}" y2="{y(l):.1f}" stroke="{color}"/>')
        yy=min(y(o),y(cl)); hh=max(1.2,abs(y(o)-y(cl)))
        out.append(f'<rect x="{x-body_w/2:.1f}" y="{yy:.1f}" width="{body_w:.1f}" height="{hh:.1f}" fill="{color}"/>')
    if isinstance(level,(int,float)):
        yy=y(float(level)); out.append(f'<line x1="{left}" y1="{yy:.1f}" x2="{width-right}" y2="{yy:.1f}" stroke="#d6b75c" stroke-width="1.8" stroke-dasharray="6 4"/>')
        out.append(f'<text x="{width-right-2}" y="{max(top+10,yy-4):.1f}" fill="#e8cf79" font-size="11" text-anchor="end">Linked level {float(level):.2f}</text>')
    if isinstance(event_price,(int,float)):
        yy=y(float(event_price)); out.append(f'<line x1="{left}" y1="{yy:.1f}" x2="{width-right}" y2="{yy:.1f}" stroke="#8fb7ff" stroke-width="1.8"/>')
        out.append(f'<text x="{left+4}" y="{max(top+10,yy-4):.1f}" fill="#a8c8ff" font-size="11">Event {float(event_price):.2f}</text>')
    title=html.escape(f"{event.get('symbol','')} · {event.get('timeframe','')} · {event.get('event_type','')}")
    out.append(f'<text x="{left}" y="{height-7}" fill="#d9e5dc" font-size="12">{title} · candles around event</text></svg>')
    return "".join(out)


@router.get("/brief")
async def get_brief(
    symbols: str | None = None,
    service: MarketDataService = Depends(get_market_data),
) -> dict:
    """Return a one-click evidence brief for the supplied watchlist."""
    if symbols is None or not symbols.strip():
        selected_symbols = tuple(service.supported_symbols)
    else:
        raw_symbols = [item.strip() for item in symbols.split(",")]
        if any(not item for item in raw_symbols):
            raise UnsupportedSymbol("symbols must be a comma-separated list of symbols")
        selected_symbols = tuple(dict.fromkeys(raw_symbols))
        if len(selected_symbols) > 20:
            return JSONResponse(
                status_code=400,
                content={"detail": "symbols accepts at most 20 symbols", "code": "TOO_MANY_SYMBOLS"},
            )
        for requested in selected_symbols:
            if not await _supports_symbol(service, requested):
                raise UnsupportedSymbol(f"Unsupported symbol filter: {requested}")

    timeframes = _event_timeframes(service)
    jobs = [(symbol, timeframe) for symbol in selected_symbols for timeframe in timeframes]

    candle_sets: dict[tuple[str, str], list] = {}
    level_sets: dict[tuple[str, str], list] = {}
    async def analyze_pair(symbol: str, timeframe: str) -> list[LiquidityEvent]:
        candles = await service.get_candles(symbol, timeframe, limit=500)
        candle_sets[(symbol, timeframe)] = candles
        found_levels, found_events = analyse(candles, symbol, timeframe, EngineConfig())
        level_sets[(symbol, timeframe)] = found_levels
        return found_events

    results = await asyncio.gather(
        *(analyze_pair(symbol, timeframe) for symbol, timeframe in jobs),
        return_exceptions=True,
    )
    found: list[LiquidityEvent] = []
    for (symbol, timeframe), result in zip(jobs, results):
        if isinstance(result, Exception):
            logger.warning("Skipping brief analysis for %s %s: %s", symbol, timeframe, result)
            continue
        found.extend(result)

    # Priority is deliberately transparent: highest engine-reported volume ratio first.
    found.sort(
        key=lambda event: (
            event.volume_ratio,
            event.detected_at or event.timestamp,
            event.event_id,
        ),
        reverse=True,
    )
    top_events = []
    for event in found[:3]:
        row = event.model_dump(mode="json")
        matched = next((level for level in level_sets.get((event.symbol, event.timeframe), []) if level.level_id == event.level_id), None)
        row["level_swept"] = matched.swept if matched is not None else None
        row["level_kind"] = matched.kind if matched is not None else None
        row["level_touches"] = matched.touches if matched is not None else None
        top_events.append(row)
    brief = await generate_brief(top_events)
    for item in brief.get("events", []):
        evidence = item.get("evidence", {})
        key = (evidence.get("symbol"), evidence.get("timeframe"))
        candles = candle_sets.get(key, [])
        if candles:
            stamp = evidence.get("timestamp")
            try:
                event_dt = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
                idx = min(range(len(candles)), key=lambda i: abs((candles[i].timestamp-event_dt).total_seconds()))
                # Approximately 10 hours before / 5 hours after for 15m; fewer bars on higher timeframes.
                timeframe = str(evidence.get("timeframe", "15m"))
                bars_before, bars_after = {"5m": (120, 60), "15m": (40, 20), "1h": (10, 5), "4h": (3, 1)}.get(timeframe, (40, 20))
                window = candles[max(0, idx-bars_before):min(len(candles), idx+bars_after+1)]
                cache_key = str(evidence.get("event_id"))
                data_uri = _BRIEF_SNAPSHOT_CACHE.get(cache_key)
                if not data_uri:
                    svg = _snapshot_svg(window, evidence)
                    if svg:
                        data_uri = "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()
                        _BRIEF_SNAPSHOT_CACHE[cache_key] = data_uri
                if data_uri:
                    item["snapshot"] = data_uri
                    item["snapshot_alt"] = f"{evidence.get('symbol')} {timeframe} event with linked level marked"
            except (ValueError, TypeError):
                logger.warning("Could not render brief snapshot for event %s", evidence.get("event_id"))
    return brief

