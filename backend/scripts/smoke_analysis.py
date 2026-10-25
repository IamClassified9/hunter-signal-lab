"""Live Binance smoke check for the Phase 1 liquidity engine."""

from __future__ import annotations

import asyncio
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Ensure `python scripts\\smoke_analysis.py` works from the backend directory.
BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from app.liquidity_engine import analyse  # noqa: E402
from app.market_data import MarketDataService  # noqa: E402
from app.schemas import EngineConfig, TIMEFRAME_SECONDS  # noqa: E402


async def main() -> None:
    service = MarketDataService()
    try:
        now = datetime.now(timezone.utc)
        candles = await service.get_candles(
            "BTC/USDT", "15m", limit=500, now=now
        )

        analysis_started = time.perf_counter()
        levels, events = analyse(
            candles, "BTC/USDT", "15m", EngineConfig()
        )
        analysis_elapsed = time.perf_counter() - analysis_started

        print(f"Candle count: {len(candles)}")
        print(
            "First candle open: "
            f"{candles[0].timestamp.isoformat() if candles else 'n/a'}"
        )
        print(
            "Last candle open: "
            f"{candles[-1].timestamp.isoformat() if candles else 'n/a'}"
        )
        print(f"analyse() elapsed seconds: {analysis_elapsed:.6f}")

        print("Level counts by kind:")
        for kind, count in sorted(
            Counter(level.kind for level in levels).items()
        ):
            print(f"  {kind}: {count}")

        print("Event counts by type:")
        for event_type, count in sorted(
            Counter(event.event_type for event in events).items()
        ):
            print(f"  {event_type}: {count}")

        print("Last 5 events (timestamp, detected_at, type, price, level_price):")
        for event in events[-5:]:
            print(
                f"  {event.timestamp.isoformat()}, "
                f"{event.detected_at.isoformat()}, "
                f"{event.event_type}, {event.price}, {event.level_price}"
            )

        assert all(
            event.detected_at is not None and event.detected_at <= now
            for event in events
        )
        assert all(
            candle.timestamp
            + timedelta(seconds=TIMEFRAME_SECONDS["15m"])
            <= now
            for candle in candles
        )
    finally:
        await service.close()


if __name__ == "__main__":
    asyncio.run(main())