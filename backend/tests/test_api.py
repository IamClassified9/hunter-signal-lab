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


def test_brief_uses_requested_watchlist_and_passes_top_three_to_brief(api_client, monkeypatch):
    client, fake = api_client
    analysed = []
    captured = {}

    def fake_analyse(candles, symbol, timeframe, config):
        analysed.append((symbol, timeframe))
        event = LiquidityEvent(
            event_id=f"{symbol}-{timeframe}",
            symbol=symbol,
            timeframe=timeframe,
            event_type="BREAKOUT",
            price=100.0,
            volume_ratio=2.0 if timeframe == "15m" else 1.0,
            structure="SUPPORT_BREAK",
            timestamp=NOW,
            level_id=None,
            level_price=99.0,
            detected_at=NOW + timedelta(minutes=15),
        )
        return [], [event]

    async def fake_generate(events):
        captured["events"] = events
        return {"source": "template", "events": events}

    monkeypatch.setattr(routes, "analyse", fake_analyse)
    monkeypatch.setattr(routes, "generate_brief", fake_generate)
    response = client.get("/brief", params={"symbols": "BTC/USDT"})

    assert response.status_code == 200
    assert {symbol for symbol, _ in analysed} == {"BTC/USDT"}
    assert len(analysed) == 2  # configured 15m and 1h timeframes
    assert len(captured["events"]) == 2
    assert all(item["symbol"] == "BTC/USDT" for item in captured["events"])


def test_brief_rejects_more_than_twenty_watchlist_symbols(api_client):
    client, _ = api_client
    symbols = ",".join(f"COIN{i}/USDT" for i in range(21))
    response = client.get("/brief", params={"symbols": symbols})
    assert response.status_code == 400
    assert response.json()["code"] == "TOO_MANY_SYMBOLS"
