"""Causal liquidity-level and event analysis for closed OHLCV candles."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, Sequence

from app.schemas import (
    Candle,
    EngineConfig,
    LiquidityEvent,
    LiquidityLevel,
    TIMEFRAME_SECONDS,
    make_event_id,
    make_level_id,
)

SwingKind = Literal["SWING_HIGH", "SWING_LOW"]
Side = Literal["high", "low"]


@dataclass(frozen=True)
class Swing:
    index: int
    kind: SwingKind
    price: float
    formed_at: datetime
    confirmed_at: datetime


def breakout_threshold(level_price: float, side: Side, config: EngineConfig) -> float:
    """Return the configured breakout boundary; percentages are percentage points."""
    buffer = config.breakout_buffer_pct / 100.0
    if side == "high":
        return level_price * (1.0 + buffer)
    if side == "low":
        return level_price * (1.0 - buffer)
    raise ValueError(f"Unsupported side: {side!r}")


def _close_time(candle: Candle, timeframe: str) -> datetime:
    return candle.timestamp + timedelta(seconds=TIMEFRAME_SECONDS[timeframe])


def _validate_candles(candles: Sequence[Candle], timeframe: str) -> None:
    if timeframe not in TIMEFRAME_SECONDS:
        raise ValueError(f"Unsupported timeframe: {timeframe!r}")
    previous: datetime | None = None
    expected_delta = timedelta(seconds=TIMEFRAME_SECONDS[timeframe])
    for candle in candles:
        if previous is not None:
            expected_timestamp = previous + expected_delta
            if candle.timestamp != expected_timestamp:
                raise ValueError(
                    f"candles must be exactly {timeframe} apart; "
                    f"first offending timestamp: {candle.timestamp.isoformat()}"
                )
        previous = candle.timestamp


def _swing_kinds_at(
    candles: Sequence[Candle], pivot_index: int, n: int, visible_len: int
) -> list[SwingKind]:
    """Return strict swing kinds without reading past the visible prefix."""
    if visible_len < 0 or visible_len > len(candles):
        raise ValueError("visible_len must be between 0 and len(candles)")
    if pivot_index < n or pivot_index + n >= visible_len:
        return []

    pivot = candles[pivot_index]
    neighbors = (
        *candles[pivot_index - n : pivot_index],
        *candles[pivot_index + 1 : min(pivot_index + n + 1, visible_len)],
    )
    kinds: list[SwingKind] = []
    if all(pivot.high > candle.high for candle in neighbors):
        kinds.append("SWING_HIGH")
    if all(pivot.low < candle.low for candle in neighbors):
        kinds.append("SWING_LOW")
    return kinds


def find_swings(
    candles: Sequence[Candle], timeframe: str, config: EngineConfig
) -> list[Swing]:
    """Find strict pivots; each pivot is knowable only at its right-hand close."""
    _validate_candles(candles, timeframe)
    n = config.swing_bars
    swings: list[Swing] = []
    for i in range(n, len(candles) - n):
        for kind in _swing_kinds_at(candles, i, n, len(candles)):
            price = candles[i].high if kind == "SWING_HIGH" else candles[i].low
            swings.append(
                Swing(
                    i,
                    kind,
                    price,
                    candles[i].timestamp,
                    _close_time(candles[i + n], timeframe),
                )
            )
    return swings


def analyse(
    candles: Sequence[Candle],
    symbol: str,
    timeframe: str,
    config: EngineConfig | None = None,
) -> tuple[list[LiquidityLevel], list[LiquidityEvent]]:
    """Build levels and resolve events in one causal chronological pass."""
    _validate_candles(candles, timeframe)
    config = config or EngineConfig()
    n = config.swing_bars
    active: list[dict] = []
    events: list[LiquidityEvent] = []
    pending_clusters: dict[str, dict] = {}
    failed_breakouts: dict[str, dict] = {}

    def refresh_id(state: dict) -> None:
        state["level_id"] = make_level_id(
            symbol, timeframe, state["kind"], state["formed_at"], state["price"]
        )

    def emit(state: dict, candle: Candle, event_type: str, structure: str) -> None:
        previous = candles[: state["current_index"]]
        lookback = previous[-config.volume_lookback :]
        mean_volume = (
            sum(item.volume for item in lookback) / len(lookback) if lookback else 0.0
        )
        volume_ratio = candle.volume / mean_volume if mean_volume > 0 else 0.0
        events.append(
            LiquidityEvent(
                event_id=make_event_id(
                    symbol, timeframe, event_type, candle.timestamp, state["level_id"]
                ),
                symbol=symbol,
                timeframe=timeframe,
                event_type=event_type,
                price=candle.close,
                volume_ratio=volume_ratio,
                structure=structure,
                timestamp=candle.timestamp,
                level_id=state["level_id"],
                level_price=state["price"],
                detected_at=_close_time(candle, timeframe),
            )
        )

    for t, candle in enumerate(candles):
        # First advance existing pending-cluster and failed-breakout windows.
        for level_id, pending in list(pending_clusters.items()):
            state = pending["state"]
            side: Side = pending["side"]
            threshold = breakout_threshold(state["price"], side, config)
            if (side == "high" and candle.close <= state["price"]) or (
                side == "low" and candle.close >= state["price"]
            ):
                state["current_index"] = t
                emit(state, candle, "LIQUIDITY_GRAB", "REJECTION")
                del pending_clusters[level_id]
            elif (side == "high" and candle.close > threshold) or (
                side == "low" and candle.close < threshold
            ):
                state["current_index"] = t
                structure = "RESISTANCE_BREAK" if side == "high" else "SUPPORT_BREAK"
                emit(state, candle, "BREAKOUT", structure)
                failed_breakouts[level_id] = {
                    "state": state,
                    "side": side,
                    "remaining": config.confirm_window_k,
                    "level_price": state["price"],
                }
                del pending_clusters[level_id]
            else:
                pending["remaining"] -= 1
                if pending["remaining"] <= 0:
                    del pending_clusters[level_id]

        for level_id, failed in list(failed_breakouts.items()):
            state = failed["state"]
            side: Side = failed["side"]
            returned = (
                candle.close <= failed["level_price"]
                if side == "high"
                else candle.close >= failed["level_price"]
            )
            if returned:
                state["current_index"] = t
                emit(state, candle, "FAILED_BREAKOUT", "FAILED_BREAKOUT")
                del failed_breakouts[level_id]
            else:
                failed["remaining"] -= 1
                if failed["remaining"] <= 0:
                    del failed_breakouts[level_id]

        # Then test the current candle against eligible, not-yet-pierced levels.
        hit_levels = sorted(
            (
                state for state in active
                if not state["swept"] and state["confirmed_at"] <= candle.timestamp
            ),
            key=lambda state: (state["price"], state["formed_at"], state["level_id"]),
        )
        for state in hit_levels:
            side: Side = state["side"]
            level_price = state["price"]
            pierced = candle.high > level_price if side == "high" else candle.low < level_price
            if not pierced:
                continue

            state["swept"] = True
            state["current_index"] = t
            threshold = breakout_threshold(level_price, side, config)
            if side == "high":
                returned = candle.close <= level_price
                beyond = candle.close > threshold
            else:
                returned = candle.close >= level_price
                beyond = candle.close < threshold

            is_cluster = state["kind"] in ("EQUAL_HIGHS", "EQUAL_LOWS")
            if beyond:
                structure = "RESISTANCE_BREAK" if side == "high" else "SUPPORT_BREAK"
                emit(state, candle, "BREAKOUT", structure)
                failed_breakouts[state["level_id"]] = {
                    "state": state,
                    "side": side,
                    "remaining": config.confirm_window_k,
                    "level_price": level_price,
                }
            elif returned:
                if is_cluster:
                    emit(state, candle, "LIQUIDITY_GRAB", "REJECTION")
                else:
                    event_type = "SWING_HIGH_SWEEP" if side == "high" else "SWING_LOW_SWEEP"
                    emit(state, candle, event_type, "REJECTION")
            elif is_cluster:
                pending_clusters[state["level_id"]] = {
                    "state": state,
                    "side": side,
                    "remaining": config.confirm_window_k,
                }

        # Only after this candle's events, confirm swings at its close. Such a
        # level cannot be used by this candle because confirmed_at is its close.
        # At t, only inspect the pivot t-N and candles at or before t. This
        # confirms swings causally without precomputing from future candles.
        pivot_index = t - n
        if pivot_index >= n:
            confirmed_at = _close_time(candle, timeframe)
            for kind in _swing_kinds_at(candles, pivot_index, n, t + 1):
                pivot = candles[pivot_index]
                price = pivot.high if kind == "SWING_HIGH" else pivot.low
                swing = Swing(pivot_index, kind, price, pivot.timestamp, confirmed_at)
                side: Side = "high" if swing.kind == "SWING_HIGH" else "low"
                tolerance = config.equal_tolerance_pct / 100.0
                candidates = [
                    state for state in active
                    if state["side"] == side
                    and not state["swept"]
                    and abs(state["price"] - swing.price) / state["price"] <= tolerance
                ]
                # Nearest level first; ties prefer the earliest formed level.
                candidates.sort(
                    key=lambda state: (abs(state["price"] - swing.price), state["formed_at"])
                )
                if candidates:
                    state = candidates[0]
                    state["touches"] += 1
                    state["kind"] = "EQUAL_HIGHS" if side == "high" else "EQUAL_LOWS"
                    state["price"] = (
                        max(state["price"], swing.price)
                        if side == "high"
                        else min(state["price"], swing.price)
                    )
                    state["formed_at"] = min(state["formed_at"], swing.formed_at)
                    state["confirmed_at"] = swing.confirmed_at
                    refresh_id(state)
                else:
                    state = {
                        "symbol": symbol,
                        "timeframe": timeframe,
                        "kind": swing.kind,
                        "side": side,
                        "price": swing.price,
                        "formed_at": swing.formed_at,
                        "confirmed_at": swing.confirmed_at,
                        "touches": 1,
                        "swept": False,
                        "current_index": t,
                    }
                    refresh_id(state)
                    active.append(state)

    # Every level remains in active, including resolved and pending states.
    levels = [
        LiquidityLevel(
            level_id=state["level_id"],
            symbol=state["symbol"],
            timeframe=state["timeframe"],
            kind=state["kind"],
            price=state["price"],
            formed_at=state["formed_at"],
            confirmed_at=state["confirmed_at"],
            touches=state["touches"],
            swept=state["swept"],
        )
        for state in active
    ]
    levels.sort(key=lambda level: (level.formed_at, level.price))
    events.sort(key=lambda event: (event.timestamp, event.event_type, event.level_id or ""))
    return levels, events