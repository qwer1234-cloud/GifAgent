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
SCORE_SCHEMA_MODES = ("legacy", "two_tier")
SCORE_SCHEMA_MODE_ALIASES = {
    "": "legacy",
    "legacy": "legacy",
    "full": "legacy",
    "two_tier": "two_tier",
    "score": "two_tier",
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


def normalize_score_schema_mode(value: Any, *, strict: bool = True) -> str:
    """Return the canonical ``legacy`` or ``two_tier`` scoring schema."""
    key = "" if value is None else str(value).strip().lower()
    if key in SCORE_SCHEMA_MODE_ALIASES:
        return SCORE_SCHEMA_MODE_ALIASES[key]
    if not strict:
        return "legacy"
    raise ValueError(
        "adaptive.score_schema_mode must be one of "
        f"{list(SCORE_SCHEMA_MODES)}, got {value!r}"
    )
