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
