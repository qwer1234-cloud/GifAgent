"""LLM Synthesis Service — synthesize frame analyses into a cohesive media-level annotation."""
import json
import uuid
import time
from datetime import datetime, timezone
from typing import List

from app.db import get_connection
from app.services.json_guard import parse_json_response
from app.services.llm_client import generate_llm_text, llm_model_name
from app.services.quality import validate_media_annotation

LLM_MODEL = llm_model_name()


def _parse_response(text: str) -> dict:
    """Thin wrapper around json_guard for backward compat."""
    result = parse_json_response(text)
    if result.ok and result.data:
        return result.data
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
            response_text = generate_llm_text(prompt, temperature=0.3, timeout=120)
            if not response_text or not response_text.strip():
                raise ValueError("Empty response from LLM")

            parsed = _parse_response(response_text)
            if parsed.get("_parse_error"):
                raw = parsed.get("_raw", "")
                if attempt < 2 and raw:
                    print(f"[WARN] JSON parse failed for media {media_id} (attempt {attempt+1}/3): {raw[:100]}")
                    prompt = prompt + "\n\nCRITICAL: Your last response was not valid JSON. Output ONLY the JSON object, no other text."
                    continue
                print(f"[WARN] JSON parse failed for media {media_id} after 3 attempts: {raw[:200]}")

            # Quality validation
            cleaned, q_errors = validate_media_annotation(parsed)
            if q_errors and attempt < 2:
                print(f"[WARN] Quality check failed for media {media_id}: {q_errors}")
                prompt = prompt + f"\n\nIssues with your response: {', '.join(q_errors)}. Fix these and output valid JSON."
                continue
            elif q_errors:
                print(f"[WARN] Quality check failed for media {media_id} after 3 attempts: {q_errors}")
            parsed = {**parsed, **cleaned}
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
