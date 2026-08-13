"""Frozen VLM scoring-prompt mode for adaptive extraction.

The live pipeline reads this from the job snapshot via ``extract_config()``.
It must not consult process environment variables at scoring time.
"""

from __future__ import annotations

from typing import Any

SCORE_PROMPT_MODES = ("default", "adult")
SCORE_PROMPT_MODE_ALIASES = {
    "": "default",
    "default": "default",
    "adult": "adult",
    "optimized": "adult",
    "nsfw": "adult",
}


def normalize_score_prompt_mode(value: Any, *, strict: bool = True) -> str:
    """Return the canonical ``default`` or ``adult`` scoring-prompt mode."""
    key = "" if value is None else str(value).strip().lower()
    if key in SCORE_PROMPT_MODE_ALIASES:
        return SCORE_PROMPT_MODE_ALIASES[key]
    if not strict:
        return "default"
    raise ValueError(
        "adaptive.score_prompt_mode must be one of "
        f"{list(SCORE_PROMPT_MODES)} (aliases: optimized, nsfw), got {value!r}"
    )
