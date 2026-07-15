"""Preference-aware ranking helpers for adaptive GIF export."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def rank_clips_for_export(
    clips: list[dict[str, Any]],
    score_clip: Callable[[dict[str, Any]], dict[str, Any] | None],
) -> list[dict[str, Any]]:
    """Score all candidates before sorting, falling back to VLM worthiness."""
    for clip in clips:
        base_score = float(clip["gif_worthiness"])
        try:
            result = score_clip(clip) or {}
        except Exception:
            result = {}
        final_score = result.get("final_score", base_score)
        if final_score is None:
            final_score = base_score
        clip.update(result)
        clip["final_score"] = float(final_score)
    return sorted(clips, key=lambda clip: clip["final_score"], reverse=True)
