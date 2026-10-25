from __future__ import annotations

import pytest

from tests.test_market_data import FakeExchange, make_service, row


def spot(base: str) -> dict:
    return {"base": base, "quote": "USDT", "spot": True, "active": True}


@pytest.mark.asyncio
async def test_cache_is_bounded(monkeypatch):
    monkeypatch.setattr("app.market_data.MAX_CACHE_ENTRIES", 2)
    service = make_service(FakeExchange([row(-30)]))
    for limit in (1, 2, 3):
        await service.get_candles("BTC/USDT", "15m", limit=limit)
    assert len(service._cache) == 2
    assert (service.exchange_id, "BTC/USDT", "15m", 3) in service._cache
    assert (service.exchange_id, "BTC/USDT", "15m", 1) not in service._cache


@pytest.mark.asyncio
async def test_non_binance_exchange_still_serves_configured_symbols(monkeypatch):
    # The documented fallback for regions that block Binance must keep working.
    monkeypatch.setenv("HUNTER_EXCHANGE", "kraken")
    candles = await make_service(FakeExchange([row(-30)])).get_candles(
        "BTC/USDT", "15m", limit=1
    )
    assert len(candles) == 1


@pytest.mark.asyncio
async def test_search_ranks_exact_then_prefix_then_contains():
    markets = {
        "XSOL/USDT": spot("XSOL"),
        "SOLO/USDT": spot("SOLO"),
        "1000SOL/USDT": spot("1000SOL"),
        "SOL/USDT": spot("SOL"),
        "BTC/USDT": spot("BTC"),
    }
    service = make_service(FakeExchange(markets=markets))
    results = await service.search_markets("sol")
    assert [item["symbol"] for item in results] == [
        "SOL/USDT",
        "SOLO/USDT",
        "1000SOL/USDT",
        "XSOL/USDT",
    ]
