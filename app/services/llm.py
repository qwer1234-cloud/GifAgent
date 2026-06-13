"""LLM Synthesis Service — synthesize frame analyses into a cohesive media-level annotation."""
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Optional, List

import httpx

from app.db import get_connection
from app.config import get

LLM_BASE = get("llm.base_url", "http://localhost:11434")
LLM_MODEL = get("llm.model")


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


def build_synthesis_prompt(film_name: str, frame_analyses: List[dict]) -> str:
    analyses_text = "\n\n".join(
        f"Frame {i+1}:\n"
        f"  Caption: {fa.get('caption', '')}\n"
        f"  Emotional core: {fa.get('emotional_core', '')}\n"
        f"  Aesthetic notes: {fa.get('aesthetic_notes', [])}\n"
        f"  Why compelling: {fa.get('why_i_like_it', '')}"
        for i, fa in enumerate(frame_analyses)
    )

    return f"""You are an AI that synthesizes frame-by-frame analysis into a cohesive movie scene annotation.

Film title hint (from filename): {film_name}

Given multiple frame analyses from the same GIF, output a single JSON only, no markdown:
{{{{
  "summary": "one cohesive sentence describing the full moment captured in this GIF",
  "emotional_core": "the dominant emotion carried through the scene",
  "aesthetic_notes": ["consolidated list of the most significant cinematic qualities across all frames"],
  "why_i_like_it": "an eloquent, personal reason this moment is worth saving - what makes it cinematically special",
  "tags": ["film_title", "character_name", "actor_name", "scene_type", "notable_keywords"],
  "scene_type": "close-up | dialogue | action | transition | reaction | establishing | montage | other"
}}}}

Frame analyses:
{analyses_text}"""


def synthesize_media_annotation(media_id: str, vlm_results: List[dict]) -> dict:
    """Take VLM frame analyses and synthesize a cohesive media-level annotation using the LLM model."""
    conn = get_connection()
    media = conn.execute("SELECT film FROM media WHERE media_id=?", (media_id,)).fetchone()
    if not media:
        raise ValueError(f"media_id {media_id} not found")

    film_name = media["film"] or "Unknown"

    # Extract frame annotations from vlm_results
    frame_analyses: List[dict] = []
    for r in vlm_results:
        ann = r.get("annotation", {})
        frame_analyses.append(ann)

    if not frame_analyses:
        return {}

    prompt = build_synthesis_prompt(film_name, frame_analyses)

    resp = httpx.post(
        f"{LLM_BASE}/api/generate",
        json={"model": LLM_MODEL, "prompt": prompt, "stream": False},
        timeout=120,
    )
    resp.raise_for_status()
    data = resp.json()
    response_text = data.get("response", "")

    parsed = _parse_json_response(response_text)
    if parsed.get("_parse_error"):
        print(f"[WARN] JSON parse failed for media {media_id}: {parsed.get('_raw', '')[:200]}")

    annotation_id = f"ann_{uuid.uuid4().hex[:12]}"
    now = datetime.now(timezone.utc).isoformat()

    conn.execute(
        """INSERT INTO annotations
           (annotation_id, media_id, model_name, summary, emotional_core,
            aesthetic_notes_json, why_i_like_it, tags_json, scene_type, raw_json, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (
            annotation_id,
            media_id,
            LLM_MODEL,
            parsed.get("summary", ""),
            parsed.get("emotional_core", ""),
            json.dumps(parsed.get("aesthetic_notes", []), ensure_ascii=False),
            parsed.get("why_i_like_it", ""),
            json.dumps(parsed.get("tags", []), ensure_ascii=False),
            parsed.get("scene_type", ""),
            json.dumps(parsed, ensure_ascii=False),
            now,
        ),
    )
    conn.commit()

    return {**parsed, "annotation_id": annotation_id, "media_id": media_id}
