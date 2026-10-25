from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.schemas import (
    Candle,
    LiquidityEvent,
    LiquidityLevel,
    make_event_id,
    make_level_id,
)

UTC = timezone.utc
BASE_TIME = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)


DEMO_EVENTS = [
    {
        "event_id": "EVT-001",
        "symbol": "BTC/USDT",
        "timeframe": "15m",
        "event_type": "SWING_HIGH_SWEEP",
        "price": 68234.5,
        "volume_ratio": 3.8,
        "structure": "BREAK_OF_STRUCTURE",
        "timestamp": datetime(2026, 10, 22, 9, 15, tzinfo=UTC),
        "level_id": "LVL-DEMO-001",
        "level_price": 68250.0,
        "detected_at": datetime(2026, 10, 22, 9, 30, tzinfo=UTC),
    },
    {
        "event_id": "EVT-002",
        "symbol": "ETH/USDT",
        "timeframe": "15m",
        "event_type": "LIQUIDITY_GRAB",
        "price": 2611.5,
        "volume_ratio": 2.1,
        "structure": "REJECTION",
        "timestamp": datetime(2026, 10, 22, 9, 30, tzinfo=UTC),
        "level_id": "LVL-DEMO-002",
        "level_price": 2615.0,
        "detected_at": datetime(2026, 10, 22, 9, 45, tzinfo=UTC),
    },
    {
        "event_id": "EVT-003",
        "symbol": "BTC/USDT",
        "timeframe": "1h",
        "event_type": "BREAKOUT",
        "price": 68100.0,
        "volume_ratio": 1.4,
        "structure": "RESISTANCE_BREAK",
        "timestamp": datetime(2026, 10, 22, 10, 0, tzinfo=UTC),
        "level_id": "LVL-DEMO-003",
        "level_price": 68050.0,
        "detected_at": datetime(2026, 10, 22, 11, 0, tzinfo=UTC),
    },
]


@pytest.mark.parametrize("demo_event", DEMO_EVENTS)
def test_original_demo_events_validate_against_shared_schema(demo_event):
    event = LiquidityEvent.model_validate(demo_event)
    assert event.event_id == demo_event["event_id"]
    assert event.symbol == demo_event["symbol"]
    assert event.timestamp == demo_event["timestamp"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"open": 10, "high": 9, "low": 8, "close": 9.5, "volume": 1},
        {"open": 10, "high": 12, "low": 11, "close": 11.5, "volume": 1},
        {"open": 10, "high": 12, "low": 8, "close": 11, "volume": -1},
        {
            "timestamp": datetime(2026, 10, 22, 9, 0),
            "open": 10,
            "high": 12,
            "low": 8,
            "close": 11,
            "volume": 1,
        },
    ],
)
def test_candle_rejects_invalid_data(overrides):
    values = {
        "timestamp": BASE_TIME,
        "open": 10,
        "high": 12,
        "low": 8,
        "close": 11,
        "volume": 1,
    }
    values.update(overrides)
    with pytest.raises(ValidationError):
        Candle.model_validate(values)


@pytest.mark.parametrize("field", ["open", "high", "low", "close", "volume"])
@pytest.mark.parametrize("bad_value", [float("nan"), float("inf")])
def test_candle_rejects_nan_and_infinity_in_numeric_fields(field, bad_value):
    values = {
        "timestamp": BASE_TIME,
        "open": 10,
        "high": 12,
        "low": 8,
        "close": 11,
        "volume": 1,
    }
    values[field] = bad_value
    with pytest.raises(ValidationError):
        Candle.model_validate(values)


def _level(*, kind="SWING_HIGH", touches=1, formed_at=BASE_TIME, confirmed_at=None):
    return LiquidityLevel(
        level_id="LVL-TEST",
        symbol="BTC/USDT",
        timeframe="15m",
        kind=kind,
        price=100.0,
        formed_at=formed_at,
        confirmed_at=confirmed_at or datetime(2026, 10, 22, 9, 15, tzinfo=UTC),
        touches=touches,
    )


def test_liquidity_level_rejects_confirmation_before_formation():
    with pytest.raises(ValidationError):
        _level(confirmed_at=datetime(2026, 10, 22, 8, 59, tzinfo=UTC))


def test_equal_high_cluster_requires_two_touches():
    with pytest.raises(ValidationError):
        _level(kind="EQUAL_HIGHS", touches=1)


def test_single_swing_requires_exactly_one_touch():
    with pytest.raises(ValidationError):
        _level(kind="SWING_HIGH", touches=2)


def _event(*, detected_at):
    return {
        "event_id": "EVT-TEST",
        "symbol": "BTC/USDT",
        "timeframe": "15m",
        "event_type": "SWING_HIGH_SWEEP",
        "price": 100.0,
        "volume_ratio": 1.2,
        "structure": "REJECTION",
        "timestamp": BASE_TIME,
        "level_id": "LVL-TEST",
        "level_price": 100.0,
        "detected_at": detected_at,
    }


@pytest.mark.parametrize(
    "detected_at",
    [BASE_TIME, datetime(2026, 10, 22, 8, 59, tzinfo=UTC)],
)
def test_event_rejects_detected_at_not_after_timestamp(detected_at):
    with pytest.raises(ValidationError):
        LiquidityEvent.model_validate(_event(detected_at=detected_at))


def test_make_event_id_is_deterministic():
    args = ("BTC/USDT", "15m", "SWING_HIGH_SWEEP", BASE_TIME, "LVL-1")
    assert make_event_id(*args) == make_event_id(*args)


def test_make_event_id_differs_when_level_id_differs():
    common = ("BTC/USDT", "15m", "SWING_HIGH_SWEEP", BASE_TIME)
    assert make_event_id(*common, "LVL-1") != make_event_id(*common, "LVL-2")


def test_make_level_id_is_deterministic():
    args = ("BTC/USDT", "15m", "SWING_HIGH", BASE_TIME, 100.0)
    assert make_level_id(*args) == make_level_id(*args)