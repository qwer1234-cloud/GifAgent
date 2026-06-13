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
    # Strip Qwen-style think tags
    if "</think>" in text:
        text = text.split("</think>")[-1].strip()
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    # Strip markdown code fences
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
        f"Caption: {fa.get('caption', '')}\n"
        f"Emotional core: {fa.get('emotional_core', '')}\n"
        f"Aesthetic notes: {fa.get('aesthetic_notes', [])}\n"
        f"Why compelling: {fa.get('why_i_like_it', '')}"
        for i, fa in enumerate(frame_analyses)
    )

    return (
        "You are an AI that synthesizes frame-by-frame film analyses into a cohesive annotation.\n\n"
        f"Film: {film_name}\n\n"
        "IMPORTANT: You MUST respond with ONLY a valid JSON object. Start with {{\"summary\". No markdown, no preamble, no other text.\n\n"
        "{\n"
        '  "summary": "one cohesive sentence describing the visual style across these frames",\n'
        '  "emotional_core": "one dominant emotion",\n'
        '  "aesthetic_notes": ["2-4 cinematographic qualities"],\n'
        '  "why_i_like_it": "one personal, cinephile-level sentence",\n'
        '  "tags": ["3-5 keywords"],\n'
        '  "scene_type": "close-up | dialogue | action | transition | reaction | establishing | montage | other"\n'
        "}\n\n"
        "Frame analyses:\n" + analyses_text
    )


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

    for attempt in range(3):
        try:
            resp = httpx.post(
                f"{LLM_BASE}/api/generate",
                json={"model": LLM_MODEL, "prompt": prompt, "stream": False, "options": {"temperature": 0.3, "num_think": 0}},
                timeout=120,
            )
            resp.raise_for_status()
            data = resp.json()
            response_text = data.get("response", "")
            # Fallback: deepseek-v4-flash models put content in "thinking" field
            if not response_text or not response_text.strip():
                response_text = data.get("thinking", "")
            if not response_text or not response_text.strip():
                raise ValueError("Empty response from LLM")

            parsed = _parse_json_response(response_text)
            if parsed.get("_parse_error"):
                raw = parsed.get("_raw", "")
                if attempt < 2 and raw:
                    print(f"[WARN] JSON parse failed for media {media_id} (attempt {attempt+1}/3): {raw[:100]}")
                    prompt = prompt + "\n\nCRITICAL: Your last response was not valid JSON. Output ONLY the JSON object, no other text."
                    continue
                print(f"[WARN] JSON parse failed for media {media_id} after 3 attempts: {raw[:200]}")
            break
        except Exception as e:
            if attempt < 2:
                print(f"[WARN] LLM call failed for media {media_id} (attempt {attempt+1}/3): {e}")
                time.sleep(5)
            else:
                raise

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
