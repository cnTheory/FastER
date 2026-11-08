"""Server-side Gemini helper. The API key never leaves the backend."""
import json
import os
import re

import httpx

DEFAULT_MODEL = "gemini-2.5-flash"


def configured():
    return bool(os.getenv("GEMINI_API_KEY"))


async def generate_json(system, payload, max_tokens=900):
    """Ask Gemini for a JSON object. Returns a dict, or None on ANY failure (caller falls back)."""
    key = os.getenv("GEMINI_API_KEY")
    model = os.getenv("GEMINI_MODEL") or DEFAULT_MODEL
    if not key or not re.fullmatch(r"[A-Za-z0-9._-]+", model):
        return None
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                headers={"x-goog-api-key": key},
                json={"systemInstruction": {"parts": [{"text": system}]},
                      "contents": [{"role": "user", "parts": [{"text": json.dumps(payload)}]}],
                      "generationConfig": {"temperature": 0.1, "maxOutputTokens": max_tokens,
                                           "responseMimeType": "application/json"}})
            r.raise_for_status()
            parts = r.json()["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except (httpx.HTTPError, KeyError, IndexError, ValueError, TypeError):
        return None
