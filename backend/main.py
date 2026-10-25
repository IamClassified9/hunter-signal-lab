from __future__ import annotations
from dotenv import load_dotenv

load_dotenv()

import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.market_data import (
    ExchangeUnavailable,
    MarketDataError,
    MarketDataService,
    RateLimited,
    UnsupportedSymbol,
    UnsupportedTimeframe,
)
from app.routes import router
from app.schemas import ErrorResponse


DEFAULT_CORS_ORIGINS = "http://localhost:5173,http://127.0.0.1:5173"


def cors_origins() -> list[str]:
    """Allowed browser origins from HUNTER_CORS_ORIGINS (comma-separated)."""
    raw = os.getenv("HUNTER_CORS_ORIGINS", DEFAULT_CORS_ORIGINS)
    origins = [item.strip().rstrip("/") for item in raw.split(",") if item.strip()]
    return origins or DEFAULT_CORS_ORIGINS.split(",")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    service = MarketDataService()
    app.state.market_data = service
    try:
        yield
    finally:
        await service.close()


app = FastAPI(
    title="Hunter Signal Lab",
    description="Liquidity-event research and analysis API",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins(),
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)


def _error_response(status_code: int, detail: str, code: str) -> JSONResponse:
    body = ErrorResponse(detail=detail, code=code)
    return JSONResponse(status_code=status_code, content=body.model_dump())


@app.exception_handler(UnsupportedSymbol)
async def unsupported_symbol_handler(request: Request, exc: UnsupportedSymbol) -> JSONResponse:
    return _error_response(400, str(exc), "UNSUPPORTED_SYMBOL")


@app.exception_handler(UnsupportedTimeframe)
async def unsupported_timeframe_handler(request: Request, exc: UnsupportedTimeframe) -> JSONResponse:
    return _error_response(400, str(exc), "UNSUPPORTED_TIMEFRAME")


@app.exception_handler(RateLimited)
async def rate_limited_handler(request: Request, exc: RateLimited) -> JSONResponse:
    return _error_response(429, str(exc), "RATE_LIMITED")


@app.exception_handler(ExchangeUnavailable)
async def exchange_unavailable_handler(request: Request, exc: ExchangeUnavailable) -> JSONResponse:
    return _error_response(503, str(exc), "EXCHANGE_UNAVAILABLE")


@app.exception_handler(MarketDataError)
async def market_data_error_handler(request: Request, exc: MarketDataError) -> JSONResponse:
    return _error_response(502, str(exc), "EXCHANGE_UNAVAILABLE")


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    code = "NOT_FOUND" if exc.status_code == 404 else "EXCHANGE_UNAVAILABLE"
    return _error_response(exc.status_code, str(exc.detail), code)


@app.get("/")
def root() -> dict[str, str]:
    return {
        "application": "Hunter Signal Lab",
        "version": "0.1.0",
        "status": "running",
    }


@app.get("/health")
async def health(request: Request) -> dict[str, object]:
    from app.ai_service import gemma_health
    result: dict[str, object] = {"status": "healthy", "prediction_model": "not_connected", "gemma": await gemma_health()}
    service = getattr(request.app.state, "market_data", None)
    if service is not None:
        result["exchange"] = service.exchange_id
    return result


app.include_router(router)

# END OF FILE backend/main.py
