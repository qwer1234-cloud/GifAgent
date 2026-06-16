"""VLM/LLM output quality validation — placeholder detection and field checks."""
from app.services.schemas import VALID_EMOTIONS, FrameAnalysis, MediaAnnotation


PLACEHOLDER_SUBSTRINGS = [
    "what you see",
    "one word",
    "one reason",
    "2-3 observations",
    "2-4 qualities",
    "3-5 keywords",
    "why this works as a gif",
    "describe what you actually see",
    "concise description",
    "what you actually observe",
]


def detect_placeholder_text(value: str) -> list[str]:
    """Return list of matching placeholder substrings (empty = clean)."""
    if not value:
        return ["empty"]
    lowered = value.strip().lower()
    return [p for p in PLACEHOLDER_SUBSTRINGS if p in lowered]


def normalize_emotional_core(raw: str | None) -> str:
    """Normalize a raw emotional_core string to a canonical lowercase value."""
    if not raw:
        return "other"
    lowered = raw.strip().lower()
    if lowered in VALID_EMOTIONS:
        return lowered
    # Try splitting by pipe or comma
    parts = [p.strip() for p in lowered.replace("|", ",").split(",")]
    for p in parts:
        if p in VALID_EMOTIONS:
            return p
    return "other"


def validate_frame_analysis(payload: dict) -> tuple[dict | None, list[str]]:
    """Validate a VLM frame analysis dict. Returns (cleaned_dict, error_list).

    If errors are found, the dict is still returned with corrections applied,
    but the caller should NOT write it as 'done'.
    """
    errors: list[str] = []

    # caption
    caption = (payload.get("caption") or "").strip()
    cap_placeholders = detect_placeholder_text(caption)
    if cap_placeholders:
        errors.append(f"caption placeholder: {cap_placeholders}")
        caption = ""
    if caption and len(caption) < 8:
        errors.append(f"caption too short: {len(caption)} chars")

    # emotional_core
    raw_emo = (payload.get("emotional_core") or "").strip()
    # Check for multi-value BEFORE normalization
    if raw_emo and ("|" in raw_emo or "," in raw_emo):
        errors.append(f"emotional_core was multi-value: {raw_emo[:50]}")
    emo = normalize_emotional_core(raw_emo)
    if raw_emo and emo == "other" and raw_emo.lower() not in VALID_EMOTIONS:
        errors.append(f"emotional_core not in valid set: {raw_emo[:50]}")

    # aesthetic_notes
    notes = payload.get("aesthetic_notes", [])
    if not isinstance(notes, list):
        errors.append("aesthetic_notes is not a list")
        notes = []
    clean_notes = []
    for n in notes:
        n_str = str(n).strip()
        note_ph = detect_placeholder_text(n_str)
        if note_ph:
            errors.append(f"aesthetic_note placeholder: {note_ph}")
            continue
        if len(n_str) >= 8:
            clean_notes.append(n_str)
        elif n_str:
            errors.append(f"aesthetic_note too short: {len(n_str)} chars '{n_str[:20]}'")
    if len(clean_notes) < 2:
        errors.append(f"too few valid aesthetic_notes: {len(clean_notes)}")

    # why_i_like_it
    why = (payload.get("why_i_like_it") or "").strip()
    why_ph = detect_placeholder_text(why)
    if why_ph:
        errors.append(f"why_i_like_it placeholder: {why_ph}")
        why = ""
    if why and len(why) < 12:
        errors.append(f"why_i_like_it too short: {len(why)} chars")

    gif_worth = payload.get("gif_worthiness")
    if gif_worth is not None:
        try:
            w = float(gif_worth)
            if w < 0.0 or w > 1.0:
                errors.append(f"gif_worthiness out of range: {w}")
        except (TypeError, ValueError):
            errors.append(f"gif_worthiness not a float: {gif_worth}")

    cleaned = {
        "caption": caption,
        "emotional_core": emo,
        "aesthetic_notes": clean_notes,
        "why_i_like_it": why,
        "gif_worthiness": float(gif_worth) if gif_worth is not None else None,
        "reason": (payload.get("reason") or "").strip(),
        "timestamp": payload.get("timestamp"),
        "frame_name": payload.get("frame_name"),
    }
    return cleaned, errors


def validate_media_annotation(payload: dict) -> tuple[dict | None, list[str]]:
    """Validate a media-level annotation dict."""
    errors: list[str] = []

    summary = (payload.get("summary") or "").strip()
    if detect_placeholder_text(summary):
        errors.append(f"summary placeholder")
        summary = ""

    emo = normalize_emotional_core(payload.get("emotional_core"))
    if emo == "other":
        raw = (payload.get("emotional_core") or "").strip().lower()
        if raw and raw not in VALID_EMOTIONS:
            errors.append(f"emotional_core not canonical: {raw[:50]}")

    notes = payload.get("aesthetic_notes", [])
    if not isinstance(notes, list):
        notes = []

    why = (payload.get("why_i_like_it") or "").strip()
    if detect_placeholder_text(why):
        errors.append("why_i_like_it placeholder")

    tags = payload.get("tags", [])
    if not isinstance(tags, list):
        tags = []

    cleaned = {
        "summary": summary,
        "emotional_core": emo,
        "aesthetic_notes": notes,
        "why_i_like_it": why,
        "tags": tags,
        "scene_type": payload.get("scene_type"),
    }
    return cleaned, errors
