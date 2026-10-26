from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import datetime, timezone
from typing import Any

from google import genai
from google.genai import types

BRIEF_TEMPERATURE = 0.2
BRIEF_MODEL = os.getenv("BRIEF_MODEL", "").strip()
ALLOW_TEMPLATE_FALLBACK = os.getenv("ALLOW_TEMPLATE_FALLBACK", "false").strip().lower() == "true"


def _format_timestamps_in_text(text: str) -> str:
    """Render ISO-8601 timestamps in brief prose as readable UTC dates."""
    timestamp_pattern = re.compile(
        r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)\b"
    )

    def format_match(match: re.Match[str]) -> str:
        value = match.group(0)
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.astimezone(timezone.utc).strftime("%d %b %Y, %H:%M:%S UTC")
        except ValueError:
            return value

    return timestamp_pattern.sub(format_match, text)


def _event_evidence(event: dict[str, Any]) -> dict[str, Any]:
    """Expose only supplied engine fields."""
    keys = (
        "event_id", "symbol", "timeframe", "event_type", "price",
        "volume_ratio", "structure", "timestamp", "level_id",
        "level_price", "detected_at", "level_swept", "level_kind",
        "level_touches",
    )
    return {key: event[key] for key in keys if key in event}


def _template_sentences(event: dict[str, Any]) -> list[str]:
    symbol = str(event.get("symbol", "Unknown market"))
    timeframe = str(event.get("timeframe", "unknown timeframe"))
    event_type = str(event.get("event_type", "EVENT")).replace("_", " ").lower()
    price = event.get("price")
    level_price = event.get("level_price")
    volume_ratio = event.get("volume_ratio")
    structure = str(event.get("structure", "unspecified structure")).replace("_", " ").lower()

    price_text = f"{price:g}" if isinstance(price, (int, float)) else "not supplied"
    volume_text = f"{volume_ratio:.2f}x" if isinstance(volume_ratio, (int, float)) else "not supplied"

    first = (
        f"Hunter recorded a {event_type} for {symbol} on the "
        f"{timeframe} timeframe at price {price_text}."
    )

    if isinstance(level_price, (int, float)):
        second = (
            f"The event is linked to level {level_price:g}, with volume ratio "
            f"{volume_text} and structure marked {structure}."
        )
    else:
        second = (
            f"The engine reports volume ratio {volume_text} and structure "
            f"marked {structure}; no linked level price was supplied."
        )

    return [first, second]


def _template_summary(events: list[dict[str, Any]]) -> list[str]:
    if not events:
        return [
            "Hunter returned no liquidity events for the selected watchlist and timeframes.",
            "No AI interpretation was generated because there were no events to describe.",
        ]

    symbols = sorted({str(event.get("symbol", "unknown")) for event in events})
    event_types = sorted({
        str(event.get("event_type", "event")).replace("_", " ").lower()
        for event in events
    })

    return [
        f"The brief contains {len(events)} selected event(s) across {', '.join(symbols)}.",
        f"Recorded event types: {', '.join(event_types)}; these are observations, not predictions or trading advice.",
    ]


def _parse_model_json(
    answer: str,
    events: list[dict[str, Any]],
) -> tuple[list[str], list[list[str]]]:
    """Validate the required two-line summary and two sentences per event."""
    cleaned = answer.strip()

    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()

    try:
        data = json.loads(cleaned)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Gemma response was not valid JSON") from exc

    summary = data.get("summary") if isinstance(data, dict) else None
    items = data.get("events") if isinstance(data, dict) else None

    if (
        not isinstance(summary, list)
        or len(summary) != 2
        or not all(isinstance(x, str) and x.strip() for x in summary)
    ):
        raise ValueError("Gemma response must contain exactly two summary lines")

    if not isinstance(items, list) or len(items) != len(events):
        raise ValueError("Gemma response event count did not match input")

    explanations: list[list[str]] = []

    for item in items:
        sentences = item.get("sentences") if isinstance(item, dict) else None
        if (
            not isinstance(sentences, list)
            or len(sentences) != 2
            or not all(isinstance(x, str) and x.strip() for x in sentences)
        ):
            raise ValueError("Each event must contain exactly two sentences")

        explanations.append([sentence.strip() for sentence in sentences])

    return [line.strip() for line in summary], explanations


def _api_key() -> str:
    return os.getenv("GEMMA_API_KEY", "").strip()


def _generate_content(prompt: str) -> str:
    """Synchronous SDK call; invoked from an async-safe worker thread."""
    api_key = _api_key()
    if not BRIEF_MODEL:
        raise RuntimeError("BRIEF_MODEL is not configured in the backend environment")
    if not api_key:
        raise RuntimeError("GEMMA_API_KEY is not configured")

    client = genai.Client(api_key=api_key)

    response = client.models.generate_content(
        model=BRIEF_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=BRIEF_TEMPERATURE,
        ),
    )

    answer = response.text
    if not isinstance(answer, str) or not answer.strip():
        raise ValueError("Gemma returned an empty response")

    return answer.strip()


async def _request_gemma(prompt: str) -> str:
    return await asyncio.to_thread(_generate_content, prompt)


async def gemma_health() -> dict[str, Any]:
    """Check key configuration and whether the configured model is accessible."""
    if not BRIEF_MODEL:
        return {
            "configured": False, "loaded": False, "model": None,
            "provider": "Google Gen AI", "detail": "BRIEF_MODEL is not configured in the backend environment",
        }
    if not _api_key():
        return {
            "configured": False,
            "loaded": False,
            "model": BRIEF_MODEL,
            "provider": "Google Gen AI",
            "detail": "GEMMA_API_KEY is not configured",
        }

    try:
        def check_model() -> None:
            client = genai.Client(api_key=_api_key())
            client.models.get(model=BRIEF_MODEL)

        await asyncio.to_thread(check_model)

        return {
            "configured": True,
            "loaded": True,
            "model": BRIEF_MODEL,
            "provider": "Google Gen AI",
            "detail": "API key accepted and model is accessible",
        }

    except Exception as exc:
        return {
            "configured": True,
            "loaded": False,
            "model": BRIEF_MODEL,
            "provider": "Google Gen AI",
            "detail": f"{type(exc).__name__}: {str(exc)[:240]}",
        }




async def generate_brief(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Generate evidence-only event explanations; fallback is explicitly opt-in."""
    from fastapi import HTTPException

    selected = [_event_evidence(event) for event in events[:3]]
    if not selected:
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "model": BRIEF_MODEL or "not configured",
            "source": "gemma",
            "temperature": BRIEF_TEMPERATURE,
            "fallback_reason": None,
            "summary": [
                "Hunter returned no liquidity events for the selected watchlist and timeframes.",
                "No AI interpretation was needed because there were no events to describe.",
            ],
            "events": [],
            "disclaimer": "Recorded engine observations only. No predictions or trading advice.",
        }

    if not BRIEF_MODEL:
        raise HTTPException(status_code=503, detail="BRIEF_MODEL is not configured in the backend environment.")

    prompt = (
        "You write an evidence-only liquidity brief for Hunter Signal Lab. "
        "Use ONLY the supplied event records. Do not predict prices, recommend "
        "buying or selling, infer causation, or invent missing values. "
        "Do not call an event current unless timestamps support that statement. "
        "For each event, explain the event structure and linked level in plain "
        "English. State swept/unswept status only when explicitly supplied; "
        "otherwise say status was not supplied. Explain volume_ratio only as "
        "a comparison to the engine baseline, never as proof of buying or selling. "
        "Distinguish timestamp (event candle open) from detected_at (when Hunter "
        "detected or confirmed the event). Mention that follow-through cannot be "
        "assessed when later candles are not supplied. Do not invent facts. "
        "Return JSON only in this shape: "
        '{"summary":["line 1","line 2"],'
        '"events":[{"sentences":["sentence 1","sentence 2"]}]}. '
        "The summary must have exactly two short lines. Each event must have "
        "exactly two plain-English sentences. Preserve event order. "
        "Temperature is 0.2.\nEVENTS:\n"
        + json.dumps(selected, ensure_ascii=False, separators=(",", ":"))
    )

    try:
        answer = await _request_gemma(prompt)
        summary, explanations = _parse_model_json(answer, selected)
        # Keep timestamps in the evidence payload unchanged; format only the prose shown in the UI.
        summary = [_format_timestamps_in_text(line) for line in summary]
        explanations = [
            [_format_timestamps_in_text(sentence) for sentence in sentences]
            for sentences in explanations
        ]
        source = "gemma"
        model = BRIEF_MODEL
        fallback_reason = None
    except Exception as exc:
        fallback_reason = f"{type(exc).__name__}: {str(exc)[:240]}"
        if not ALLOW_TEMPLATE_FALLBACK:
            raise HTTPException(
                status_code=502,
                detail=f"Gemma brief generation failed ({fallback_reason}). Template fallback is disabled. Set ALLOW_TEMPLATE_FALLBACK=true in the backend environment to explicitly enable it.",
            ) from exc
        summary = _template_summary(selected)
        explanations = [_template_sentences(event) for event in selected]
        source = "template"
        model = "template-fallback"

    result_events = [
        {
            "event_id": evidence.get("event_id"),
            "evidence": evidence,
            "sentences": sentences,
        }
        for evidence, sentences in zip(selected, explanations)
    ]

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model": model,
        "source": source,
        "temperature": BRIEF_TEMPERATURE if source == "gemma" else None,
        "fallback_reason": fallback_reason,
        "summary": summary,
        "events": result_events,
        "disclaimer": "Recorded engine observations only. No predictions or trading advice.",
    }


# Preserve compatibility with the existing /api/ai/interpret endpoint.
async def interpret_analysis(analysis: dict) -> dict[str, Any]:
    prompt = (
        "Analyze this Hunter Signal Lab market analysis using only supplied "
        "evidence. Do not invent prices or indicators. Do not make predictions "
        "or trading recommendations.\n\n"
        + json.dumps(analysis, default=str)
    )

    try:
        answer = await _request_gemma(prompt)
    except Exception as exc:
        from fastapi import HTTPException

        raise HTTPException(
            status_code=502,
            detail=f"Gemma service request failed: {type(exc).__name__}: {str(exc)[:240]}",
        ) from exc

    return {
        "model": BRIEF_MODEL,
        "analysis": answer,
        "source": "hunter_live_analysis",
        "symbol": analysis.get("symbol"),
        "timeframe": analysis.get("timeframe"),
    }
