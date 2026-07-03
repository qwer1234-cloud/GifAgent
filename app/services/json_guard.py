"""Unified JSON parser for all VLM/LLM responses.

Replaces scattered _parse_json_response() copies across:
  - app/services/vision.py
  - app/services/llm.py
  - scripts/vlm_*.py
  - scripts/test_*.py
"""
import json, re
from dataclasses import dataclass


@dataclass
class JsonParseResult:
    ok: bool
    data: dict | None
    raw: str
    error: str | None


def parse_json_response(text: str) -> JsonParseResult:
    """Parse JSON from a model response. Handles markdown fences, think tags, etc."""
    if not text or not text.strip():
        return JsonParseResult(ok=False, data=None, raw=text, error="Empty input")

    cleaned = text.strip()

    # Strip model reasoning tags when present.
    if "</think>" in cleaned:
        cleaned = cleaned.split("</think>")[-1].strip()
    cleaned = re.sub(r"<think>.*?</think>", "", cleaned, flags=re.DOTALL).strip()

    # Strip markdown code fences
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
        cleaned = cleaned.strip()

    # Try strict parse
    try:
        data = json.loads(cleaned)
        return JsonParseResult(ok=True, data=data, raw=text, error=None)
    except json.JSONDecodeError as e:
        pass

    # Try to extract the first complete JSON object
    # Match balanced braces: { ... }
    depth = 0
    start = -1
    for i, ch in enumerate(cleaned):
        if ch == '{':
            if depth == 0:
                start = i
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    data = json.loads(cleaned[start:i + 1])
                    return JsonParseResult(ok=True, data=data, raw=text, error=None)
                except json.JSONDecodeError:
                    pass

    return JsonParseResult(
        ok=False, data=None, raw=text,
        error=f"Could not parse JSON from {len(text)}-char response",
    )
