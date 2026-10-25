from __future__ import annotations

import random
import time
from datetime import datetime, timedelta, timezone
from collections import Counter

import pytest

from app.liquidity_engine import analyse, breakout_threshold, find_swings
from app.schemas import Candle, EngineConfig

UTC = timezone.utc
START = datetime(2026, 1, 1, tzinfo=UTC)


def make_candles(rows, timeframe="15m"):
    seconds = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400}[timeframe]
    result = []
    for i, item in enumerate(rows):
        if isinstance(item, dict):
            values = item
            o = values.get("open", 5.0)
            h = values["high"]
            l = values["low"]
            c = values.get("close", (h + l) / 2)
            v = values.get("volume", 10.0)
        else:
            h, l, c = item
            o, v = min(max(c, l), h), 10.0
        result.append(
            Candle(
                timestamp=START + timedelta(seconds=seconds * i),
                open=o,
                high=h,
                low=l,
                close=c,
                volume=v,
            )
        )
    return result


def cfg(**kwargs):
    return EngineConfig(
        swing_bars=kwargs.pop("swing_bars", 1),
        breakout_buffer_pct=kwargs.pop("breakout_buffer_pct", 1.0),
        equal_tolerance_pct=kwargs.pop("equal_tolerance_pct", 0.1),
        confirm_window_k=kwargs.pop("confirm_window_k", 2),
        volume_lookback=kwargs.pop("volume_lookback", 3),
        **kwargs,
    )


def high_setup(hit_high, hit_close, *, cluster=False, tail=None):
    rows = [(8, 6, 7), (10, 7, 9), (9, 6, 8)]
    if cluster:
        rows += [(9, 6, 8), (9.995, 7, 8), (9, 6, 8)]
    rows.append((hit_high, 6, hit_close))
    rows.extend(tail or [])
    return make_candles(rows)


def low_setup(hit_low, hit_close, *, cluster=False, tail=None):
    rows = [(10, 8, 9), (9, 5, 6), (10, 7, 9)]
    if cluster:
        rows += [(10, 7, 9), (9, 5.004, 6), (10, 7, 9)]
    rows.append((11, hit_low, hit_close))
    rows.extend(tail or [])
    return make_candles(rows)


def event_types(events):
    return [event.event_type for event in events]


def test_find_swings_detects_simple_high_and_low():
    candles = make_candles(
        [(8, 7, 7.5), (9, 6, 7), (12, 4, 8), (10, 5, 7), (8, 6, 7)]
    )
    swings = find_swings(candles, "15m", cfg())
    assert [(s.index, s.kind) for s in swings] == [
        (2, "SWING_HIGH"),
        (2, "SWING_LOW"),
    ]
    assert all(
        s.confirmed_at == candles[3].timestamp + timedelta(minutes=15)
        for s in swings
    )


def test_find_swings_equal_neighbouring_highs_are_not_strict_swings():
    candles = make_candles(
        [(8, 6, 7), (10, 6, 8), (10, 5, 8), (9, 6, 7), (8, 6, 7)]
    )
    assert not any(
        s.kind == "SWING_HIGH"
        for s in find_swings(candles, "15m", cfg())
    )


def test_find_swings_series_edges_never_swing():
    candles = make_candles(
        [(20, 1, 10), (8, 6, 7), (9, 5, 7), (8, 6, 7), (20, 1, 10)]
    )
    swings = find_swings(candles, "15m", cfg())
    assert all(0 < s.index < len(candles) - 1 for s in swings)


@pytest.mark.parametrize(
    "side,hit_high,hit_low,close,event_type,structure",
    [
        ("high", 10.2, 6, 10.2, "BREAKOUT", "RESISTANCE_BREAK"),
        ("low", 11, 4.8, 4.8, "BREAKOUT", "SUPPORT_BREAK"),
    ],
)
def test_rule_1_breakout_both_sides(
    side, hit_high, hit_low, close, event_type, structure
):
    candles = (
        high_setup(hit_high, close)
        if side == "high"
        else low_setup(hit_low, close)
    )
    _, events = analyse(candles, "TEST/USDT", "15m", cfg())
    assert event_type in event_types(events)
    assert next(
        e for e in events if e.event_type == event_type
    ).structure == structure


@pytest.mark.parametrize(
    "side,hit_high,hit_low,close,event_type",
    [
        ("high", 10.2, 6, 9.5, "SWING_HIGH_SWEEP"),
        ("low", 11, 4.8, 5.5, "SWING_LOW_SWEEP"),
    ],
)
def test_rule_2_single_swing_return_inside_both_sides(
    side, hit_high, hit_low, close, event_type
):
    candles = (
        high_setup(hit_high, close)
        if side == "high"
        else low_setup(hit_low, close)
    )
    _, events = analyse(candles, "TEST/USDT", "15m", cfg())
    assert event_type in event_types(events)


@pytest.mark.parametrize(
    "side,hit_high,hit_low,close",
    [("high", 10.2, 6, 9.5), ("low", 11, 4.8, 5.5)],
)
def test_rule_3_cluster_return_inside_is_grab_both_sides(
    side, hit_high, hit_low, close
):
    candles = (
        high_setup(hit_high, close, cluster=True)
        if side == "high"
        else low_setup(hit_low, close, cluster=True)
    )
    _, events = analyse(candles, "TEST/USDT", "15m", cfg())
    assert "LIQUIDITY_GRAB" in event_types(events)
    assert not any(
        e.event_type in ("SWING_HIGH_SWEEP", "SWING_LOW_SWEEP")
        for e in events
    )


@pytest.mark.parametrize(
    "side,hit_high,hit_low,close",
    [("high", 10.2, 6, 10.05), ("low", 11, 4.8, 4.98)],
)
def test_rule_4_single_swing_dead_zone_has_no_event_both_sides(
    side, hit_high, hit_low, close
):
    candles = (
        high_setup(hit_high, close)
        if side == "high"
        else low_setup(hit_low, close)
    )
    _, events = analyse(candles, "TEST/USDT", "15m", cfg())
    assert not events


@pytest.mark.parametrize(
    "side,hit_high,hit_low,close,expected",
    [
        ("high", 10.2, 6, 10.05, "LIQUIDITY_GRAB"),
        ("low", 11, 4.8, 4.98, "LIQUIDITY_GRAB"),
    ],
)
def test_rule_5_cluster_dead_zone_then_return_is_grab_both_sides(
    side, hit_high, hit_low, close, expected
):
    candles = (
        high_setup(
            hit_high, close, cluster=True, tail=[(10, 6, 9.5)]
        )
        if side == "high"
        else low_setup(
            hit_low, close, cluster=True, tail=[(11, 5.5, 6)]
        )
    )
    _, events = analyse(candles, "TEST/USDT", "15m", cfg())
    assert expected in event_types(events)


@pytest.mark.parametrize(
    "side,initial_high,initial_low,initial_close,follow_high,follow_low,follow_close",
    [
        ("high", 10.2, 6, 10.05, 10.2, 8, 10.2),
        ("low", 11, 4.8, 4.98, 6, 4.8, 4.8),
    ],
)
def test_pending_cluster_breakout_outcome_both_sides(
    side,
    initial_high,
    initial_low,
    initial_close,
    follow_high,
    follow_low,
    follow_close,
):
    tail = [(follow_high, follow_low, follow_close)]
    candles = (
        high_setup(initial_high, initial_close, cluster=True, tail=tail)
        if side == "high"
        else low_setup(initial_low, initial_close, cluster=True, tail=tail)
    )
    _, events = analyse(candles, "TEST/USDT", "15m", cfg())
    assert "BREAKOUT" in event_types(events)


@pytest.mark.parametrize(
    "side,hit_high,hit_low,close,expected",
    [
        ("high", 10.2, 6, 10.0, "SWING_HIGH_SWEEP"),
        ("low", 11, 4.8, 5.0, "SWING_LOW_SWEEP"),
    ],
)
def test_close_equal_to_level_is_returned_inside_boundary(
    side, hit_high, hit_low, close, expected
):
    candles = (
        high_setup(hit_high, close)
        if side == "high"
        else low_setup(hit_low, close)
    )
    _, events = analyse(candles, "TEST/USDT", "15m", cfg())
    assert expected in event_types(events)


def test_failed_breakout_both_sides():
    high = high_setup(10.2, 10.2, tail=[(10.1, 8, 9.8)])
    _, high_events = analyse(high, "TEST/USDT", "15m", cfg())
    assert "BREAKOUT" in event_types(high_events)
    assert "FAILED_BREAKOUT" in event_types(high_events)

    low = low_setup(4.8, 4.8, tail=[(7, 4.9, 5.2)])
    _, low_events = analyse(low, "TEST/USDT", "15m", cfg())
    assert "BREAKOUT" in event_types(low_events)
    assert "FAILED_BREAKOUT" in event_types(low_events)


def test_pending_cluster_expires_without_event():
    candles = high_setup(
        10.2,
        10.05,
        cluster=True,
        tail=[(10.1, 8, 10.05), (10.1, 8, 10.05)],
    )
    _, events = analyse(
        candles, "TEST/USDT", "15m", cfg(confirm_window_k=2)
    )
    assert not events


def test_breakout_threshold_helper_and_exact_boundaries():
    config = cfg(breakout_buffer_pct=1.0)
    high_boundary = breakout_threshold(10.0, "high", config)
    low_boundary = breakout_threshold(10.0, "low", config)

    assert high_boundary == pytest.approx(10.1)
    assert low_boundary == pytest.approx(9.9)

    # Exact threshold is not a breakout: single swings are consumed silently.
    _, high_events = analyse(
        high_setup(10.2, high_boundary), "TEST/USDT", "15m", config
    )
    _, low_events = analyse(
        low_setup(9.8, low_boundary), "TEST/USDT", "15m", config
    )
    assert not high_events
    assert not low_events


def test_event_fields_volume_ratio_and_structure():
    candles = high_setup(10.2, 9.5)
    candles[0] = candles[0].model_copy(update={"volume": 0})
    candles[1] = candles[1].model_copy(update={"volume": 0})
    candles[2] = candles[2].model_copy(update={"volume": 0})
    candles[3] = candles[3].model_copy(update={"volume": 10})
    _, events = analyse(candles, "TEST/USDT", "15m", cfg())
    event = next(e for e in events if e.event_type == "SWING_HIGH_SWEEP")

    assert event.timestamp == candles[3].timestamp
    assert event.detected_at == candles[3].timestamp + timedelta(minutes=15)
    assert event.price == candles[3].close
    assert event.volume_ratio == 0.0
    assert event.structure == "REJECTION"
    assert event.level_id and event.event_id and event.level_price == 10


def test_volume_ratio_uses_available_prior_candles():
    candles = high_setup(10.2, 9.5)
    candles[0] = candles[0].model_copy(update={"volume": 2})
    candles[1] = candles[1].model_copy(update={"volume": 4})
    candles[2] = candles[2].model_copy(update={"volume": 6})
    candles[3] = candles[3].model_copy(update={"volume": 8})
    _, events = analyse(
        candles, "TEST/USDT", "15m", cfg(volume_lookback=20)
    )
    event = next(e for e in events if e.event_type == "SWING_HIGH_SWEEP")
    assert event.volume_ratio == pytest.approx(8 / 4)


def test_deterministic_level_and_event_ids_and_outputs():
    candles = high_setup(10.2, 9.5)
    first = analyse(candles, "TEST/USDT", "15m", cfg())
    second = analyse(candles, "TEST/USDT", "15m", cfg())
    assert first == second


def test_two_levels_hit_by_one_candle_emit_distinct_event_ids():
    # Both confirmed high levels are pierced and the final close reclaims below both.
    candles = make_candles(
        [
            (8, 6, 7),
            (10, 7, 9),
            (9, 6, 8),
            (8, 6, 7),
            (9, 7, 8),
            (8, 6, 7),
            (11, 6, 8.5),
        ]
    )
    _, events = analyse(candles, "TEST/USDT", "15m", cfg())
    same_candle = [
        event for event in events if event.timestamp == candles[-1].timestamp
    ]
    assert len(same_candle) == 2
    assert same_candle[0].event_type == same_candle[1].event_type
    assert same_candle[0].level_id != same_candle[1].level_id
    assert same_candle[0].event_id != same_candle[1].event_id


def test_input_rejects_unsorted_and_duplicate_timestamps():
    candles = make_candles([(8, 6, 7), (10, 7, 9), (9, 6, 8)])
    with pytest.raises(ValueError, match="first offending timestamp"):
        analyse(candles[::-1], "TEST/USDT", "15m", cfg())

    duplicate = candles[1].model_copy(
        update={"timestamp": candles[0].timestamp}
    )
    with pytest.raises(ValueError, match="first offending timestamp"):
        analyse([candles[0], duplicate, candles[2]], "TEST/USDT", "15m", cfg())


def test_analyse_rejects_gap_and_names_first_offending_timestamp():
    candles = make_candles([(8, 6, 7), (10, 7, 9), (9, 6, 8)])
    offending_timestamp = candles[1].timestamp + timedelta(minutes=30)
    candles[2] = candles[2].model_copy(
        update={"timestamp": offending_timestamp}
    )

    with pytest.raises(ValueError) as exc_info:
        analyse(candles, "TEST/USDT", "15m", cfg())
    assert offending_timestamp.isoformat() in str(exc_info.value)


def test_analyse_accepts_exactly_contiguous_series():
    candles = make_candles(
        [(8, 6, 7), (10, 7, 9), (9, 6, 8), (8, 6, 7)]
    )
    levels, events = analyse(candles, "TEST/USDT", "15m", cfg())
    assert isinstance(levels, list)
    assert isinstance(events, list)


def _random_walk_candles(seed: int, count: int = 300):
    rng = random.Random(seed)
    price = 100.0
    rows = []

    for _ in range(count):
        open_price = price
        price = max(1.0, price + rng.uniform(-2.0, 2.0))
        rows.append(
            {
                "open": open_price,
                "high": max(open_price, price) + rng.random(),
                "low": max(0.01, min(open_price, price) - rng.random()),
                "close": price,
                "volume": rng.uniform(1, 100),
            }
        )
    return make_candles(rows)


def _assert_prefix_no_lookahead(candles, symbol, config, step=1):
    _, full_events = analyse(candles, symbol, "15m", config)

    for prefix_length in range(1, len(candles) + 1, step):
        _, prefix_events = analyse(
            candles[:prefix_length], symbol, "15m", config
        )
        prefix_close = (
            candles[prefix_length - 1].timestamp + timedelta(minutes=15)
        )
        expected = [
            event for event in full_events if event.detected_at <= prefix_close
        ]
        assert prefix_events == expected

    if len(candles) % step:
        _, prefix_events = analyse(candles, symbol, "15m", config)
        prefix_close = candles[-1].timestamp + timedelta(minutes=15)
        expected = [
            event for event in full_events if event.detected_at <= prefix_close
        ]
        assert prefix_events == expected

    return full_events


def test_no_lookahead_hand_built_series_has_all_required_event_types():
    # Start with a seeded price path that naturally exercises sweeps, breakouts,
    # and failed breakouts; append a price-isolated hand-built cluster grab.
    rows = [
        {
            "open": candle.open,
            "high": candle.high,
            "low": candle.low,
            "close": candle.close,
            "volume": candle.volume,
        }
        for candle in _random_walk_candles(42)
    ]
    rows.extend(
        {
            "open": candle.open + 1000,
            "high": candle.high + 1000,
            "low": candle.low + 1000,
            "close": candle.close + 1000,
            "volume": candle.volume,
        }
        for candle in high_setup(10.2, 9.5, cluster=True)
    )
    candles = make_candles(rows)
    config = cfg(
        swing_bars=3,
        equal_tolerance_pct=0.1,
        confirm_window_k=3,
        breakout_buffer_pct=0.1,
        volume_lookback=20,
    )
    _, full_events = analyse(candles, "HAND/USDT", "15m", config)
    required = {
        "SWING_HIGH_SWEEP",
        "LIQUIDITY_GRAB",
        "BREAKOUT",
        "FAILED_BREAKOUT",
    }
    assert required.issubset(set(event_types(full_events)))
    _assert_prefix_no_lookahead(candles, "HAND/USDT", config, step=1)


def test_no_lookahead_seeded_random_walk_300_candles():
    candles = _random_walk_candles(42)
    config = cfg(
        swing_bars=3,
        equal_tolerance_pct=0.1,
        confirm_window_k=3,
        breakout_buffer_pct=0.1,
        volume_lookback=20,
    )
    _, full_events = analyse(candles, "RANDOM42/USDT", "15m", config)
    counts = Counter(event.event_type for event in full_events)
    print(f"seed=42 event counts: {dict(sorted(counts.items()))}")
    assert len(full_events) >= 10
    assert len(counts) >= 3
    _assert_prefix_no_lookahead(candles, "RANDOM42/USDT", config, step=1)


def test_no_lookahead_seed_7_random_walk_300_candles():
    candles = _random_walk_candles(7)
    config = cfg(
        swing_bars=2,
        equal_tolerance_pct=0.1,
        confirm_window_k=2,
        breakout_buffer_pct=0.1,
        volume_lookback=20,
    )
    _, full_events = analyse(candles, "RANDOM7/USDT", "15m", config)
    counts = Counter(event.event_type for event in full_events)
    print(f"seed=7 event counts: {dict(sorted(counts.items()))}")
    assert len(full_events) >= 10
    assert len(counts) >= 3
    _assert_prefix_no_lookahead(candles, "RANDOM7/USDT", config, step=1)


def test_seed_42_find_swings_are_represented_by_analysis_levels():
    candles = _random_walk_candles(42)
    config = cfg(
        swing_bars=3,
        equal_tolerance_pct=0.1,
        confirm_window_k=3,
        breakout_buffer_pct=0.1,
        volume_lookback=20,
    )
    swings = find_swings(candles, "15m", config)
    levels, _ = analyse(candles, "RANDOM42/USDT", "15m", config)

    for swing in swings:
        matching_kind = (
            {"SWING_HIGH", "EQUAL_HIGHS"}
            if swing.kind == "SWING_HIGH"
            else {"SWING_LOW", "EQUAL_LOWS"}
        )
        represented = any(
            level.kind in matching_kind
            and (
                level.formed_at == swing.formed_at
                or (
                    level.kind in ("EQUAL_HIGHS", "EQUAL_LOWS")
                    and level.formed_at <= swing.formed_at < level.confirmed_at
                )
            )
            for level in levels
        )
        assert represented, (
            f"Swing {swing.kind} at {swing.formed_at} "
            "has no level representation"
        )


def test_causal_clustering_joins_unpierced_level_and_third_touch():
    # Three highs within tolerance confirm causally; final cluster has 3 touches.
    candles = make_candles(
        [
            (8, 6, 7),
            (10, 7, 9),
            (9, 6, 8),
            (8, 6, 7),
            (9.995, 7, 8),
            (8, 6, 7),
            (7, 6, 6.5),
            (9.992, 7, 8),
            (8, 6, 7),
        ]
    )
    levels, _ = analyse(
        candles, "TEST/USDT", "15m", cfg(equal_tolerance_pct=0.1)
    )
    clusters = [level for level in levels if level.kind == "EQUAL_HIGHS"]
    assert clusters and max(level.touches for level in clusters) == 3


def test_causal_clustering_does_not_join_pierced_level():
    candles = make_candles(
        [
            (8, 6, 7),
            (10, 7, 9),
            (9, 6, 8),
            (10.2, 6, 10.2),
            (8, 6, 7),
            (9.995, 7, 8),
            (8, 6, 7),
        ]
    )
    levels, _ = analyse(
        candles, "TEST/USDT", "15m", cfg(equal_tolerance_pct=0.1)
    )
    assert not any(level.kind == "EQUAL_HIGHS" for level in levels)


def test_causal_clustering_outside_tolerance_stays_separate():
    candles = make_candles(
        [
            (8, 6, 7),
            (10, 7, 9),
            (9, 6, 8),
            (8, 6, 7),
            (8.5, 6, 7),
            (7, 6, 6.5),
        ]
    )
    levels, _ = analyse(
        candles, "TEST/USDT", "15m", cfg(equal_tolerance_pct=0.1)
    )
    highs = [
        level
        for level in levels
        if level.kind in ("SWING_HIGH", "EQUAL_HIGHS")
    ]
    assert len(highs) >= 2


def test_low_clustering_outside_tolerance_stays_separate():
    # 5.006 is 0.12% above 5.0, outside the configured 0.10% tolerance.
    candles = make_candles(
        [
            (10, 8, 9),
            (9, 5, 6),
            (10, 7, 9),
            (10, 7, 9),
            (9, 5.006, 6),
            (10, 7, 9),
        ]
    )
    levels, _ = analyse(
        candles, "TEST/USDT", "15m", cfg(equal_tolerance_pct=0.1)
    )
    lows = [
        level for level in levels if level.kind in ("SWING_LOW", "EQUAL_LOWS")
    ]
    assert len(lows) >= 2


def test_cluster_close_exactly_at_breakout_threshold_stays_pending_then_expires():
    config = cfg(breakout_buffer_pct=1.0, confirm_window_k=2)
    threshold = breakout_threshold(10.0, "high", config)
    candles = high_setup(
        10.2,
        threshold,
        cluster=True,
        tail=[(10.1, 8, threshold), (10.1, 8, threshold)],
    )
    levels, events = analyse(candles, "TEST/USDT", "15m", config)

    assert "BREAKOUT" not in event_types(events)
    assert "LIQUIDITY_GRAB" not in event_types(events)
    pending_cluster = next(
        level for level in levels if level.kind == "EQUAL_HIGHS"
    )
    assert pending_cluster.swept is True


def test_levels_preserve_swept_flags_touches_and_confirmation_times():
    candles = make_candles(
        [
            (8, 6, 7),
            (10, 7, 9),
            (9, 6, 8),
            (8, 6, 7),
            (7, 5, 6),
            (8, 6, 7),
            (7, 5.5, 6),
        ]
    )
    levels, _ = analyse(candles, "TEST/USDT", "15m", cfg())
    high_level = next(level for level in levels if level.kind == "SWING_HIGH")
    assert high_level.swept is False
    assert high_level.touches == 1
    assert high_level.confirmed_at == candles[2].timestamp + timedelta(minutes=15)

    hit_candles = high_setup(10.2, 9.5)
    hit_levels, hit_events = analyse(hit_candles, "TEST/USDT", "15m", cfg())
    swept = next(level for level in hit_levels if level.kind == "SWING_HIGH")
    assert swept.swept is True
    assert swept.touches == 1
    assert swept.confirmed_at == (
        hit_candles[2].timestamp + timedelta(minutes=15)
    )
    assert any(event.level_id == swept.level_id for event in hit_events)


def test_series_too_short_for_any_swing_returns_empty_results():
    candles = make_candles([(8, 6, 7), (10, 7, 9)])
    assert analyse(candles, "TEST/USDT", "15m", cfg(swing_bars=2)) == ([], [])


def test_analyse_1000_candles_timing_report():
    rng = random.Random(42)
    price = 100.0
    rows = []

    for _ in range(1000):
        open_price = price
        price = max(1.0, price + rng.uniform(-2, 2))
        rows.append(
            {
                "open": open_price,
                "high": max(open_price, price) + rng.random(),
                "low": max(0.01, min(open_price, price) - rng.random()),
                "close": price,
                "volume": rng.uniform(1, 100),
            }
        )

    candles = make_candles(rows)
    started = time.perf_counter()
    analyse(candles, "PERF/USDT", "15m", cfg(swing_bars=3))
    elapsed = time.perf_counter() - started

    print(f"analyse() on 1000 candles: {elapsed:.6f} seconds")
    assert elapsed >= 0