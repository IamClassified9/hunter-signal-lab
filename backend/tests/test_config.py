from __future__ import annotations

from main import DEFAULT_CORS_ORIGINS, cors_origins
from app.market_data import DEFAULT_SYMBOLS, MarketDataService


class _NoNetworkExchange:
    """Stand-in exchange: the service constructor must not need the network."""


def test_cors_origins_default_when_env_missing(monkeypatch):
    monkeypatch.delenv("HUNTER_CORS_ORIGINS", raising=False)
    assert cors_origins() == DEFAULT_CORS_ORIGINS.split(",")


def test_cors_origins_reads_env_trims_spaces_and_trailing_slashes(monkeypatch):
    monkeypatch.setenv(
        "HUNTER_CORS_ORIGINS",
        " https://demo.example.com/ , https://other.example.com ,, ",
    )
    assert cors_origins() == [
        "https://demo.example.com",
        "https://other.example.com",
    ]


def test_cors_origins_falls_back_to_default_when_env_is_blank(monkeypatch):
    monkeypatch.setenv("HUNTER_CORS_ORIGINS", " , ")
    assert cors_origins() == DEFAULT_CORS_ORIGINS.split(",")


def test_default_symbols_cover_six_pairs_and_keep_btc_eth_first(monkeypatch):
    monkeypatch.delenv("HUNTER_SYMBOLS", raising=False)
    service = MarketDataService(exchange=_NoNetworkExchange())
    assert service.supported_symbols == tuple(DEFAULT_SYMBOLS.split(","))
    assert service.supported_symbols[:2] == ("BTC/USDT", "ETH/USDT")
    assert len(service.supported_symbols) == 6


def test_symbols_env_overrides_default(monkeypatch):
    monkeypatch.setenv("HUNTER_SYMBOLS", "BTC/USDT, SOL/USDT")
    service = MarketDataService(exchange=_NoNetworkExchange())
    assert service.supported_symbols == ("BTC/USDT", "SOL/USDT")
