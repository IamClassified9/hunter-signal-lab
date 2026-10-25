from __future__ import annotations

import json

import httpx
import pytest

from app import ai_service


@pytest.mark.asyncio
async def test_brief_uses_fake_gemma_and_keeps_engine_evidence(monkeypatch):
    events = [{
        "event_id": "EVT-1", "symbol": "BTC/USDT", "timeframe": "15m",
        "event_type": "BREAKOUT", "price": 80291.7, "volume_ratio": 15.53,
        "structure": "SUPPORT_BREAK", "timestamp": "2026-10-25T04:00:00Z",
        "level_price": 80451.18, "detected_at": "2026-10-25T04:15:00Z",
    }]
    fake_response = json.dumps({
        "summary": ["One breakout event was selected.", "The supplied event is recorded evidence, not a prediction."],
        "events": [{"sentences": [
            "Hunter recorded a breakout for BTC/USDT at 80291.7.",
            "The linked level is 80451.18 and the reported volume ratio is 15.53x.",
        ]}],
    })
    captured = {}

    async def fake_model(prompt: str) -> str:
        captured["prompt"] = prompt
        return fake_response

    monkeypatch.setattr(ai_service, "_request_gemma", fake_model)
    monkeypatch.setattr(ai_service, "BRIEF_MODEL", "test-model")
    result = await ai_service.generate_brief(events)

    assert result["source"] == "gemma"
    assert result["temperature"] == 0.2
    assert result["summary"] == ["One breakout event was selected.", "The supplied event is recorded evidence, not a prediction."]
    assert len(result["events"][0]["sentences"]) == 2
    assert result["events"][0]["evidence"]["price"] == 80291.7
    assert "80291.7" in captured["prompt"]
    assert "Temperature is 0.2" in captured["prompt"]


@pytest.mark.asyncio
async def test_brief_fails_when_gemma_is_unavailable_by_default(monkeypatch):
    events = [{
        "event_id": "EVT-2", "symbol": "ETH/USDT", "timeframe": "1h",
        "event_type": "LIQUIDITY_GRAB", "price": 2500.0, "volume_ratio": 1.4,
        "structure": "REJECTION", "timestamp": "2026-10-25T04:00:00Z",
        "level_price": 2510.0,
    }]

    async def unavailable(_: str) -> str:
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(ai_service, "_request_gemma", unavailable)
    monkeypatch.setattr(ai_service, "BRIEF_MODEL", "gemma-test-model")
    monkeypatch.setattr(ai_service, "ALLOW_TEMPLATE_FALLBACK", False)
    with pytest.raises(Exception, match="Gemma brief generation failed"):
        await ai_service.generate_brief(events)

    monkeypatch.setattr(ai_service, "ALLOW_TEMPLATE_FALLBACK", True)
    result = await ai_service.generate_brief(events)
    assert result["source"] == "template"
    assert result["model"] == "template-fallback"
    assert len(result["summary"]) == 2
    assert len(result["events"][0]["sentences"]) == 2
    assert "2500" in result["events"][0]["sentences"][0]


@pytest.mark.asyncio
async def test_brief_fails_for_malformed_model_output_by_default(monkeypatch):
    async def malformed(_: str) -> str:
        return "not json"

    monkeypatch.setattr(ai_service, "_request_gemma", malformed)
    monkeypatch.setattr(ai_service, "BRIEF_MODEL", "gemma-test-model")
    monkeypatch.setattr(ai_service, "ALLOW_TEMPLATE_FALLBACK", False)
    with pytest.raises(Exception, match="Gemma brief generation failed"):
        await ai_service.generate_brief([{
            "event_id": "EVT-3", "symbol": "BTC/USDT", "timeframe": "15m",
            "event_type": "BREAKOUT", "price": 100.0, "volume_ratio": 2.0,
            "structure": "SUPPORT_BREAK",
        }])


@pytest.mark.asyncio
async def test_gemma_health_reports_missing_configuration(monkeypatch):
    monkeypatch.delenv("GEMMA_API_KEY", raising=False)
    monkeypatch.setattr(ai_service, "BRIEF_MODEL", "gemma-test-model")
    result = await ai_service.gemma_health()
    assert result["configured"] is False
    assert result["loaded"] is False
    assert "not configured" in result["detail"]


@pytest.mark.asyncio
async def test_gemma_health_checks_configured_model(monkeypatch):
    monkeypatch.setenv("GEMMA_API_KEY", "test-key")
    monkeypatch.setattr(ai_service, "BRIEF_MODEL", "gemma-test-model")
    class Models:
        def get(self, model):
            assert model == "gemma-test-model"
    class Client:
        models = Models()
        def __init__(self, *args, **kwargs): pass
    monkeypatch.setattr(ai_service.genai, "Client", Client)
    result = await ai_service.gemma_health()
    assert result["loaded"] is True
    assert result["model"] == "gemma-test-model"
