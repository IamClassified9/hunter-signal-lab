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
MAX_CACHE_ENTRIES = 256  # users can request any listed pair, so bound memory


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
        """Load and cache the exchange's active USDT spot pairs for one hour."""
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
            load_markets = getattr(self.exchange, "load_markets", None)
            if not callable(load_markets):
                raise MarketDataError("Exchange does not support loading market metadata")
            try:
                loaded = await load_markets()
            except ccxt.RateLimitExceeded as exc:
                raise RateLimited(f"Rate limited by {self.exchange_id} while loading markets: {exc}") from exc
            except (ccxt.NetworkError, ccxt.ExchangeNotAvailable, ccxt.RequestTimeout) as exc:
                raise ExchangeUnavailable(f"{self.exchange_id} market list is unavailable: {exc}") from exc
            except ccxt.BaseError as exc:
                raise MarketDataError(f"Could not load {self.exchange_id} market list: {exc}") from exc
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
        ranked = sorted(
            (
                (0 if item["base"] == needle else 1 if item["base"].startswith(needle) else 2, item["symbol"], item)
                for item in self._active_spot_usdt_markets
                if needle in item["base"]
            ),
            key=lambda entry: (entry[0], entry[1]),
        )
        return [dict(item) for _, _, item in ranked[:limit]]

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
                f"Unsupported symbol '{symbol}' on {self.exchange_id}: not in HUNTER_SYMBOLS "
                f"and not an active USDT spot market"
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
            exchange_markets = getattr(self.exchange, "markets", None)
            if isinstance(exchange_markets, dict) and exchange_markets and symbol not in exchange_markets:
                raise UnsupportedSymbol(f"Unsupported symbol '{symbol}' on {self.exchange_id}")
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
            while len(self._cache) > MAX_CACHE_ENTRIES:
                oldest = min(self._cache, key=lambda item: self._cache[item][0])
                self._cache.pop(oldest, None)
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