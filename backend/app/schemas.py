from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

TimeFrame = Literal["5m", "15m", "1h", "4h"]

EventType = Literal[
    "SWING_HIGH_SWEEP",
    "SWING_LOW_SWEEP",
    "LIQUIDITY_GRAB",
    "BREAKOUT",
    "FAILED_BREAKOUT",
]

LevelKind = Literal["SWING_HIGH", "SWING_LOW", "EQUAL_HIGHS", "EQUAL_LOWS"]

TIMEFRAME_SECONDS: dict[str, int] = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400}


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware (UTC)")
    return value.astimezone(timezone.utc)


def make_level_id(
    symbol: str, timeframe: str, kind: str, formed_at: datetime, price: float
) -> str:
    raw = f"{symbol}|{timeframe}|{kind}|{_require_utc(formed_at).isoformat()}|{price:.8f}"
    return "LVL-" + hashlib.sha256(raw.encode()).hexdigest()[:12].upper()


def make_event_id(
    symbol: str,
    timeframe: str,
    event_type: str,
    candle_time: datetime,
    level_id: str,
) -> str:
    """Deterministic ID so /api/events/{event_id} works without a database.
    Includes the level, so two levels hit by one candle get distinct IDs."""
    raw = (
        f"{symbol}|{timeframe}|{event_type}|"
        f"{_require_utc(candle_time).isoformat()}|{level_id}"
    )
    return "EVT-" + hashlib.sha256(raw.encode()).hexdigest()[:12].upper()


class Candle(BaseModel):
    timestamp: datetime  # candle OPEN time, UTC
    open: float = Field(gt=0, allow_inf_nan=False)
    high: float = Field(gt=0, allow_inf_nan=False)
    low: float = Field(gt=0, allow_inf_nan=False)
    close: float = Field(gt=0, allow_inf_nan=False)
    volume: float = Field(ge=0, allow_inf_nan=False)

    _utc = field_validator("timestamp")(_require_utc)

    @model_validator(mode="after")
    def _check_ohlc(self) -> "Candle":
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("high must be >= open, close and low")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("low must be <= open, close and high")
        return self


class LiquidityLevel(BaseModel):
    level_id: str
    symbol: str
    timeframe: TimeFrame
    kind: LevelKind
    price: float = Field(gt=0)
    formed_at: datetime  # open time of the swing candle itself
    confirmed_at: datetime  # when the swing became knowable (formed + N bars)
    touches: int = Field(default=1, ge=1)  # swings merged into this level
    swept: bool = False

    _utc = field_validator("formed_at", "confirmed_at")(_require_utc)

    @model_validator(mode="after")
    def _confirmed_after_formed(self) -> "LiquidityLevel":
        if self.confirmed_at < self.formed_at:
            raise ValueError("confirmed_at cannot be before formed_at")
        is_cluster = self.kind in ("EQUAL_HIGHS", "EQUAL_LOWS")
        if is_cluster and self.touches < 2:
            raise ValueError("cluster levels need touches >= 2")
        if not is_cluster and self.touches != 1:
            raise ValueError("single swing levels must have touches == 1")
        return self


class LiquidityEvent(BaseModel):
    # --- original fields: DO NOT rename or remove (dashboard depends on them)
    event_id: str
    symbol: str
    timeframe: TimeFrame
    event_type: EventType
    price: float = Field(gt=0)
    volume_ratio: float = Field(ge=0)
    structure: str
    timestamp: datetime
    # --- additive, optional: old consumers simply ignore these
    level_id: Optional[str] = None
    level_price: Optional[float] = Field(default=None, gt=0)
    detected_at: Optional[datetime] = None  # always set by the real engine

    _utc = field_validator("timestamp")(_require_utc)

    @field_validator("detected_at")
    @classmethod
    def _detected_utc(cls, v: Optional[datetime]) -> Optional[datetime]:
        return None if v is None else _require_utc(v)

    @model_validator(mode="after")
    def _detected_not_before_open(self) -> "LiquidityEvent":
        if self.detected_at is not None and self.detected_at <= self.timestamp:
            raise ValueError("detected_at must be after the candle open time")
        return self


class EngineConfig(BaseModel):
    swing_bars: int = Field(default=3, ge=1, le=20)
    equal_tolerance_pct: float = Field(default=0.10, gt=0, le=2)
    confirm_window_k: int = Field(default=3, ge=1, le=20)
    breakout_buffer_pct: float = Field(default=0.10, ge=0, le=2)
    volume_lookback: int = Field(default=20, ge=2, le=200)


class MarketInfo(BaseModel):
    symbol: str
    timeframes: list[TimeFrame]
    exchange: str


class CandlesResponse(BaseModel):
    symbol: str
    timeframe: TimeFrame
    exchange: str
    data_source: Literal["live_exchange", "cache"]
    generated_at: datetime
    candles: list[Candle]


class AnalysisResponse(BaseModel):
    symbol: str
    timeframe: TimeFrame
    exchange: str
    generated_at: datetime
    candles_analysed: int = Field(ge=0)
    config: EngineConfig  # echoed so results are reproducible
    levels: list[LiquidityLevel]
    events: list[LiquidityEvent]


class ErrorResponse(BaseModel):
    detail: str
    code: Literal[
        "UNSUPPORTED_SYMBOL",
        "UNSUPPORTED_TIMEFRAME",
        "EXCHANGE_UNAVAILABLE",
        "RATE_LIMITED",
        "NOT_FOUND",
        "INVALID_EVENT_TYPE",
        "INVALID_QUERY",
        "TOO_MANY_SYMBOLS",
    ]

class SummaryResponse(BaseModel):
    total_events: int = Field(ge=0)
    symbols: list[str]
    event_counts: dict[str, int]
    data_source: Literal["live_exchange"]
    generated_at: datetime

    _utc = field_validator("generated_at")(_require_utc)