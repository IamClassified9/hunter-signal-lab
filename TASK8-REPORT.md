# TASK 8 — Search and Add Binance USDT Spot Pairs
## Expected test names and new total

Baseline reported by the project owner: **128 tests**. The added tests contribute 11 cases (the invalid-query test is parameterized across four inputs), giving an expected total of **139 tests**. This is an expected count, not a claim that the suite passed.

1. `test_market_search_matches_base_coin_case_insensitively`
2. `test_market_search_respects_limit_and_caps_results_at_20`
3. `test_market_search_rejects_invalid_query` — 4 parameterized cases
4. `test_analysis_accepts_custom_symbol_from_cached_market_list`
5. `test_analysis_rejects_unknown_custom_symbol`
6. `test_events_symbols_rejects_more_than_10_symbols`
7. `test_events_symbols_validates_and_adds_custom_symbols`
8. `test_search_markets_filters_active_binance_spot_usdt_and_caches_one_hour`

## A. Files created/changed

- `backend/app/market_data.py`
- `backend/app/routes.py`
- `backend/tests/test_api.py`
- `backend/tests/test_market_data.py`
- `frontend/src/App.tsx`
- `frontend/src/App.css`
- `frontend/src/services/api.ts`

## B. Full final contents of every changed file

### `backend/app/market_data.py`

```python
"""Async market-data access, validation, and short-lived closed-candle caching."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import ccxt.async_support as ccxt

from app.schemas import Candle, TIMEFRAME_SECONDS

logger = logging.getLogger(__name__)
SUPPORTED_TIMEFRAMES = tuple(TIMEFRAME_SECONDS)
DEFAULT_SYMBOLS = "BTC/USDT,ETH/USDT,SOL/USDT,BNB/USDT,XRP/USDT,DOGE/USDT"


class MarketDataError(Exception):
    """Base error for market-data retrieval or validation failures."""


class UnsupportedSymbol(MarketDataError):
    """The exchange does not support the requested symbol."""


class UnsupportedTimeframe(MarketDataError):
    """The service does not support the requested timeframe."""


class RateLimited(MarketDataError):
    """The exchange rate limit was reached."""


class ExchangeUnavailable(MarketDataError):
    """The exchange could not be reached or is temporarily unavailable."""


def _ccxt_timestamp_to_utc(timestamp_ms: int | float) -> datetime:
    """Convert one CCXT millisecond timestamp to an aware UTC datetime."""
    return datetime.fromtimestamp(float(timestamp_ms) / 1000.0, tz=timezone.utc)


def _read_config() -> tuple[str, tuple[str, ...], float]:
    """Read all market-data environment configuration in one place."""
    exchange_id = os.getenv("HUNTER_EXCHANGE", "binance").strip().lower()
    symbols = tuple(
        symbol.strip()
        for symbol in os.getenv("HUNTER_SYMBOLS", DEFAULT_SYMBOLS).split(",")
        if symbol.strip()
    )
    raw_ttl = os.getenv("HUNTER_CACHE_TTL", "15")
    try:
        cache_ttl = float(raw_ttl)
    except ValueError as exc:
        raise ValueError("HUNTER_CACHE_TTL must be a number of seconds") from exc
    if cache_ttl < 0:
        raise ValueError("HUNTER_CACHE_TTL must be >= 0")
    if not exchange_id:
        raise ValueError("HUNTER_EXCHANGE must not be empty")
    return exchange_id, symbols, cache_ttl


class MarketDataService:
    """Fetch validated, closed OHLCV candles through an async CCXT exchange."""

    def __init__(
        self,
        exchange: Any | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        exchange_id, symbols, self.cache_ttl = _read_config()
        self._exchange_id = exchange_id
        self._supported_symbols = symbols
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._cache: dict[
            tuple[str, str, str, int], tuple[datetime, list[Candle]]
        ] = {}
        self.exchange = exchange if exchange is not None else self._create_exchange(self.exchange_id)
        self._markets_loaded = False
        self._markets_loaded_at: datetime | None = None
        self._market_cache_ttl = 3600.0
        self._active_spot_usdt_markets: list[dict[str, str]] = []
        self._market_load_lock = asyncio.Lock()

    @property
    def exchange_id(self) -> str:
        """Configured exchange identifier (read-only)."""
        return self._exchange_id

    @property
    def supported_symbols(self) -> tuple[str, ...]:
        """Configured symbol allowlist (read-only)."""
        return self._supported_symbols

    @property
    def supported_timeframes(self) -> tuple[str, ...]:
        """Supported timeframes in canonical order (read-only)."""
        return SUPPORTED_TIMEFRAMES

    @staticmethod
    def _create_exchange(exchange_id: str) -> Any:
        exchange_class = getattr(ccxt, exchange_id, None)
        if exchange_class is None or not isinstance(exchange_class, type):
            available = sorted(
                name
                for name, value in vars(ccxt).items()
                if isinstance(value, type) and hasattr(value, "fetch_ohlcv")
            )
            raise ValueError(
                f"Unknown HUNTER_EXCHANGE '{exchange_id}'. "
                f"Check the CCXT exchange id. Examples: {', '.join(available[:10])}"
            )
        try:
            return exchange_class({"enableRateLimit": True})
        except ccxt.BaseError as exc:
            raise MarketDataError(f"Could not initialize exchange '{exchange_id}': {exc}") from exc

    def _now_utc(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Injected clock must return a timezone-aware datetime")
        return now.astimezone(timezone.utc)

    async def _ensure_markets_loaded(self, *, force: bool = False) -> None:
        """Load and cache active Binance USDT spot pairs for one hour."""
        now = self._now_utc()
        cache_is_fresh = (
            self._markets_loaded_at is not None
            and (now - self._markets_loaded_at).total_seconds() < self._market_cache_ttl
            and (now - self._markets_loaded_at).total_seconds() >= 0
        )
        if self._markets_loaded and cache_is_fresh and not force:
            return
        async with self._market_load_lock:
            now = self._now_utc()
            cache_is_fresh = (
                self._markets_loaded_at is not None
                and 0 <= (now - self._markets_loaded_at).total_seconds() < self._market_cache_ttl
            )
            if self._markets_loaded and cache_is_fresh and not force:
                return
            if self.exchange_id != "binance":
                raise MarketDataError("Custom pair search and validation require Binance spot markets")
            load_markets = getattr(self.exchange, "load_markets", None)
            if not callable(load_markets):
                raise MarketDataError("Exchange does not support loading market metadata")
            try:
                loaded = await load_markets()
            except ccxt.RateLimitExceeded as exc:
                raise RateLimited(f"Rate limited by Binance while loading markets: {exc}") from exc
            except (ccxt.NetworkError, ccxt.ExchangeNotAvailable, ccxt.RequestTimeout) as exc:
                raise ExchangeUnavailable(f"Binance market list is unavailable: {exc}") from exc
            except ccxt.BaseError as exc:
                raise MarketDataError(f"Could not load Binance market list: {exc}") from exc
            markets = loaded if isinstance(loaded, dict) else getattr(self.exchange, "markets", {})
            filtered: list[dict[str, str]] = []
            if isinstance(markets, dict):
                for symbol, market in markets.items():
                    if not isinstance(market, dict):
                        continue
                    base = market.get("base")
                    quote = market.get("quote")
                    is_spot = market.get("spot") is True
                    is_active = market.get("active") is True
                    if (
                        isinstance(base, str)
                        and re.fullmatch(r"[A-Z0-9]{2,20}", base)
                        and quote == "USDT"
                        and is_spot
                        and is_active
                        and isinstance(symbol, str)
                    ):
                        filtered.append({"symbol": symbol, "base": base})
            self._active_spot_usdt_markets = sorted(filtered, key=lambda item: item["symbol"])
            self._markets_loaded = True
            self._markets_loaded_at = now

    async def search_markets(self, query: str, limit: int = 10) -> list[dict[str, str]]:
        """Search cached active Binance USDT spot pairs by base-coin substring."""
        if not isinstance(query, str) or not re.fullmatch(r"[A-Za-z0-9]{2,20}", query):
            raise ValueError("q must contain 2-20 letters or digits")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
            raise ValueError("limit must be an integer between 1 and 20")
        await self._ensure_markets_loaded()
        needle = query.upper()
        return [
            dict(item)
            for item in self._active_spot_usdt_markets
            if needle in item["base"]
        ][:limit]

    async def supports_symbol(self, symbol: str) -> bool:
        """Configured symbols are always accepted; other symbols must be cached Binance spot pairs."""
        if symbol in self.supported_symbols:
            return True
        await self._ensure_markets_loaded()
        return any(item["symbol"] == symbol for item in self._active_spot_usdt_markets)

    async def get_candles(
        self,
        symbol: str,
        timeframe: str,
        limit: int = 500,
        now: datetime | None = None,
    ) -> list[Candle]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("limit must be an integer between 1 and 1000")
        if timeframe not in self.supported_timeframes:
            raise UnsupportedTimeframe(
                f"Unsupported timeframe '{timeframe}'. Supported: {', '.join(self.supported_timeframes)}"
            )
        if not await self.supports_symbol(symbol):
            raise UnsupportedSymbol(
                f"Symbol '{symbol}' is not configured in HUNTER_SYMBOLS or an active Binance USDT spot market"
            )

        key = (self.exchange_id, symbol, timeframe, limit)
        current_time = now or self._now_utc()
        if current_time.tzinfo is None or current_time.utcoffset() is None:
            raise ValueError("now must be a timezone-aware datetime")
        current_time = current_time.astimezone(timezone.utc)
        cached = self._cache.get(key)
        if cached is not None:
            cached_at, candles = cached
            cache_age = (current_time - cached_at).total_seconds()
            if 0 <= cache_age < self.cache_ttl:
                # Re-filter on each call so a previously closed candle cannot
                # be returned if the caller supplies a later clock value.
                closed_cached = self._closed_candles(candles, timeframe, current_time)
                if closed_cached:
                    return closed_cached[-limit:]
            self._cache.pop(key, None)

        try:
            await self._ensure_markets_loaded()
            rows = await self.exchange.fetch_ohlcv(
                symbol, timeframe, since=None, limit=min(limit + 1, 1000)
            )
        except UnsupportedSymbol:
            raise
        except ccxt.BadSymbol as exc:
            raise UnsupportedSymbol(f"Unsupported symbol '{symbol}' on {self.exchange_id}: {exc}") from exc
        except ccxt.RateLimitExceeded as exc:
            raise RateLimited(f"Rate limited by {self.exchange_id}: {exc}") from exc
        except (ccxt.NetworkError, ccxt.ExchangeNotAvailable, ccxt.RequestTimeout) as exc:
            raise ExchangeUnavailable(f"Exchange '{self.exchange_id}' is unavailable: {exc}") from exc
        except ccxt.BaseError as exc:
            raise MarketDataError(f"Market-data request failed: {exc}") from exc

        candles, malformed_count = self._clean_rows(rows, symbol, timeframe)
        if malformed_count:
            logger.warning(
                "Dropped %d malformed OHLCV row(s) for %s %s",
                malformed_count,
                symbol,
                timeframe,
            )
        closed = self._closed_candles(candles, timeframe, current_time)
        if not closed:
            raise MarketDataError(
                f"No valid closed candles returned for {symbol} {timeframe}"
            )
        contiguous = self._most_recent_contiguous_run(closed, timeframe, symbol)
        if not contiguous:
            raise MarketDataError(
                f"No valid closed candles returned for {symbol} {timeframe}"
            )
        result = contiguous[-limit:]
        if self.cache_ttl > 0:
            self._cache[key] = (current_time, result)
        return result

    @staticmethod
    def _clean_rows(
        rows: Any, symbol: str, timeframe: str
    ) -> tuple[list[Candle], int]:
        by_timestamp: dict[datetime, Candle] = {}
        malformed_count = 0
        if not isinstance(rows, (list, tuple)):
            return [], 1
        for row in rows:
            try:
                if not isinstance(row, (list, tuple)) or len(row) < 6:
                    raise ValueError("OHLCV row must contain six values")
                timestamp_ms, open_price, high, low, close, volume = row[:6]
                timestamp = _ccxt_timestamp_to_utc(timestamp_ms)
                candle = Candle.model_validate(
                    {
                        "timestamp": timestamp,
                        "open": open_price,
                        "high": high,
                        "low": low,
                        "close": close,
                        "volume": volume,
                    }
                )
            except (TypeError, ValueError, OverflowError, OSError):
                malformed_count += 1
                continue
            # Assignment in exchange order intentionally makes the last row
            # for a duplicated timestamp win.
            by_timestamp[candle.timestamp] = candle
        return sorted(by_timestamp.values(), key=lambda item: item.timestamp), malformed_count

    @staticmethod
    def _most_recent_contiguous_run(
        candles: list[Candle], timeframe: str, symbol: str
    ) -> list[Candle]:
        """Keep only the newest run whose candle opens are exactly one timeframe apart."""
        if len(candles) < 2:
            return candles

        expected_delta = TIMEFRAME_SECONDS[timeframe]
        run_start = 0
        for index in range(1, len(candles)):
            previous = candles[index - 1].timestamp
            current = candles[index].timestamp
            actual_delta = (current - previous).total_seconds()
            if actual_delta != expected_delta:
                discarded_count = index - run_start
                logger.warning(
                    "Discarded %d older candle(s) for %s %s due to a gap at %s",
                    discarded_count,
                    symbol,
                    timeframe,
                    current.isoformat(),
                )
                run_start = index
        return candles[run_start:]

    @staticmethod
    def _closed_candles(
        candles: list[Candle], timeframe: str, now: datetime
    ) -> list[Candle]:
        seconds = TIMEFRAME_SECONDS[timeframe]
        return [
            candle
            for candle in candles
            if candle.timestamp.timestamp() + seconds <= now.timestamp()
        ]

    async def close(self) -> None:
        self._cache.clear()
        self._active_spot_usdt_markets.clear()
        self._markets_loaded = False
        self._markets_loaded_at = None
        close = getattr(self.exchange, "close", None)
        if callable(close):
            await close()

# END OF FILE backend/app/market_data.py
```

### `backend/app/routes.py`

```python
"""HTTP routes for live market data and causal liquidity analysis."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timezone
from typing import Annotated, get_args

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

# END OF FILE backend/app/routes.py
```

### `backend/tests/test_api.py`

```python
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import app.routes as routes
from app.market_data import (
    ExchangeUnavailable,
    MarketDataError,
    RateLimited,
    UnsupportedSymbol,
    UnsupportedTimeframe,
)
from app.schemas import (
    AnalysisResponse,
    CandlesResponse,
    ErrorResponse,
    LiquidityEvent,
    MarketInfo,
    SummaryResponse,
    Candle,
    TIMEFRAME_SECONDS,
)
from main import app

UTC = timezone.utc
NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
def make_candles(timeframe: str = "15m") -> list[Candle]:
    """Hand-built contiguous candles with a confirmed swing-high sweep."""
    highs = [11, 12, 13, 15, 13, 12, 11, 16, 13, 12, 11, 12, 13, 12, 11]
    closes = [10, 11, 12, 14, 12, 11, 10, 14.5, 12, 11, 10, 11, 12, 11, 10]
    lows = [9, 10, 11, 12, 11, 10, 9, 13, 11, 10, 9, 10, 11, 10, 9]
    step = TIMEFRAME_SECONDS[timeframe]
    start = NOW - timedelta(seconds=step * len(highs))
    candles = []
    for index, (high, close, low) in enumerate(zip(highs, closes, lows)):
        candles.append(
            Candle(
                timestamp=start + timedelta(seconds=step * index),
                open=close,
                high=max(high, close, low),
                low=min(high, close, low),
                close=close,
                volume=10.0 + index,
            )
        )
    return candles


class FakeMarketDataService:
    exchange_id = "fakeexchange"
    supported_symbols = ("BTC/USDT", "ETH/USDT")
    supported_timeframes = ("5m", "15m", "1h", "4h")

    def __init__(self, errors: dict[tuple[str, str], type[Exception]] | None = None):
        self.errors = errors or {}
        self.calls: list[tuple[str, str, int]] = []
        self.closed = False
        self.extra_symbols: set[str] = set()
        self.market_results = [
            {"symbol": f"COIN{index}/USDT", "base": f"COIN{index}"}
            for index in range(25)
        ] + [
            {"symbol": "BTC/USDT", "base": "BTC"},
            {"symbol": "ETH/USDT", "base": "ETH"},
            {"symbol": "SOL/USDT", "base": "SOL"},
        ]

    async def supports_symbol(self, symbol: str) -> bool:
        return symbol in self.supported_symbols or symbol in self.extra_symbols

    async def search_markets(self, query: str, limit: int = 10):
        needle = query.upper()
        return [item for item in self.market_results if needle in item["base"]][:limit]

    async def get_candles(self, symbol: str, timeframe: str, limit: int = 500):
        self.calls.append((symbol, timeframe, limit))
        error = self.errors.get((symbol, timeframe))
        if error:
            raise error(f"fake failure for {symbol} {timeframe}")
        if symbol not in self.supported_symbols and symbol not in self.extra_symbols:
            raise UnsupportedSymbol(f"unsupported symbol: {symbol}")
        if timeframe not in self.supported_timeframes:
            raise UnsupportedTimeframe(f"unsupported timeframe: {timeframe}")
        return make_candles(timeframe)[-limit:]

    async def close(self):
        self.closed = True




@pytest.fixture
def api_client(monkeypatch):
    monkeypatch.setenv("HUNTER_EVENT_TIMEFRAMES", "15m,1h")
    fake = FakeMarketDataService()
    app.dependency_overrides[routes.get_market_data] = lambda: fake
    previous_market_data = getattr(app.state, "market_data", None)
    app.state.market_data = fake
    # Deliberately do not use TestClient as a context manager: that would run
    # the real lifespan and initialize a network-backed CCXT exchange.
    client = TestClient(app)
    yield client, fake
    app.dependency_overrides.clear()
    if previous_market_data is None:
        if hasattr(app.state, "market_data"):
            delattr(app.state, "market_data")
    else:
        app.state.market_data = previous_market_data
    client.close()


def assert_error(response, status_code: int, code: str | None = None):
    assert response.status_code == status_code
    payload = ErrorResponse.model_validate(response.json())
    if code is not None:
        assert payload.code == code


def test_markets_status_and_response_schema(api_client):
    client, fake = api_client
    response = client.get("/api/markets")
    assert response.status_code == 200
    markets = [MarketInfo.model_validate(item) for item in response.json()]
    assert [item.symbol for item in markets] == list(fake.supported_symbols)
    assert all(item.timeframes == list(fake.supported_timeframes) for item in markets)
    assert all(item.exchange == fake.exchange_id for item in markets)


def test_candles_status_schema_and_fake_dependency_used(api_client):
    client, fake = api_client
    response = client.get("/api/candles", params={"symbol": "BTC/USDT", "timeframe": "15m"})
    assert response.status_code == 200
    payload = CandlesResponse.model_validate(response.json())
    assert payload.data_source == "live_exchange"
    assert payload.exchange == fake.exchange_id
    assert len(payload.candles) == 15
    assert fake.calls == [("BTC/USDT", "15m", 500)]


def test_analysis_status_schema_and_default_config(api_client):
    client, fake = api_client
    response = client.get("/api/analysis", params={"symbol": "BTC/USDT", "timeframe": "15m", "limit": 200})
    assert response.status_code == 200
    payload = AnalysisResponse.model_validate(response.json())
    assert payload.candles_analysed == 15
    assert payload.config.swing_bars == 3
    exact_event = next(event for event in payload.events if event.event_type == "SWING_HIGH_SWEEP")
    assert exact_event.symbol == "BTC/USDT"
    assert exact_event.price == 14.5
    assert exact_event.detected_at == NOW - timedelta(seconds=TIMEFRAME_SECONDS["15m"] * 7)
    assert fake.calls == [("BTC/USDT", "15m", 200)]


def test_events_status_schema_and_old_fields(api_client):
    client, fake = api_client
    response = client.get("/api/events")
    assert response.status_code == 200
    events = [LiquidityEvent.model_validate(item) for item in response.json()]
    assert events
    assert fake.calls
    for item in response.json():
        assert {"event_id", "symbol", "timeframe", "event_type", "price", "volume_ratio", "structure", "timestamp"} <= item.keys()


def test_events_filter_by_symbol(api_client):
    client, _ = api_client
    response = client.get("/api/events", params={"symbol": "BTC/USDT"})
    assert response.status_code == 200
    assert response.json()
    assert {event["symbol"] for event in response.json()} == {"BTC/USDT"}


def test_events_filter_by_timeframe(api_client):
    client, _ = api_client
    response = client.get("/api/events", params={"timeframe": "1h"})
    assert response.status_code == 200
    assert response.json()
    assert {event["timeframe"] for event in response.json()} == {"1h"}


def test_events_filter_by_event_type(api_client):
    client, _ = api_client
    response = client.get("/api/events", params={"event_type": "SWING_HIGH_SWEEP"})
    assert response.status_code == 200
    assert response.json()
    assert {event["event_type"] for event in response.json()} == {"SWING_HIGH_SWEEP"}


def test_events_limit_is_applied_after_newest_first_sort(api_client):
    client, _ = api_client
    all_response = client.get("/api/events", params={"limit": 100})
    limited_response = client.get("/api/events", params={"limit": 1})
    assert all_response.status_code == limited_response.status_code == 200
    events = all_response.json()
    assert len(limited_response.json()) == 1
    sort_keys = [(event.get("detected_at") or event["timestamp"], event["event_id"]) for event in events]
    assert sort_keys == sorted(sort_keys, reverse=True)
    assert limited_response.json()[0]["event_id"] == events[0]["event_id"]


def test_event_lookup_returns_matching_event(api_client):
    client, _ = api_client
    events = client.get("/api/events").json()
    assert events
    response = client.get(f"/api/events/{events[0]['event_id']}")
    assert response.status_code == 200
    assert LiquidityEvent.model_validate(response.json()).event_id == events[0]["event_id"]


def test_event_lookup_missing_returns_error_response(api_client):
    client, _ = api_client
    assert_error(client.get("/api/events/EVT-MISSING"), 404, "NOT_FOUND")


@pytest.mark.parametrize(
    ("params", "expected_detail", "expected_code"),
    [
        ({"symbol": "DOGE/USDT"}, "symbol", "UNSUPPORTED_SYMBOL"),
        ({"timeframe": "2m"}, "timeframe", "UNSUPPORTED_TIMEFRAME"),
        ({"event_type": "NOT_AN_EVENT"}, "event_type", "INVALID_EVENT_TYPE"),
    ],
    ids=["unsupported-symbol", "unsupported-timeframe", "unsupported-event-type"],
)
def test_events_unknown_filter_values_return_400(api_client, params, expected_detail, expected_code):
    client, _ = api_client
    response = client.get("/api/events", params=params)
    assert response.status_code == 400
    assert expected_detail in response.json()["detail"]
    assert response.json()["code"] == expected_code


@pytest.mark.parametrize("limit", [0, 1001, "not-a-number"], ids=["below-min", "above-max", "not-integer"])
def test_candles_invalid_limit_returns_422(api_client, limit):
    client, _ = api_client
    response = client.get("/api/candles", params={"symbol": "BTC/USDT", "timeframe": "15m", "limit": limit})
    assert response.status_code == 422


def test_events_invalid_limit_returns_422(api_client):
    client, _ = api_client
    assert client.get("/api/events", params={"limit": 501}).status_code == 422


@pytest.mark.parametrize(
    ("error_type", "status", "code"),
    [
        (RateLimited, 429, "RATE_LIMITED"),
        (ExchangeUnavailable, 503, "EXCHANGE_UNAVAILABLE"),
        (MarketDataError, 502, "EXCHANGE_UNAVAILABLE"),
        (UnsupportedSymbol, 400, "UNSUPPORTED_SYMBOL"),
        (UnsupportedTimeframe, 400, "UNSUPPORTED_TIMEFRAME"),
    ],
    ids=["rate-limited", "exchange-unavailable", "generic-market-data-error", "unsupported-symbol", "unsupported-timeframe"],
)
def test_market_data_exceptions_map_to_error_response(monkeypatch, error_type, status, code):
    monkeypatch.setenv("HUNTER_EVENT_TIMEFRAMES", "15m")
    fake = FakeMarketDataService({("BTC/USDT", "15m"): error_type})
    app.dependency_overrides[routes.get_market_data] = lambda: fake
    client = TestClient(app)
    try:
        response = client.get("/api/candles", params={"symbol": "BTC/USDT", "timeframe": "15m"})
        assert_error(response, status, code)
    finally:
        app.dependency_overrides.clear()
        client.close()


def test_events_skip_one_failing_symbol_and_return_other_events(monkeypatch):
    monkeypatch.setenv("HUNTER_EVENT_TIMEFRAMES", "15m")
    fake = FakeMarketDataService({("ETH/USDT", "15m"): ExchangeUnavailable})
    app.dependency_overrides[routes.get_market_data] = lambda: fake
    client = TestClient(app)
    try:
        response = client.get("/api/events")
        assert response.status_code == 200
        assert response.json()
        returned_symbols = {event["symbol"] for event in response.json()}
        assert "BTC/USDT" in returned_symbols
        assert "ETH/USDT" not in returned_symbols
        assert ("ETH/USDT", "15m", 500) in fake.calls
    finally:
        app.dependency_overrides.clear()
        client.close()



def test_invalid_hunter_event_timeframes_returns_json_error(monkeypatch):
    monkeypatch.setenv("HUNTER_EVENT_TIMEFRAMES", "15m,2m")
    fake = FakeMarketDataService()
    app.dependency_overrides[routes.get_market_data] = lambda: fake
    previous_market_data = getattr(app.state, "market_data", None)
    app.state.market_data = fake
    client = TestClient(app)
    try:
        response = client.get("/api/events")
        assert response.status_code == 400
        assert response.json()["code"] == "UNSUPPORTED_TIMEFRAME"
        assert "HUNTER_EVENT_TIMEFRAMES" in response.json()["detail"]
    finally:
        app.dependency_overrides.clear()
        if previous_market_data is None:
            if hasattr(app.state, "market_data"):
                delattr(app.state, "market_data")
        else:
            app.state.market_data = previous_market_data
        client.close()


def test_summary_status_schema_and_exact_key_set(api_client):
    client, _ = api_client
    response = client.get("/api/summary")
    assert response.status_code == 200
    assert set(response.json()) == {"total_events", "symbols", "event_counts", "data_source", "generated_at"}
    summary = SummaryResponse.model_validate(response.json())
    assert summary.data_source == "live_exchange"
    assert summary.total_events > 0
    events_response = client.get("/api/events", params={"limit": 500})
    assert events_response.status_code == 200
    expected_counts: dict[str, int] = {}
    for event in events_response.json():
        expected_counts[event["event_type"]] = expected_counts.get(event["event_type"], 0) + 1
    assert summary.event_counts == expected_counts
    assert all(isinstance(count, int) and count > 0 for count in summary.event_counts.values())
    assert summary.total_events == sum(summary.event_counts.values())


def test_root_and_health_remain_compatible(api_client):
    client, fake = api_client
    root = client.get("/")
    health = client.get("/health")
    assert root.status_code == 200
    assert root.json() == {"application": "Hunter Signal Lab", "version": "0.1.0", "status": "running"}
    assert health.status_code == 200
    assert health.json()["status"] == "healthy"
    assert health.json()["prediction_model"] == "not_connected"
    assert health.json()["exchange"] == fake.exchange_id

# END OF FILE backend/tests/test_api.py


# Task 8: active Binance USDT spot pair search and watchlist symbol validation.
def test_market_search_matches_base_coin_case_insensitively(api_client):
    client, _ = api_client
    response = client.get("/api/markets/search", params={"q": "sOl"})
    assert response.status_code == 200
    assert response.json() == [{"symbol": "SOL/USDT", "base": "SOL"}]


def test_market_search_respects_limit_and_caps_results_at_20(api_client):
    client, _ = api_client
    limited = client.get("/api/markets/search", params={"q": "COIN", "limit": 3})
    capped = client.get("/api/markets/search", params={"q": "COIN", "limit": 50})
    assert limited.status_code == 200 and len(limited.json()) == 3
    assert capped.status_code == 200 and len(capped.json()) == 20


@pytest.mark.parametrize("query", ["", "A", "coin-name", "BTC/USDT"])
def test_market_search_rejects_invalid_query(api_client, query):
    client, _ = api_client
    response = client.get("/api/markets/search", params={"q": query})
    assert_error(response, 400, "INVALID_QUERY")


def test_analysis_accepts_custom_symbol_from_cached_market_list(api_client):
    client, fake = api_client
    fake.extra_symbols.add("SOL/USDT")
    response = client.get("/api/analysis", params={"symbol": "SOL/USDT", "timeframe": "15m"})
    assert response.status_code == 200
    assert response.json()["symbol"] == "SOL/USDT"
    assert fake.calls == [("SOL/USDT", "15m", 500)]


def test_analysis_rejects_unknown_custom_symbol(api_client):
    client, _ = api_client
    assert_error(
        client.get("/api/analysis", params={"symbol": "NOTREAL/USDT", "timeframe": "15m"}),
        400,
        "UNSUPPORTED_SYMBOL",
    )


def test_events_symbols_rejects_more_than_10_symbols(api_client):
    client, _ = api_client
    requested = ",".join(f"COIN{i}/USDT" for i in range(11))
    assert_error(client.get("/api/events", params={"symbols": requested}), 400, "TOO_MANY_SYMBOLS")


def test_events_symbols_validates_and_adds_custom_symbols(api_client, monkeypatch):
    client, fake = api_client
    monkeypatch.setenv("HUNTER_EVENT_TIMEFRAMES", "15m")
    fake.extra_symbols.add("SOL/USDT")
    response = client.get("/api/events", params={"symbols": "SOL/USDT"})
    assert response.status_code == 200
    assert any(call[0] == "SOL/USDT" for call in fake.calls)


def test_events_symbols_rejects_unsupported_custom_symbol(api_client):
    client, _ = api_client
    assert_error(client.get("/api/events", params={"symbols": "NOTREAL/USDT"}), 400, "UNSUPPORTED_SYMBOL")

# END OF FILE backend/tests/test_api.py
```

### `backend/tests/test_market_data.py`

```python
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import ccxt.async_support as ccxt
import pytest

from app.market_data import (
    ExchangeUnavailable,
    MarketDataError,
    MarketDataService,
    RateLimited,
    UnsupportedSymbol,
    UnsupportedTimeframe,
)

UTC = timezone.utc
NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
TF_SECONDS = 900
BASE_MS = int((NOW - timedelta(minutes=45)).timestamp() * 1000)


def row(offset_minutes=0, *, open_=100, high=105, low=95, close=101, volume=10):
    return [BASE_MS + offset_minutes * 60_000, open_, high, low, close, volume]


class FakeExchange:
    def __init__(self, rows=None, error=None, markets=None):
        self.rows = list(rows or [])
        self.error = error
        self.markets = markets if markets is not None else {"BTC/USDT": {}}
        self.calls = 0
        self.load_calls = 0
        self.limits = []
        self.closed = False

    async def load_markets(self):
        self.load_calls += 1
        return self.markets

    async def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None):
        self.calls += 1
        self.limits.append(limit)
        if self.error is not None:
            raise self.error("test exchange error")
        return self.rows

    async def close(self):
        self.closed = True


def make_service(fake, *, clock=lambda: NOW):
    return MarketDataService(exchange=fake, clock=clock)


@pytest.mark.asyncio
async def test_ccxt_millisecond_timestamp_converts_to_aware_utc():
    fake = FakeExchange([row(offset_minutes=0)])
    service = make_service(fake)
    candles = await service.get_candles("BTC/USDT", "15m", limit=5)
    assert candles[0].timestamp.tzinfo is not None
    assert candles[0].timestamp.utcoffset() == timedelta(0)
    assert candles[0].timestamp == datetime.fromtimestamp(BASE_MS / 1000, UTC)


@pytest.mark.asyncio
async def test_forming_candle_is_dropped():
    forming = [int((NOW - timedelta(minutes=5)).timestamp() * 1000), 100, 105, 95, 101, 10]
    fake = FakeExchange([forming])
    with pytest.raises(MarketDataError, match="No valid closed candles"):
        await make_service(fake).get_candles("BTC/USDT", "15m", limit=5)


@pytest.mark.asyncio
async def test_exactly_closed_candle_is_kept():
    exact_open = int((NOW - timedelta(seconds=TF_SECONDS)).timestamp() * 1000)
    fake = FakeExchange([[exact_open, 100, 105, 95, 101, 10]])
    candles = await make_service(fake).get_candles("BTC/USDT", "15m", limit=1)
    assert len(candles) == 1
    assert candles[0].timestamp + timedelta(seconds=TF_SECONDS) == NOW


@pytest.mark.asyncio
async def test_candles_are_sorted_ascending():
    fake = FakeExchange([row(15), row(-15), row(0)])
    candles = await make_service(fake).get_candles("BTC/USDT", "15m", limit=5)
    assert [c.timestamp for c in candles] == sorted(c.timestamp for c in candles)


@pytest.mark.asyncio
async def test_duplicate_timestamp_keeps_last_row():
    duplicate_first = row(0, close=101)
    duplicate_last = row(0, close=103)
    fake = FakeExchange([duplicate_first, duplicate_last])
    candles = await make_service(fake).get_candles("BTC/USDT", "15m", limit=5)
    assert len(candles) == 1
    assert candles[0].close == 103


@pytest.mark.asyncio
async def test_malformed_row_is_dropped_and_warning_logged(caplog):
    malformed = row(0, high=90, low=95)
    fake = FakeExchange([malformed, row(-15)])
    with caplog.at_level("WARNING"):
        candles = await make_service(fake).get_candles("BTC/USDT", "15m", limit=5)
    assert len(candles) == 1
    assert "Dropped 1 malformed OHLCV row(s)" in caplog.text


@pytest.mark.asyncio
async def test_no_gap_keeps_all_closed_candles_unchanged():
    fake = FakeExchange([row(-60), row(-45), row(-30)])
    candles = await make_service(fake).get_candles("BTC/USDT", "15m", limit=10)
    assert [c.timestamp for c in candles] == [
        _ccxt_open_time(row_item) for row_item in [row(-60), row(-45), row(-30)]
    ]


@pytest.mark.asyncio
async def test_gap_in_middle_keeps_only_later_contiguous_run(caplog):
    fake = FakeExchange([row(-60), row(-45), row(-15), row(0)])
    with caplog.at_level("WARNING"):
        candles = await make_service(fake).get_candles("BTC/USDT", "15m", limit=10)
    assert [c.timestamp for c in candles] == [
        _ccxt_open_time(row(-15)), _ccxt_open_time(row(0))
    ]
    assert "Discarded 2 older candle(s)" in caplog.text
    assert _ccxt_open_time(row(-15)).isoformat() in caplog.text


@pytest.mark.asyncio
async def test_malformed_middle_row_creates_gap_and_keeps_later_run(caplog):
    malformed = row(-45, high=90, low=95)
    fake = FakeExchange([row(-60), malformed, row(-30), row(-15)])
    with caplog.at_level("WARNING"):
        candles = await make_service(fake).get_candles("BTC/USDT", "15m", limit=10)
    assert [c.timestamp for c in candles] == [
        _ccxt_open_time(row(-30)), _ccxt_open_time(row(-15))
    ]
    assert "Dropped 1 malformed OHLCV row(s)" in caplog.text
    assert "Discarded 1 older candle(s)" in caplog.text


@pytest.mark.asyncio
async def test_cached_result_is_contiguous_after_gap_cleanup():
    fake = FakeExchange([row(-60), row(-45), row(-15), row(0)])
    service = make_service(fake)
    first = await service.get_candles("BTC/USDT", "15m", limit=10)
    second = await service.get_candles("BTC/USDT", "15m", limit=10)
    assert first == second
    assert fake.calls == 1
    assert all(
        (right.timestamp - left.timestamp).total_seconds() == TF_SECONDS
        for left, right in zip(second, second[1:])
    )


def _ccxt_open_time(ohlcv_row):
    return datetime.fromtimestamp(ohlcv_row[0] / 1000, UTC)


@pytest.mark.asyncio
async def test_all_malformed_rows_raise_market_data_error():
    fake = FakeExchange([row(0, high=90, low=95), [1, 2]])
    with pytest.raises(MarketDataError, match="No valid closed candles"):
        await make_service(fake).get_candles("BTC/USDT", "15m", limit=5)


@pytest.mark.asyncio
async def test_unsupported_symbol_raises_typed_error(monkeypatch):
    monkeypatch.setenv("HUNTER_SYMBOLS", "BTC/USDT")
    fake = FakeExchange(markets={"BTC/USDT": {}})
    with pytest.raises(UnsupportedSymbol):
        await make_service(fake).get_candles("DOGE/USDT", "15m", limit=5)


@pytest.mark.asyncio
async def test_symbol_in_exchange_markets_but_not_allowlist_is_rejected(monkeypatch):
    monkeypatch.setenv("HUNTER_SYMBOLS", "BTC/USDT")
    fake = FakeExchange(rows=[row(-30)], markets={"BTC/USDT": {}, "DOGE/USDT": {}})
    with pytest.raises(UnsupportedSymbol, match="HUNTER_SYMBOLS"):
        await make_service(fake).get_candles("DOGE/USDT", "15m", limit=1)
    assert fake.calls == 0


@pytest.mark.asyncio
async def test_configured_symbol_missing_from_exchange_markets_is_rejected(monkeypatch):
    monkeypatch.setenv("HUNTER_SYMBOLS", "BTC/USDT,DOGE/USDT")
    fake = FakeExchange(rows=[row(-30)], markets={"BTC/USDT": {}})
    with pytest.raises(UnsupportedSymbol, match="Unsupported symbol"):
        await make_service(fake).get_candles("DOGE/USDT", "15m", limit=1)
    assert fake.calls == 0


def test_supported_properties_are_ordered_tuples_and_read_only(monkeypatch):
    monkeypatch.setenv("HUNTER_EXCHANGE", "binance")
    monkeypatch.setenv("HUNTER_SYMBOLS", "BTC/USDT,ETH/USDT")
    service = make_service(FakeExchange())
    assert service.supported_symbols == ("BTC/USDT", "ETH/USDT")
    assert service.supported_timeframes == ("5m", "15m", "1h", "4h")
    assert service.exchange_id == "binance"
    with pytest.raises(AttributeError):
        service.supported_symbols = ("DOGE/USDT",)
    with pytest.raises(AttributeError):
        service.supported_timeframes = ("1d",)
    with pytest.raises(AttributeError):
        service.exchange_id = "other"


@pytest.mark.asyncio
async def test_unsupported_timeframe_raises_typed_error():
    fake = FakeExchange([row(-15)])
    with pytest.raises(UnsupportedTimeframe):
        await make_service(fake).get_candles("BTC/USDT", "1d", limit=5)


@pytest.mark.parametrize("limit", [0, 1001])
@pytest.mark.asyncio
async def test_limits_outside_range_are_rejected(limit):
    with pytest.raises(ValueError, match="between 1 and 1000"):
        await make_service(FakeExchange([row(-15)])).get_candles(
            "BTC/USDT", "15m", limit=limit
        )


@pytest.mark.parametrize("limit", [1, 1000])
@pytest.mark.asyncio
async def test_limit_boundaries_are_accepted(limit):
    # Supply a single valid closed candle; a valid request need not return the
    # requested count when the exchange has less historical data available.
    fake = FakeExchange([row(-30)])
    candles = await make_service(fake).get_candles(
        "BTC/USDT", "15m", limit=limit
    )
    assert len(candles) == 1
    assert fake.limits == [min(limit + 1, 1000)]


@pytest.mark.asyncio
async def test_explicit_now_overrides_injected_clock_for_closed_filter():
    candle_open_ms = int((NOW - timedelta(minutes=20)).timestamp() * 1000)
    fake = FakeExchange([[candle_open_ms, 100, 105, 95, 101, 10]])
    service = make_service(fake, clock=lambda: NOW - timedelta(hours=1))

    kept = await service.get_candles(
        "BTC/USDT", "15m", limit=1, now=NOW
    )
    assert len(kept) == 1

    fake2 = FakeExchange([[candle_open_ms, 100, 105, 95, 101, 10]])
    service2 = make_service(fake2, clock=lambda: NOW + timedelta(hours=1))
    with pytest.raises(MarketDataError, match="No valid closed candles"):
        await service2.get_candles(
            "BTC/USDT", "15m", limit=1, now=NOW - timedelta(minutes=20)
        )


@pytest.mark.asyncio
async def test_cache_hit_within_ttl_calls_fake_once(monkeypatch):
    monkeypatch.setenv("HUNTER_CACHE_TTL", "15")
    fake = FakeExchange([row(-30)])
    service = make_service(fake)
    await service.get_candles("BTC/USDT", "15m", limit=1)
    await service.get_candles("BTC/USDT", "15m", limit=1)
    assert fake.calls == 1


@pytest.mark.asyncio
async def test_cache_refetches_after_ttl(monkeypatch):
    monkeypatch.setenv("HUNTER_CACHE_TTL", "1")
    fake = FakeExchange([row(-30)])
    clock_time = [NOW]
    service = make_service(fake, clock=lambda: clock_time[0])
    await service.get_candles("BTC/USDT", "15m", limit=1)
    await service.get_candles("BTC/USDT", "15m", limit=1)
    assert fake.calls == 1
    clock_time[0] = NOW + timedelta(seconds=2)
    await service.get_candles("BTC/USDT", "15m", limit=1)
    assert fake.calls == 2


@pytest.mark.parametrize(
    ("ccxt_error", "expected_error"),
    [
        (ccxt.BadSymbol, UnsupportedSymbol),
        (ccxt.RateLimitExceeded, RateLimited),
        (ccxt.NetworkError, ExchangeUnavailable),
        (ccxt.ExchangeNotAvailable, ExchangeUnavailable),
        (ccxt.RequestTimeout, ExchangeUnavailable),
        (ccxt.ExchangeError, MarketDataError),
    ],
)
@pytest.mark.asyncio
async def test_ccxt_errors_map_to_typed_errors(ccxt_error, expected_error):
    fake = FakeExchange([row(-30)], error=ccxt_error)
    with pytest.raises(expected_error):
        await make_service(fake).get_candles("BTC/USDT", "15m", limit=1)


@pytest.mark.asyncio
async def test_rate_limit_exceeded_is_not_mapped_to_exchange_unavailable():
    fake = FakeExchange([row(-30)], error=ccxt.RateLimitExceeded)
    with pytest.raises(RateLimited) as caught:
        await make_service(fake).get_candles("BTC/USDT", "15m", limit=1)
    assert not isinstance(caught.value, ExchangeUnavailable)


@pytest.mark.asyncio
async def test_returns_requested_closed_limit_when_last_exchange_row_is_forming():
    # Three closed rows followed by one forming row; request limit=3 asks CCXT
    # for four rows and returns the three closed rows only.
    rows = [row(-60), row(-45), row(-30)]
    forming = [int((NOW - timedelta(minutes=5)).timestamp() * 1000), 100, 105, 95, 101, 10]
    fake = FakeExchange(rows + [forming])
    candles = await make_service(fake).get_candles("BTC/USDT", "15m", limit=3)
    assert len(candles) == 3
    assert fake.limits == [4]
    assert all(c.timestamp + timedelta(seconds=TF_SECONDS) <= NOW for c in candles)


def test_unknown_exchange_id_raises_clear_value_error(monkeypatch):
    monkeypatch.setenv("HUNTER_EXCHANGE", "not_a_real_exchange")
    with pytest.raises(ValueError, match="Unknown HUNTER_EXCHANGE"):
        MarketDataService()


@pytest.mark.asyncio
async def test_close_closes_injected_exchange():
    fake = FakeExchange([row(-30)])
    service = make_service(fake)
    await service.close()
    assert fake.closed is True

@pytest.mark.asyncio
async def test_search_markets_filters_active_binance_spot_usdt_and_caches_one_hour():
    markets = {
        "BTC/USDT": {"base": "BTC", "quote": "USDT", "spot": True, "active": True},
        "ETH/USDT": {"base": "ETH", "quote": "USDT", "spot": True, "active": True},
        "ETH/BTC": {"base": "ETH", "quote": "BTC", "spot": True, "active": True},
        "ETH/USDT:USDT": {"base": "ETH", "quote": "USDT", "spot": False, "active": True},
        "BAD/USDT": {"base": "BAD", "quote": "USDT", "spot": True, "active": False},
        "XRP/USDT": {"base": "XRP", "quote": "USDT", "spot": True, "active": True},
    }
    current = [NOW]
    fake = FakeExchange(markets=markets)
    service = MarketDataService(exchange=fake, clock=lambda: current[0])

    assert await service.search_markets("eth") == [{"symbol": "ETH/USDT", "base": "ETH"}]
    assert await service.search_markets("USDT", limit=20) == []
    assert fake.load_calls == 1

    current[0] += timedelta(minutes=59)
    await service.search_markets("BTC")
    assert fake.load_calls == 1

    current[0] += timedelta(minutes=2)
    await service.search_markets("XRP")
    assert fake.load_calls == 2
    await service.close()

# END OF FILE backend/tests/test_market_data.py
```

### `frontend/src/App.tsx`

```tsx
import { useCallback, useEffect, useMemo, useState, useRef } from "react";
import "./App.css";
import LiquidityChart from "./components/LiquidityChart";
import {
  getAnalysis,
  getCandles,
  getEvents,
  getMarkets,
  getSummary,
  searchMarkets,
} from "./services/api";
import type {
  LiquidityEvent,
  Market,
  MarketCandle,
  LiquidityLevel,
  SummaryResponse,
} from "./types/market";
import type { MarketSearchResult } from "./services/api";

const DEFAULT_LIMIT = 300;
const WATCHLIST_KEY = "hunter.watchlist.v1";
const PRIORITY_WINDOW_MS = 24 * 60 * 60 * 1000;

function readWatchlist(): string[] {
  try {
    const raw = window.localStorage.getItem(WATCHLIST_KEY);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return [...new Set(parsed.filter((item): item is string => typeof item === "string"))].slice(0, 20);
  } catch {
    return [];
  }
}

const formatEvent = (value: string) => value.replaceAll("_", " ").toLowerCase();

const formatPrice = (value: number) =>
  value.toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 6,
  });

const formatUtcTime = (value: string) => {
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return value;
  return `${date.toLocaleString("en-GB", {
    timeZone: "UTC",
    year: "numeric",
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  })} UTC`;
};

function App() {
  const [markets, setMarkets] = useState<Market[]>([]);
  const [symbol, setSymbol] = useState("");
  const [timeframe, setTimeframe] = useState("");
  const [events, setEvents] = useState<LiquidityEvent[]>([]);
  const [candles, setCandles] = useState<MarketCandle[]>([]);
  const [levels, setLevels] = useState<LiquidityLevel[]>([]);
  const [summary, setSummary] = useState<SummaryResponse | null>(null);
  const [selectedId, setSelectedId] = useState("");
  const [filter, setFilter] = useState("ALL");
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(true);
  const [marketsLoading, setMarketsLoading] = useState(true);
  const [error, setError] = useState("");
  const [refreshToken, setRefreshToken] = useState(0);
  const [lastUpdated, setLastUpdated] = useState("");
  const lastSuccessfulFetchAt = useRef<number | null>(null);
  const activeSelection = useRef("");
  const pendingEventId = useRef("");
  const [engineConfig, setEngineConfig] = useState<Record<string, unknown> | null>(null);
  const [watchlist, setWatchlist] = useState<string[]>(readWatchlist);
  const [watchOnly, setWatchOnly] = useState(false);
  const [priorityEvents, setPriorityEvents] = useState<LiquidityEvent[]>([]);
  const [priorityLoadedAt, setPriorityLoadedAt] = useState(0);
  const [priorityError, setPriorityError] = useState("");
  const [pairSearch, setPairSearch] = useState("");
  const [pairSearchResults, setPairSearchResults] = useState<MarketSearchResult[]>([]);
  const [pairSearchLoading, setPairSearchLoading] = useState(false);
  const [pairSearchError, setPairSearchError] = useState("");

  const refresh = useCallback(() => {
    setRefreshToken((current) => current + 1);
  }, []);

  useEffect(() => {
    const controller = new AbortController();

    async function loadInitialData() {
      setMarketsLoading(true);
      try {
        const [marketData, summaryData] = await Promise.all([
          getMarkets(controller.signal),
          getSummary(controller.signal),
        ]);
        setMarkets(marketData);
        setSummary(summaryData);
        const firstMarket = marketData[0];
        if (firstMarket) {
          setSymbol((current) => current || firstMarket.symbol);
          setTimeframe((current) => current || firstMarket.timeframes[0] || "");
        }
      } catch (error: unknown) {
        if (error instanceof Error && error.name === "AbortError") return;
        setError(error instanceof Error ? error.message : "Could not load markets.");
      } finally {
        if (!controller.signal.aborted) setMarketsLoading(false);
      }
    }

    void loadInitialData();
    return () => controller.abort();
  }, []);

  const availableTimeframes = useMemo(
    () => markets.find((market) => market.symbol === symbol)?.timeframes ?? ["5m", "15m", "1h", "4h"],
    [markets, symbol],
  );

  useEffect(() => {
    if (!symbol || !timeframe) return;

    const controller = new AbortController();
    async function loadSelectedMarket() {
      setLoading(true);
      setError("");
      try {
        const selectionKey = `${symbol}::${timeframe}`;
        if (activeSelection.current !== selectionKey) {
          activeSelection.current = selectionKey;
          setCandles([]);
          setLevels([]);
          setEvents([]);
          setSelectedId("");
        }

        const [analysis, candleResponse, summaryData] = await Promise.all([
          getAnalysis(symbol, timeframe, DEFAULT_LIMIT, controller.signal),
          getCandles(symbol, timeframe, DEFAULT_LIMIT, controller.signal),
          getSummary(controller.signal),
        ]);
        const eventData = [...analysis.events].sort((left, right) => {
          const detectedOrder = (right.detected_at ?? "").localeCompare(left.detected_at ?? "");
          return detectedOrder || right.event_id.localeCompare(left.event_id);
        });
        setLevels(analysis.levels);
        setCandles(candleResponse.candles);
        setEvents(eventData);
        setSummary(summaryData);
        setEngineConfig(analysis.config);
        const wantedId = pendingEventId.current;
        pendingEventId.current = "";
        setSelectedId((current) => {
          if (wantedId && eventData.some((event) => event.event_id === wantedId)) {
            return wantedId;
          }
          return eventData.some((event) => event.event_id === current)
            ? current
            : eventData[0]?.event_id ?? "";
        });
        const successTime = Date.now();
        lastSuccessfulFetchAt.current = successTime;
        setLastUpdated(new Date(successTime).toISOString());
      } catch (error: unknown) {
        if (error instanceof Error && error.name === "AbortError") return;
        setError(error instanceof Error ? error.message : "Could not load market data.");
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    }

    void loadSelectedMarket();
    return () => controller.abort();
  }, [symbol, timeframe, refreshToken]);

  useEffect(() => {
    try {
      window.localStorage.setItem(WATCHLIST_KEY, JSON.stringify(watchlist));
    } catch {
      // Storage can be unavailable (private mode); the watchlist then lasts for this visit only.
    }
  }, [watchlist]);

  useEffect(() => {
    const controller = new AbortController();

    async function loadPriorityEvents() {
      try {
        let data: LiquidityEvent[];
        if (watchOnly && watchlist.length > 0) {
          const batches: string[][] = [];
          for (let index = 0; index < watchlist.length; index += 10) {
            batches.push(watchlist.slice(index, index + 10));
          }
          const results = await Promise.all(
            batches.map((batch) => getEvents(500, controller.signal, batch)),
          );
          const unique = new Map<string, LiquidityEvent>();
          for (const event of results.flat()) unique.set(event.event_id, event);
          data = [...unique.values()];
        } else {
          data = await getEvents(500, controller.signal);
        }
        setPriorityEvents(data);
        setPriorityLoadedAt(Date.now());
        setPriorityError("");
      } catch (error: unknown) {
        if (error instanceof Error && error.name === "AbortError") return;
        setPriorityError(error instanceof Error ? error.message : "Could not load top events.");
      }
    }

    void loadPriorityEvents();
    return () => controller.abort();
  }, [refreshToken, watchOnly, watchlist]);

  useEffect(() => {
    const query = pairSearch.trim();
    if (query.length < 2) {
      setPairSearchResults([]);
      setPairSearchLoading(false);
      setPairSearchError("");
      return;
    }
    const controller = new AbortController();
    const timer = window.setTimeout(async () => {
      setPairSearchLoading(true);
      setPairSearchError("");
      try {
        const results = await searchMarkets(query, 10, controller.signal);
        setPairSearchResults(results);
      } catch (error: unknown) {
        if (error instanceof Error && error.name === "AbortError") return;
        setPairSearchError(error instanceof Error ? error.message : "Could not search markets.");
        setPairSearchResults([]);
      } finally {
        if (!controller.signal.aborted) setPairSearchLoading(false);
      }
    }, 400);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [pairSearch]);

  useEffect(() => {
    const refreshIfDue = () => {
      if (document.visibilityState !== "visible") return;
      const lastSuccess = lastSuccessfulFetchAt.current;
      if (lastSuccess !== null && Date.now() - lastSuccess < 30_000) return;
      setRefreshToken((current) => current + 1);
    };

    const timer = window.setInterval(refreshIfDue, 60_000);
    const handleVisibilityChange = () => {
      if (document.visibilityState === "visible") refreshIfDue();
    };

    document.addEventListener("visibilitychange", handleVisibilityChange);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", handleVisibilityChange);
    };
  }, []);

  useEffect(() => {
    if (!availableTimeframes.includes(timeframe) && availableTimeframes[0]) {
      setTimeframe(availableTimeframes[0]);
    }
  }, [availableTimeframes, timeframe]);

  const topEvents = useMemo(() => {
    const cutoff = priorityLoadedAt - PRIORITY_WINDOW_MS;
    const watched = new Set(watchlist);
    return priorityEvents
      .filter((event) => {
        const known = Date.parse(event.detected_at ?? event.timestamp);
        return (
          Number.isFinite(known) &&
          known >= cutoff &&
          (!watchOnly || watched.has(event.symbol))
        );
      })
      .sort(
        (left, right) =>
          right.volume_ratio - left.volume_ratio ||
          right.event_id.localeCompare(left.event_id),
      )
      .slice(0, 5);
  }, [priorityEvents, priorityLoadedAt, watchlist, watchOnly]);

  const isWatched = watchlist.includes(symbol);
  const watchedMarkets = watchlist;
  const selectableMarkets = useMemo(() => {
    const bySymbol = new Map(markets.map((market) => [market.symbol, market]));
    for (const watched of watchlist) {
      if (!bySymbol.has(watched)) {
        bySymbol.set(watched, { symbol: watched, timeframes: ["5m", "15m", "1h", "4h"], exchange: "binance" });
      }
    }
    return [...bySymbol.values()];
  }, [markets, watchlist]);

  function toggleWatch(target: string) {
    if (watchlist.includes(target)) {
      setWatchlist(watchlist.filter((value) => value !== target));
      return;
    }
    if (watchlist.length >= 20) {
      setPairSearchError("Watchlist limit reached (20 pairs). Remove a pair before adding another.");
      return;
    }
    setWatchlist([...watchlist, target]);
    setPairSearchError("");
  }

  function addSearchResult(result: MarketSearchResult) {
    if (!watchlist.includes(result.symbol) && watchlist.length >= 20) {
      setPairSearchError("Watchlist limit reached (20 pairs). Remove a pair before adding another.");
      return;
    }
    if (!watchlist.includes(result.symbol)) {
      setWatchlist([...watchlist, result.symbol]);
    }
    setSymbol(result.symbol);
    setTimeframe((current) => current || "15m");
    setPairSearchError("");
  }

  function focusEvent(target: LiquidityEvent) {
    setFilter("ALL");
    setSearch("");
    if (target.symbol === symbol && target.timeframe === timeframe) {
      setSelectedId(target.event_id);
    } else {
      pendingEventId.current = target.event_id;
      setSymbol(target.symbol);
      setTimeframe(target.timeframe);
    }
    document.getElementById("event-explorer")?.scrollIntoView({ behavior: "smooth" });
  }

  const filteredEvents = useMemo(() => {
    const query = search.trim().toLowerCase();
    return events.filter((event) => {
      const matchesType = filter === "ALL" || event.event_type === filter;
      const matchesSearch = [
        event.event_id,
        event.symbol,
        event.event_type,
        event.structure,
      ].join(" ").toLowerCase().includes(query);
      return matchesType && matchesSearch;
    });
  }, [events, filter, search]);

  const selectedEvent =
    filteredEvents.find((event) => event.event_id === selectedId) ?? filteredEvents[0];

  const symbolsCount = summary?.symbols.length ?? markets.length;
  const sweeps = events.filter((event) => event.event_type.toUpperCase().includes("SWEEP")).length;
  const breakouts = events.filter((event) =>
    event.event_type.toUpperCase().includes("BREAKOUT"),
  ).length;
  const eventTypes = [
    "ALL",
    ...Array.from(new Set(events.map((event) => event.event_type))),
  ];

  function downloadReport() {
    const report = {
      application: "Hunter Signal Lab",
      generated_at: new Date().toISOString(),
      source: "Analysis response for selected market and timeframe",
      symbol,
      timeframe,
      data_source: summary?.data_source ?? "unknown",
      engine_config: engineConfig,
      times_are: "UTC; timestamp is the candle open, detected_at is when the event became knowable",
      event_count: filteredEvents.length,
      events: filteredEvents,
      note: "Event observations only. No AI predictions are included.",
    };
    const blob = new Blob([JSON.stringify(report, null, 2)], {
      type: "application/json",
    });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = "hunter-events-report.json";
    link.click();
    URL.revokeObjectURL(url);
  }

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <a className="brand" href="#">
          <span className="brand-mark">H</span>
          <span>
            <strong>HUNTER</strong>
            <small>SIGNAL LAB</small>
          </span>
        </a>
        <div className="nav-label">WORKSPACE</div>
        <button className="nav-item active" onClick={() => window.scrollTo({ top: 0, behavior: "smooth" })}>
          <span>◫</span> Overview
        </button>
        <button className="nav-item" onClick={() => document.getElementById("priority-panel")?.scrollIntoView({ behavior: "smooth" })}>
          <span>★</span> Top 5 &amp; watchlist
        </button>
        <button className="nav-item" onClick={() => document.getElementById("event-explorer")?.scrollIntoView({ behavior: "smooth" })}>
          <span>⌁</span> Event explorer
        </button>
        <button className="nav-item" onClick={() => document.getElementById("prediction-panel")?.scrollIntoView({ behavior: "smooth" })}>
          <span>◎</span> AI predictions
        </button>
        <div className="sidebar-bottom">
          <div className="model-card">
            <div className="model-heading"><span className="status-dot" />SYSTEM STATUS</div>
            <strong>{loading || marketsLoading ? "Connecting..." : error ? "Offline" : "Connected"}</strong>
            <p>{error ? "API connection failed" : "Local research environment"}</p>
          </div>
          <div className="sidebar-footer">HUNTER SIGNAL LAB · v0.1.0</div>
        </div>
      </aside>

      <main className="main-content">
        <header className="topbar">
          <div className="breadcrumb">Workspace <span>/</span> <strong>Overview</strong></div>
          <div className="topbar-right">
            <span className="live-indicator"><span className="status-dot" />API {error ? "DISCONNECTED" : loading || marketsLoading ? "CONNECTING" : "CONNECTED"}</span>
            <div className="avatar">H</div>
          </div>
        </header>

        <section className="page-content">
          <div className="page-heading">
            <div>
              <div className="eyebrow">MARKET INTELLIGENCE / OVERVIEW</div>
              <h1>Signal Overview<span>.</span></h1>
              <p>Understand liquidity events. Review the evidence. Stay focused.</p>
            </div>
            <div className="heading-actions">
              <button className="secondary-button" onClick={refresh} disabled={loading}>↻ Refresh</button>
              <button className="primary-button" onClick={downloadReport} disabled={loading || !!error || filteredEvents.length === 0}>↓ Export report</button>
            </div>
          </div>

          {error && (
            <div className="error-banner" role="alert">
              <strong>Unable to load backend data.</strong>
              <span>{error}</span>
              <button onClick={refresh}>Retry</button>
            </div>
          )}

          <div className="metrics-grid">
            <Metric label="TOTAL EVENTS" value={loading ? "—" : String(summary?.total_events ?? events.length).padStart(2, "0")} note="All markets, 15m and 1h only" icon="⌁" />
            <Metric label="MARKETS TRACKED" value={loading ? "—" : String(symbolsCount).padStart(2, "0")} note="Unique trading pairs" icon="◈" />
            <Metric label="LIQUIDITY SWEEPS" value={loading ? "—" : String(sweeps).padStart(2, "0")} note="Selected market and timeframe" icon="↗" />
            <Metric label="BREAKOUT EVENTS" value={loading ? "—" : String(breakouts).padStart(2, "0")} note="Breakouts and failures" icon="⌁" />
          </div>

          <section className="priority-section" id="priority-panel">
            <div className="section-heading">
              <div><h2>Top 5 events, last 24 hours</h2><p>Ranked by volume ratio only. An event-priority list, not a prediction or a trade signal.</p></div>
              <label className="watch-only-toggle">
                <input type="checkbox" checked={watchOnly} onChange={(event) => setWatchOnly(event.currentTarget.checked)} />
                Watchlist only
              </label>
            </div>
            <div className="priority-panel">
              {priorityError ? <p className="empty-state">{priorityError}</p> : topEvents.length === 0 ? (
                <p className="empty-state">{watchOnly && watchlist.length === 0 ? "Your watchlist is empty. Add a coin with the star button in Market selection." : "No events in the last 24 hours for this view."}</p>
              ) : (
                <ol className="priority-list">
                  {topEvents.map((event, index) => (
                    <li key={event.event_id}>
                      <button className="priority-item" onClick={() => focusEvent(event)}>
                        <span className="priority-rank">{index + 1}</span>
                        <span className="priority-main"><strong>{event.symbol}</strong><small>{event.timeframe} · {formatEvent(event.event_type)}</small></span>
                        <span className="priority-meta"><strong>{event.volume_ratio.toFixed(2)}×</strong><small>{formatUtcTime(event.detected_at ?? event.timestamp)}</small></span>
                      </button>
                    </li>
                  ))}
                </ol>
              )}
            </div>
          </section>

          <div className="section-heading">
            <div><h2>Market activity</h2><p>Distribution of the selected market's recorded event types</p></div>
            <span className="subtle-tag">{summary?.data_source ?? "API DATA"}</span>
          </div>
          <section className="activity-panel">
            {loading ? <p className="empty-state">Loading event activity...</p> : events.length === 0 ? <p className="empty-state">No events have been recorded for this selection.</p> : (
              <div className="activity-bars">
                {Array.from(new Set(events.map((event) => event.event_type))).map((type) => {
                  const count = events.filter((event) => event.event_type === type).length;
                  return <div className="activity-row" key={type}>
                    <span className="activity-name">{formatEvent(type)}</span>
                    <div className="bar-track"><div className="bar-fill" style={{ width: `${(count / events.length) * 100}%` }} /></div>
                    <span className="activity-count">{count}</span>
                  </div>;
                })}
              </div>
            )}
            <div className="panel-footnote">
              {lastUpdated ? `Last updated ${formatUtcTime(lastUpdated)}. Times are UTC.` : "Waiting for the first successful data refresh."}
            </div>
          </section>

          <section className="market-section">
            <div className="section-heading">
              <div><h2>Market selection</h2><p>Choose the market and timeframe to inspect.</p></div>
              <span className="subtle-tag">CANDLE LIMIT {DEFAULT_LIMIT}</span>
            </div>
            <div className="market-controls">
              <label>Symbol
                <select value={symbol} onChange={(event) => setSymbol(event.currentTarget.value)} disabled={marketsLoading || selectableMarkets.length === 0}>
                  {selectableMarkets.map((market) => <option key={market.symbol} value={market.symbol}>{`${watchlist.includes(market.symbol) ? "★ " : ""}${market.symbol} · ${market.exchange}`}</option>)}
                </select>
              </label>
              <label>Timeframe
                <select value={timeframe} onChange={(event) => setTimeframe(event.currentTarget.value)} disabled={!symbol || availableTimeframes.length === 0}>
                  {availableTimeframes.map((value) => <option key={value} value={value}>{value}</option>)}
                </select>
              </label>
              <span className="selection-status">{marketsLoading ? "Loading markets…" : symbol && timeframe ? `${symbol} / ${timeframe}` : "No markets available"}</span>
            </div>
            <div className="pair-search-panel">
              <label htmlFor="pair-search-input">Search Binance USDT spot pairs</label>
              <input id="pair-search-input" value={pairSearch} onChange={(event) => setPairSearch(event.currentTarget.value)} placeholder="Search base coin, e.g. SOL" autoComplete="off" />
              <p className="pair-search-hint">Enter 2–20 letters or digits. Search waits 400 ms after typing.</p>
              {pairSearchLoading && <p className="pair-search-hint">Searching active spot markets…</p>}
              {pairSearchError && <p className="pair-search-error" role="alert">{pairSearchError}</p>}
              {pairSearchResults.length > 0 && <ul className="pair-search-results">{pairSearchResults.map((result) => <li key={result.symbol}><span><strong>{result.base}</strong><small>{result.symbol}</small></span><button onClick={() => addSearchResult(result)} disabled={watchlist.includes(result.symbol)}>{watchlist.includes(result.symbol) ? "Added" : "Add to watchlist"}</button></li>)}</ul>}
              {pairSearch.trim().length >= 2 && !pairSearchLoading && !pairSearchError && pairSearchResults.length === 0 && <p className="pair-search-hint">No matching active Binance USDT spot pairs.</p>}
            </div>
            <div className="watchlist-row">
              <button className={`star-button${isWatched ? " active" : ""}`} onClick={() => toggleWatch(symbol)} disabled={!symbol} aria-pressed={isWatched}>
                {isWatched ? "★ In watchlist" : "☆ Add to watchlist"}
              </button>
              {watchedMarkets.map((value) => (
                <span className="watch-chip" key={value}>
                  <button onClick={() => setSymbol(value)}>{value}</button>
                  <button aria-label={`Remove ${value} from watchlist`} onClick={() => toggleWatch(value)}>×</button>
                </span>
              ))}
            </div>
            {markets.length === 0 && !marketsLoading && !error && <p className="empty-state">The backend returned no markets.</p>}
          </section>

          {symbol && timeframe && <LiquidityChart symbol={symbol} timeframe={timeframe} candles={candles} levels={levels} events={events} loading={loading} />}

          <div className="explorer-layout" id="event-explorer">
            <section className="events-section">
              <div className="section-heading">
                <div><h2>Event explorer</h2><p>Events for the selected symbol and timeframe. Displayed timestamps are UTC.</p></div>
                <span className="subtle-tag">{filteredEvents.length} RECORDS</span>
              </div>
              <div className="filters">
                <div className="search-box"><span>⌕</span><input aria-label="Search events" placeholder="Search symbol, event or ID..." value={search} onChange={(event) => setSearch(event.currentTarget.value)} /></div>
                <select aria-label="Filter event type" value={filter} onChange={(event) => setFilter(event.currentTarget.value)}>
                  {eventTypes.map((type) => <option key={type} value={type}>{type === "ALL" ? "All event types" : formatEvent(type)}</option>)}
                </select>
              </div>
              <div className="table-wrap">
                <table>
                  <thead><tr><th>EVENT / MARKET</th><th>TYPE</th><th>UTC TIME</th><th>PRICE</th><th>VOLUME RATIO</th></tr></thead>
                  <tbody>
                    {filteredEvents.map((event) => <tr key={event.event_id} className={selectedEvent?.event_id === event.event_id ? "selected-row" : ""} onClick={() => setSelectedId(event.event_id)}>
                      <td><strong>{event.event_id}</strong><span className="cell-subtitle">{event.symbol}</span></td>
                      <td><span className={`event-badge ${event.event_type.toLowerCase()}`}>{formatEvent(event.event_type)}</span></td>
                      <td>{formatUtcTime(event.timestamp)}</td>
                      <td className="price-cell">{formatPrice(event.price)}</td>
                      <td>{event.volume_ratio.toFixed(2)}×</td>
                    </tr>)}
                    {!loading && filteredEvents.length === 0 && <tr><td colSpan={5} className="empty-state">{events.length === 0 ? "No events for this market and timeframe." : "No events match your filters."}</td></tr>}
                  </tbody>
                </table>
              </div>
            </section>

            <aside className="detail-panel">
              <div className="detail-header"><span className="eyebrow">SELECTED EVENT</span><span className="detail-symbol">◈</span></div>
              {selectedEvent ? <>
                <h3>{selectedEvent.symbol}</h3>
                <p className="detail-id">{selectedEvent.event_id}</p>
                <span className="event-badge">{formatEvent(selectedEvent.event_type)}</span>
                <div className="detail-price"><span>Recorded price</span><strong>{formatPrice(selectedEvent.price)}</strong></div>
                <div className="detail-fields">
                  <DetailField label="TIMEFRAME" value={selectedEvent.timeframe} />
                  <DetailField label="VOLUME RATIO" value={`${selectedEvent.volume_ratio.toFixed(2)}×`} />
                  <DetailField label="MARKET STRUCTURE" value={formatEvent(selectedEvent.structure)} />
                  <DetailField label="TIMESTAMP (UTC)" value={formatUtcTime(selectedEvent.timestamp)} />
                </div>
                <div className="evidence-note"><strong>Evidence record</strong><p>This panel displays supplied event attributes. It does not establish a trade outcome or predict future prices.</p></div>
              </> : <p className="empty-state">Select an event to inspect it.</p>}
            </aside>
          </div>

          <section className="prediction-panel" id="prediction-panel">
            <div className="prediction-icon">◎</div>
            <div className="prediction-copy"><span className="eyebrow">MACHINE LEARNING</span><h2>Prediction engine (later phase)</h2><p>The event dashboard is connected. Predictive inference is not yet configured because we still need labeled historical examples and a validated prediction target.</p></div>
            <span className="model-status">NOT CONNECTED</span>
          </section>
          <footer className="page-footer"><span>HUNTER SIGNAL LAB</span><span>Research tooling · Not financial advice</span></footer>
        </section>
      </main>
    </div>
  );
}

function Metric({ label, value, note, icon }: { label: string; value: string; note: string; icon: string }) {
  return <div className="metric-card"><div className="metric-top"><span>{label}</span><span className="metric-icon">{icon}</span></div><strong className="metric-value">{value}</strong><span className="metric-note">{note}</span></div>;
}

function DetailField({ label, value }: { label: string; value: string }) {
  return <div className="detail-field"><span>{label}</span><strong>{value}</strong></div>;
}

export default App;

// END OF FILE frontend/src/App.tsx
```

### `frontend/src/App.css`

```css

@import url("https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=DM+Sans:wght@400;500;600;700&display=swap");

:root {
  font-family: "DM Sans", sans-serif;
  color: #e4e9e5;
  background: #0b0e0c;
  font-synthesis: none;
  text-rendering: optimizeLegibility;
  -webkit-font-smoothing: antialiased;
  --bg: #0b0e0c;
  --panel: #111612;
  --panel-raised: #151b16;
  --border: #252e27;
  --muted: #87928a;
  --green: #a4e5a9;
  --green-dark: #294a30;
}

* {
  box-sizing: border-box;
}

body {
  margin: 0;
  min-width: 320px;
  min-height: 100vh;
}

button,
input,
select {
  font: inherit;
}

button {
  cursor: pointer;
}

button:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

button:focus-visible,
input:focus-visible,
select:focus-visible {
  outline: 2px solid var(--green);
  outline-offset: 3px;
}

.app-shell {
  min-height: 100vh;
}

.sidebar {
  width: 238px;
  position: fixed;
  inset: 0 auto 0 0;
  z-index: 5;
  padding: 27px 17px;
  background: #0d110e;
  border-right: 1px solid var(--border);
  display: flex;
  flex-direction: column;
}

.brand {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 2px 9px 38px;
  color: inherit;
  text-decoration: none;
}

.brand-mark {
  display: grid;
  place-items: center;
  width: 39px;
  height: 39px;
  background: var(--green);
  color: #0b150c;
  border-radius: 9px;
  font-size: 23px;
  font-weight: 800;
}

.brand strong,
.brand small {
  display: block;
}

.brand strong {
  letter-spacing: 2px;
  font-size: 15px;
}

.brand small {
  color: var(--muted);
  font-size: 10px;
  letter-spacing: 2.1px;
  margin-top: 4px;
}

.nav-label,
.eyebrow {
  font-size: 10px;
  letter-spacing: 1.5px;
  color: var(--muted);
  font-weight: 600;
}

.nav-label {
  padding: 0 11px 13px;
}

.nav-item {
  width: 100%;
  border: 1px solid transparent;
  background: transparent;
  color: #9aa49c;
  text-align: left;
  padding: 12px 13px;
  border-radius: 7px;
  display: flex;
  gap: 12px;
  align-items: center;
  font-size: 13px;
  margin-bottom: 5px;
}

.nav-item > span {
  font-size: 19px;
}

.nav-item.active {
  background: #19251a;
  border-color: #304a32;
  color: var(--green);
}

.nav-item:hover {
  color: var(--green);
  background: #151c16;
}

.sidebar-bottom {
  margin-top: auto;
}

.model-card {
  padding: 14px;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 8px;
}

.model-heading,
.live-indicator {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 9px;
  letter-spacing: 1px;
  color: var(--muted);
}

.status-dot {
  width: 7px;
  height: 7px;
  flex: 0 0 auto;
  border-radius: 50%;
  background: #77cf85;
}

.model-card > strong {
  display: block;
  margin-top: 15px;
  font-size: 14px;
}

.model-card p {
  font-size: 11px;
  color: var(--muted);
  margin: 5px 0 0;
}

.sidebar-footer {
  color: #69746b;
  font-size: 9px;
  letter-spacing: 1px;
  margin: 22px 2px 0;
}

.main-content {
  margin-left: 238px;
  min-height: 100vh;
}

.topbar {
  height: 66px;
  padding: 0 35px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  border-bottom: 1px solid var(--border);
  background: #0d100e;
}

.breadcrumb {
  font-size: 12px;
  color: var(--muted);
}

.breadcrumb span {
  padding: 0 10px;
  color: #485149;
}

.breadcrumb strong {
  color: #dce3dd;
  font-weight: 500;
}

.topbar-right {
  display: flex;
  align-items: center;
  gap: 20px;
}

.live-indicator {
  font-size: 9px;
}

.avatar {
  width: 30px;
  height: 30px;
  display: grid;
  place-items: center;
  border: 1px solid #3a493b;
  border-radius: 50%;
  color: var(--green);
  font-size: 12px;
}

.page-content {
  max-width: 1600px;
  padding: 34px 35px 25px;
  margin: 0 auto;
}

.page-heading {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 20px;
  margin-bottom: 28px;
}

.page-heading h1 {
  margin: 11px 0 8px;
  font-size: clamp(27px, 3vw, 36px);
  letter-spacing: -1.5px;
  font-weight: 600;
}

.page-heading h1 > span {
  color: var(--green);
}

.page-heading p,
.section-heading p {
  color: var(--muted);
  font-size: 12px;
  margin: 0;
  line-height: 1.7;
}

.primary-button {
  border: 1px solid #b7edba;
  background: var(--green);
  color: #102012;
  border-radius: 6px;
  padding: 11px 15px;
  font-size: 12px;
  font-weight: 700;
  white-space: nowrap;
}

.primary-button:hover {
  background: #c2f0c5;
}

.error-banner {
  padding: 15px;
  margin-bottom: 20px;
  border: 1px solid #74423d;
  border-radius: 7px;
  background: #241412;
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  font-size: 12px;
}

.error-banner span {
  color: #c9a9a5;
}

.error-banner button {
  margin-left: auto;
  color: var(--green);
  background: transparent;
  border: 1px solid var(--border);
  border-radius: 5px;
  padding: 6px 12px;
}

.metrics-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 13px;
  margin-bottom: 34px;
}

.metric-card {
  padding: 17px;
  min-width: 0;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 8px;
}

.metric-top {
  display: flex;
  justify-content: space-between;
  gap: 6px;
  align-items: center;
  color: var(--muted);
  font-size: 9px;
  letter-spacing: 0.7px;
}

.metric-icon {
  font-size: 19px;
  color: var(--green);
}

.metric-value {
  display: block;
  margin: 19px 0 7px;
  font-size: 32px;
  font-weight: 500;
  letter-spacing: -1px;
  font-variant-numeric: tabular-nums;
}

.metric-note {
  display: block;
  color: #89958b;
  font-size: 10px;
}

.section-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 15px;
  margin-bottom: 15px;
}

.section-heading h2,
.prediction-copy h2 {
  margin: 0 0 6px;
  font-size: 16px;
  font-weight: 600;
}

.subtle-tag {
  padding: 6px 8px;
  border: 1px solid var(--border);
  color: var(--muted);
  border-radius: 4px;
  font-size: 8px;
  letter-spacing: 1px;
  white-space: nowrap;
}

.activity-panel {
  padding: 22px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--panel);
  margin-bottom: 34px;
}

.activity-bars {
  display: flex;
  flex-direction: column;
  gap: 18px;
}

.activity-row {
  display: grid;
  grid-template-columns: minmax(110px, 180px) minmax(40px, 1fr) 22px;
  align-items: center;
  gap: 15px;
}

.activity-name {
  text-transform: capitalize;
  color: #c5cec6;
  font-size: 11px;
}

.bar-track {
  height: 7px;
  border-radius: 6px;
  background: #242c25;
  overflow: hidden;
}

.bar-fill {
  height: 100%;
  border-radius: inherit;
  background: var(--green);
  min-width: 5px;
}

.activity-count {
  color: var(--green);
  font-family: "DM Mono", monospace;
  font-size: 11px;
  text-align: right;
}

.panel-footnote {
  border-top: 1px solid var(--border);
  margin-top: 23px;
  padding-top: 14px;
  color: #788379;
  font-size: 10px;
}

.explorer-layout {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 270px;
  gap: 18px;
  align-items: start;
  scroll-margin-top: 20px;
  margin-bottom: 30px;
}

.events-section,
.detail-panel {
  min-width: 0;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--panel);
}

.events-section {
  overflow: hidden;
}

.events-section > .section-heading {
  padding: 20px 18px 4px;
}

.filters {
  display: flex;
  gap: 9px;
  padding: 15px 18px;
}

.search-box {
  flex: 1;
  min-width: 0;
  display: flex;
  gap: 8px;
  align-items: center;
  padding: 0 10px;
  border: 1px solid var(--border);
  background: #0d110e;
  border-radius: 5px;
  color: var(--muted);
}

.search-box > span {
  font-size: 19px;
}

.search-box input {
  width: 100%;
  min-width: 0;
  border: 0;
  outline: 0;
  background: transparent;
  color: #e4e9e5;
  padding: 10px 0;
  font-size: 11px;
}

.filters select {
  width: 142px;
  min-width: 0;
  background: #0d110e;
  border: 1px solid var(--border);
  color: #cbd4cc;
  padding: 9px;
  border-radius: 5px;
  font-size: 10px;
}

.table-wrap {
  overflow-x: auto;
}

table {
  width: 100%;
  border-collapse: collapse;
  text-align: left;
  white-space: nowrap;
}

thead {
  background: #151a16;
}

th {
  color: #859187;
  font-size: 8px;
  letter-spacing: 0.8px;
  font-weight: 500;
  padding: 13px 12px;
}

td {
  padding: 15px 12px;
  font-size: 10px;
  border-top: 1px solid #222a23;
  color: #bdc7be;
}

tbody tr {
  cursor: pointer;
  transition: background 0.15s;
}

tbody tr:hover,
tbody tr.selected-row {
  background: #19221a;
}

td:first-child,
th:first-child {
  padding-left: 18px;
}

td strong {
  display: block;
  font-family: "DM Mono", monospace;
  font-size: 10px;
  font-weight: 500;
}

.cell-subtitle {
  display: block;
  color: var(--muted);
  margin-top: 5px;
  font-size: 9px;
}

.event-badge {
  display: inline-block;
  padding: 5px 7px;
  border: 1px solid #39463a;
  border-radius: 4px;
  color: #b4d9b7;
  background: #1b281c;
  font-size: 9px;
  text-transform: capitalize;
}

.event-badge.liquidity_grab {
  color: #e6ca8e;
  background: #292417;
  border-color: #51462c;
}

.event-badge.breakout,
.event-badge.failed_breakout {
  color: #a7c9e8;
  background: #17232d;
  border-color: #304454;
}

.price-cell {
  font-family: "DM Mono", monospace;
  font-size: 10px;
}

.detail-panel {
  padding: 19px;
}

.detail-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.detail-symbol {
  color: var(--green);
  font-size: 20px;
}

.detail-panel h3 {
  font-size: 21px;
  margin: 24px 0 4px;
  letter-spacing: -0.5px;
}

.detail-id {
  color: var(--muted);
  font-size: 10px;
  font-family: "DM Mono", monospace;
  margin: 0 0 15px;
}

.detail-price {
  display: flex;
  flex-direction: column;
  gap: 7px;
  margin: 25px 0;
}

.detail-price > span,
.detail-field > span {
  color: var(--muted);
  font-size: 9px;
}

.detail-price > strong {
  font-size: 27px;
  font-family: "DM Mono", monospace;
  font-weight: 400;
  letter-spacing: -1px;
}

.detail-fields {
  border-top: 1px solid var(--border);
}

.detail-field {
  padding: 13px 0;
  border-bottom: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.detail-field > strong {
  color: #d6ded7;
  font-size: 11px;
  font-weight: 500;
  text-transform: capitalize;
  overflow-wrap: anywhere;
}

.evidence-note {
  margin-top: 20px;
  padding: 12px;
  border-radius: 6px;
  background: #19221a;
  border: 1px solid #2b3b2d;
}

.evidence-note > strong {
  color: var(--green);
  font-size: 10px;
}

.evidence-note p {
  color: #a8b5aa;
  font-size: 10px;
  line-height: 1.7;
  margin: 7px 0 0;
}

.prediction-panel {
  display: flex;
  align-items: center;
  gap: 18px;
  padding: 21px;
  border: 1px solid #354337;
  background: linear-gradient(110deg, #172019, #111612);
  border-radius: 8px;
  scroll-margin-top: 20px;
}

.prediction-icon {
  flex: 0 0 auto;
  display: grid;
  place-items: center;
  width: 46px;
  height: 46px;
  border-radius: 9px;
  background: #263b29;
  color: var(--green);
  font-size: 27px;
}

.prediction-copy {
  flex: 1;
}

.prediction-copy h2 {
  margin-top: 8px;
}

.prediction-copy p {
  color: #9ba99d;
  font-size: 11px;
  line-height: 1.8;
  margin: 0;
  max-width: 700px;
}

.model-status {
  flex: 0 0 auto;
  padding: 7px 9px;
  border: 1px solid #5b4c30;
  background: #2b2418;
  border-radius: 4px;
  color: #e0c78e;
  font-size: 8px;
  letter-spacing: 0.7px;
}

.empty-state {
  color: var(--muted);
  font-size: 12px;
  padding: 20px;
  text-align: center;
}

.page-footer {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  padding-top: 24px;
  color: #6e796f;
  font-size: 9px;
  letter-spacing: 0.6px;
}

@media (max-width: 1250px) {
  .sidebar {
    width: 205px;
  }

  .main-content {
    margin-left: 205px;
  }

  .page-content {
    padding: 27px 22px;
  }

  .topbar {
    padding: 0 22px;
  }

  .metrics-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .explorer-layout {
    grid-template-columns: minmax(0, 1fr);
  }

  .detail-panel {
    display: block;
  }
}

@media (max-width: 760px) {
  .sidebar {
    position: static;
    width: 100%;
    padding: 13px 15px;
    border-right: 0;
    border-bottom: 1px solid var(--border);
  }

  .brand {
    padding: 0 2px 14px;
  }

  .nav-label,
  .sidebar-bottom {
    display: none;
  }

  .sidebar > .nav-item {
    width: auto;
    display: inline-flex;
    padding: 9px 10px;
    margin: 0 4px 0 0;
    font-size: 11px;
  }

  .main-content {
    margin-left: 0;
  }

  .topbar {
    height: 52px;
    padding: 0 16px;
  }

  .page-content {
    padding: 25px 14px;
  }

  .page-heading {
    align-items: flex-start;
    flex-direction: column;
  }

  .metrics-grid {
    gap: 9px;
  }

  .metric-card {
    padding: 13px;
  }

  .metric-value {
    font-size: 27px;
  }

  .metric-top {
    font-size: 8px;
  }

  .activity-panel {
    padding: 15px;
  }

  .activity-row {
    grid-template-columns: 105px minmax(20px, 1fr) 15px;
    gap: 8px;
  }

  .activity-name {
    font-size: 9px;
  }

  .filters {
    flex-direction: column;
  }

  .filters select {
    width: 100%;
  }

  .prediction-panel {
    align-items: flex-start;
    flex-wrap: wrap;
    padding: 15px;
  }

  .prediction-copy {
    min-width: 0;
  }

  .model-status {
    margin-left: 64px;
  }

  .page-footer {
    flex-direction: column;
  }
}


/* TASK 6 additions: preserve the existing Hunter Signal Lab theme. */
#root {
  width: 100%;
  max-width: none;
  margin: 0;
  text-align: left;
  border-inline: 0;
}

.heading-actions {
  display: flex;
  align-items: center;
  gap: 10px;
}

.secondary-button {
  border: 1px solid var(--border);
  background: var(--panel-raised);
  color: #dce3dd;
  border-radius: 6px;
  padding: 11px 15px;
  font-size: 12px;
  font-weight: 600;
  white-space: nowrap;
}

.secondary-button:hover {
  border-color: #506a52;
  color: var(--green);
}

.market-section,
.chart-panel {
  padding: 22px;
  margin-bottom: 30px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--panel);
}

.market-controls {
  display: flex;
  align-items: end;
  gap: 14px;
  flex-wrap: wrap;
}

.market-controls label {
  display: flex;
  flex-direction: column;
  gap: 7px;
  color: var(--muted);
  font-size: 10px;
  letter-spacing: 0.7px;
  text-transform: uppercase;
  min-width: 180px;
}

.market-controls select {
  min-width: 200px;
  padding: 11px 12px;
  color: #dce3dd;
  background: #0d110e;
  border: 1px solid var(--border);
  border-radius: 5px;
  font-size: 12px;
  text-transform: none;
}

.selection-status {
  padding: 10px 0;
  color: var(--green);
  font-size: 11px;
}

.chart-panel {
  position: relative;
  overflow: hidden;
}

.chart-heading {
  align-items: flex-start;
}

.chart-heading p {
  color: var(--muted);
  font-size: 11px;
  margin-top: 5px;
}

.swept-toggle {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  color: #c3cdc5;
  font-size: 11px;
  white-space: nowrap;
}

.swept-toggle input {
  accent-color: var(--green);
}

.liquidity-chart {
  width: 100%;
  min-height: 360px;
}

.chart-overlay {
  padding: 14px;
  color: var(--muted);
  font-size: 12px;
}

.chart-empty {
  padding: 60px 15px;
}

.chart-legend {
  display: flex;
  flex-wrap: wrap;
  gap: 12px 20px;
  margin-top: 12px;
  padding-top: 14px;
  border-top: 1px solid var(--border);
  color: var(--muted);
  font-size: 10px;
  line-height: 1.7;
}

.chart-legend span {
  display: inline-flex;
  align-items: center;
  gap: 7px;
}

.legend-line {
  display: inline-block;
  width: 22px;
  height: 0;
  border-top: 2px solid var(--green);
}

.cluster-line {
  border-top-color: #d8a0ff;
  border-top-style: dashed;
}

.swept-line {
  border-top-color: #66716a;
}

.error-banner span {
  overflow-wrap: anywhere;
}

@media (max-width: 760px) {
  .heading-actions {
    width: 100%;
    flex-wrap: wrap;
  }

  .heading-actions button {
    flex: 1;
  }

  .market-section,
  .chart-panel {
    padding: 15px;
  }

  .market-controls,
  .market-controls label,
  .market-controls select {
    width: 100%;
    min-width: 0;
  }

  .chart-heading {
    flex-direction: column;
  }

  .swept-toggle {
    white-space: normal;
  }
}

/* // END OF FILE frontend/src/App.css */

/* Top 5 and watchlist */
.priority-section { margin-bottom: 34px; }
.priority-panel { padding: 8px; border: 1px solid var(--border); border-radius: 8px; background: var(--panel); }
.priority-list { list-style: none; margin: 0; padding: 0; display: grid; gap: 4px; }
.priority-item { width: 100%; display: grid; grid-template-columns: 28px 1fr auto; align-items: center; gap: 12px; padding: 12px 14px; border: 1px solid transparent; border-radius: 6px; background: transparent; color: inherit; font: inherit; text-align: left; cursor: pointer; }
.priority-item:hover { background: var(--panel-raised); border-color: var(--border); }
.priority-rank { display: inline-grid; place-items: center; width: 24px; height: 24px; border: 1px solid var(--border); border-radius: 50%; color: var(--muted); font-size: 11px; }
.priority-main, .priority-meta { display: flex; flex-direction: column; gap: 3px; }
.priority-meta { align-items: flex-end; }
.priority-main small, .priority-meta small { color: var(--muted); font-size: 10px; }
.watch-only-toggle { display: inline-flex; align-items: center; gap: 8px; color: var(--muted); font-size: 12px; white-space: nowrap; cursor: pointer; }
.watchlist-row { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; margin-top: 14px; }
.star-button, .watch-chip button { border: 1px solid var(--border); background: var(--panel-raised); color: #dce3dd; border-radius: 6px; padding: 8px 12px; font-size: 12px; cursor: pointer; }
.star-button.active { color: #e6ca8e; border-color: #51462c; }
.star-button:disabled { opacity: 0.5; cursor: not-allowed; }
.watch-chip { display: inline-flex; gap: 2px; }
.watch-chip button:first-child { border-radius: 6px 0 0 6px; }
.watch-chip button:last-child { border-radius: 0 6px 6px 0; padding: 8px 10px; color: var(--muted); }

/* Task 8: Binance pair search */
.pair-search-panel { display: grid; gap: 7px; max-width: 520px; margin-top: 16px; }
.pair-search-panel > label { color: #dce3dd; font-size: 12px; font-weight: 600; }
.pair-search-panel > input { width: 100%; border: 1px solid var(--border); border-radius: 6px; padding: 10px 12px; background: #0b0e0c; color: inherit; }
.pair-search-hint, .pair-search-error { margin: 0; color: var(--muted); font-size: 11px; }
.pair-search-error { color: #f0a4a4; }
.pair-search-results { display: grid; gap: 5px; margin: 2px 0 0; padding: 0; list-style: none; }
.pair-search-results li { display: flex; justify-content: space-between; align-items: center; gap: 12px; border: 1px solid var(--border); border-radius: 6px; padding: 9px 10px; background: var(--panel); }
.pair-search-results li > span { display: grid; gap: 3px; }
.pair-search-results small { color: var(--muted); font-size: 10px; }
.pair-search-results button { border: 1px solid #304a32; border-radius: 5px; padding: 7px 9px; background: #19251a; color: var(--green); font-size: 11px; }
.pair-search-results button:disabled { opacity: .6; }

/* END OF FILE frontend/src/App.css */
```

### `frontend/src/services/api.ts`

```typescript
import type {
  AnalysisResponse,
  ApiErrorBody,
  CandlesResponse,
  LiquidityEvent,
  Market,
  SummaryResponse,
} from "../types/market";

const API_BASE_URL = (
  import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000"
).replace(/\/$/, "");

export class ApiRequestError extends Error {
  readonly code: string | undefined;
  readonly status: number | undefined;

  constructor(message: string, status?: number, code?: string) {
    super(message);
    this.name = "ApiRequestError";
    this.status = status;
    this.code = code;
  }
}

async function requestJson<T>(
  path: string,
  signal?: AbortSignal,
): Promise<T> {
  let response: Response;

  try {
    response = await fetch(`${API_BASE_URL}${path}`, { signal });
  } catch (error: unknown) {
    if (error instanceof Error && error.name === "AbortError") {
      throw error;
    }
    throw new ApiRequestError(
      error instanceof Error
        ? error.message
        : "Could not connect to the Hunter API.",
    );
  }

  if (!response.ok) {
    let detail = `Request failed with HTTP ${response.status}.`;
    let code: string | undefined;

    try {
      const body: ApiErrorBody = await response.json() as ApiErrorBody;
      if (typeof body.detail === "string" && body.detail.length > 0) {
        detail = body.detail;
      }
      if (typeof body.code === "string") {
        code = body.code;
      }
    } catch {
      // Keep the HTTP fallback if the response body is not JSON.
    }

    throw new ApiRequestError(detail, response.status, code);
  }

  return response.json() as Promise<T>;
}

function queryString(values: Record<string, string | number | undefined>): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) {
    if (value !== undefined && value !== "") {
      params.set(key, String(value));
    }
  }
  const query = params.toString();
  return query ? `?${query}` : "";
}

/** The single timestamp conversion helper: ISO timestamp -> UTC epoch seconds. */
export function isoTimestampToUtcSeconds(value: string): number {
  const milliseconds = Date.parse(value);
  if (!Number.isFinite(milliseconds)) {
    throw new Error(`Invalid ISO timestamp: ${value}`);
  }
  return Math.floor(milliseconds / 1000);
}

export function getMarkets(signal?: AbortSignal): Promise<Market[]> {
  return requestJson<Market[]>("/api/markets", signal);
}

export function getAnalysis(
  symbol: string,
  timeframe: string,
  limit = 300,
  signal?: AbortSignal,
): Promise<AnalysisResponse> {
  return requestJson<AnalysisResponse>(
    `/api/analysis${queryString({ symbol, timeframe, limit })}`,
    signal,
  );
}

export function getCandles(
  symbol: string,
  timeframe: string,
  limit = 300,
  signal?: AbortSignal,
): Promise<CandlesResponse> {
  return requestJson<CandlesResponse>(
    `/api/candles${queryString({ symbol, timeframe, limit })}`,
    signal,
  );
}

export type MarketSearchResult = { symbol: string; base: string };

export function searchMarkets(
  query: string,
  limit = 10,
  signal?: AbortSignal,
): Promise<MarketSearchResult[]> {
  return requestJson<MarketSearchResult[]>(
    `/api/markets/search${queryString({ q: query, limit })}`,
    signal,
  );
}

export function getEvents(
  limit = 500,
  signal?: AbortSignal,
  symbols?: string[],
): Promise<LiquidityEvent[]> {
  return requestJson<LiquidityEvent[]>(
    `/api/events${queryString({ limit, symbols: symbols?.join(",") })}`,
    signal,
  );
}

export function getSummary(signal?: AbortSignal): Promise<SummaryResponse> {
  return requestJson<SummaryResponse>("/api/summary", signal);
}

// END OF FILE frontend/src/services/api.ts
```

## C. Verification commands and actual results

Verified in this sandbox:

- `python -m py_compile main.py app/market_data.py app/routes.py tests/test_api.py tests/test_market_data.py` — **PASS** (no output/errors; command returned successfully).
- `python -m pytest -q` — **NOT RUN TO COMPLETION**. Collection stopped because this sandbox does not have `ccxt` installed: `ModuleNotFoundError: No module named 'ccxt'`. The test suite did not run; no test passes are claimed.
- Frontend `npm run build` — **NOT RUN** because `frontend/node_modules` is not present in the supplied ZIP. No frontend build pass is claimed.

Run in Windows PowerShell from the project folders after dependencies are installed:

Backend:

```powershell
cd "$HOME\Desktop\hunter-signal-lab\backend"
.\.venv\Scripts\Activate.ps1
python -m pytest -q
```

Frontend:

```powershell
cd "$HOME\Desktop\hunter-signal-lab\frontend"
npm run build
```

Expected backend total: 139 tests, assuming the previously reported 128-test baseline is still the current baseline and no tests were added or removed since that count.

## D. Deviations and reasons

- No dependency or `requirements.txt` changes were made.
- The backend search is intentionally restricted to the configured Binance exchange and requires market metadata with `active is True`, `spot is True`, and `quote == "USDT"`.
- Top 5 watchlist retrieval batches watchlist symbols in groups of ten because the API accepts at most ten symbols per request; results are deduplicated by event ID.
- Automated frontend interaction tests were not added because the project has no frontend test runner configured. Build verification remains pending.

## E. Assumptions and open questions

- The existing 128-test baseline is the authoritative baseline for calculating the expected total.
- Binance is configured through `HUNTER_EXCHANGE=binance`; custom-pair search deliberately returns an error if another exchange is configured.
- The existing configured `HUNTER_SYMBOLS` remain accepted even if a symbol is absent from the cached market list, as requested.
- Browser watchlist persistence is local to that browser and can be cleared by the user; no server-side persistence was added.
