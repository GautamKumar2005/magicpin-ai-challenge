"""Thin wrapper around the Gemini API (Google AI Studio / generativelanguage).

Kept isolated so composer.py never has to know whether the LLM is present,
slow, or missing -- it just calls generate_json() and falls back on any
exception (missing key, timeout, malformed response).

Uses the plain REST endpoint (no google-genai SDK dependency) so the only
extra package needed is `requests`.
"""

import json
import re

import requests

from app import config

GEMINI_URL_TMPL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def generate_json(system: str, user: str) -> dict:
    """Calls Gemini with temperature=0 and parses a JSON object out of the
    response. Raises on any failure -- callers must catch and fall back to
    the deterministic template composer."""
    if not config.GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY not set")

    url = GEMINI_URL_TMPL.format(model=config.MODEL)
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {
            "temperature": 0,
            "maxOutputTokens": config.LLM_MAX_TOKENS,
            "responseMimeType": "application/json",
        },
    }
    resp = requests.post(
        url,
        params={"key": config.GEMINI_API_KEY},
        json=body,
        timeout=config.LLM_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()
    data = resp.json()

    candidates = data.get("candidates") or []
    if not candidates:
        block_reason = (data.get("promptFeedback") or {}).get("blockReason")
        raise RuntimeError(f"Gemini returned no candidates (blockReason={block_reason})")

    parts = candidates[0].get("content", {}).get("parts", [])
    raw = "".join(p.get("text", "") for p in parts).strip()
    if not raw:
        raise RuntimeError("Gemini returned an empty response")
    return _extract_json(raw)


def _extract_json(raw: str) -> dict:
    # Strip markdown code fences if the model wrapped its answer despite
    # responseMimeType=application/json (happens occasionally).
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", raw, re.DOTALL)
    candidate = fenced.group(1) if fenced else raw
    if not candidate.strip().startswith("{"):
        brace = re.search(r"\{.*\}", candidate, re.DOTALL)
        if brace:
            candidate = brace.group(0)
    return json.loads(candidate)
