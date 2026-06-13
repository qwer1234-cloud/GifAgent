"""VLM Vision Service — analyze individual frames using the VLM model."""
import base64
import json
import re
import uuid
from datetime import datetime, timezone

import httpx

from app.db import get_connection
from app.config import get

VLM_BASE = get("vlm.base_url", "http://localhost:11434")
VLM_MODEL = get("vlm.model", "llava:13b")

FRAME_PROMPT = """You are analyzing a single frame from a movie or TV show. Focus on CINEMATIC and AESTHETIC qualities.

Output ONLY a valid JSON object with real, specific content. No placeholder text, no template values, no markdown fencing.

{
  "caption": "describe what you actually see in this specific frame - composition, lighting, what makes it visually striking",
  "emotional_core": "intimacy",
  "aesthetic_notes": ["warm amber lighting wraps the subjects", "shallow depth of field isolates the figures from the background"],
  "why_i_like_it": "the vulnerability in the actors' body language draws you into their private world"
}

IMPORTANT RULES:
- emotional_core MUST be EXACTLY ONE lowercase word. Choose from: tension, melancholy, awe, joy, sadness, catharsis, serenity, excitement, dread, nostalgia, admiration, intimacy, vulnerability, longing, desire.
- NEVER output multiple emotions joined with "|" or commas. Pick the single most dominant one.
- aesthetic_notes MUST describe what you actually observe. 2-4 specific, concrete observations.
- caption and why_i_like_it MUST contain real descriptions, not the instruction text itself."""


def _parse_json_response(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{[^{}]*\{[^{}]*\}[^{}]*\}|\{[^{}]*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return {"_parse_error": True, "_raw": text[:500]}


def analyze_frame(frame_id: str, image_path: str, media_id: str) -> dict:
    """Call the VLM model to analyze a single frame. Returns annotation dict."""
    with open(image_path, "rb") as f:
        image_bytes = f.read()

    resp = httpx.post(
        f"{VLM_BASE}/api/generate",
        json={
            "model": VLM_MODEL,
            "prompt": FRAME_PROMPT,
            "images": [base64.b64encode(image_bytes).decode("utf-8")],
            "stream": False,
        },
        timeout=120,
    )
    resp.raise_for_status()
    data = resp.json()
    response_text = data.get("response", "")

    parsed = _parse_json_response(response_text)
    if parsed.get("_parse_error"):
        print(f"[WARN] JSON parse failed for frame {frame_id}: {parsed.get('_raw', '')[:200]}")

    # Post-process: clean up emotional_core (model may return pipe-delimited list or template text)
    VALID_EMOTIONS = {"tension", "melancholy", "awe", "joy", "sadness", "catharsis", "serenity",
                      "excitement", "dread", "nostalgia", "admiration", "intimacy", "vulnerability",
                      "longing", "desire", "other"}
    raw_emotion = (parsed.get("emotional_core") or "").strip().lower()
    if raw_emotion and raw_emotion not in VALID_EMOTIONS:
        # Try to extract first valid emotion from pipe-delimited or comma-delimited string
        parts = [p.strip() for p in raw_emotion.replace("|", ",").split(",")]
        found = next((p for p in parts if p in VALID_EMOTIONS), None)
        parsed["emotional_core"] = found if found else "other"

    # Post-process: discard template/placeholder text in caption
    raw_caption = (parsed.get("caption") or "").strip()
    if not raw_caption or raw_caption.startswith("concise description") or raw_caption.startswith("describe what"):
        parsed["caption"] = ""

    annotation_id = f"ann_{uuid.uuid4().hex[:12]}"
    now = datetime.now(timezone.utc).isoformat()

    conn = get_connection()
    conn.execute(
        """INSERT INTO frame_annotations
           (annotation_id, frame_id, media_id, model_name, caption, emotional_core,
            aesthetic_notes_json, why_i_like_it, raw_json, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            annotation_id,
            frame_id,
            media_id,
            VLM_MODEL,
            parsed.get("caption", ""),
            parsed.get("emotional_core", ""),
            json.dumps(parsed.get("aesthetic_notes", []), ensure_ascii=False),
            parsed.get("why_i_like_it", ""),
            json.dumps(parsed, ensure_ascii=False),
            now,
        ),
    )
    conn.execute("UPDATE frames SET vlm_status='done' WHERE frame_id=?", (frame_id,))
    conn.commit()

    return {**parsed, "annotation_id": annotation_id, "frame_id": frame_id, "media_id": media_id}
