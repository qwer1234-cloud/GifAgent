"""Versioned prompt-template registry for the W1 enrichment passes.

Prompt templates live at ``scripts/token_sprint/prompts/<pass_id>/<version>.md``
with a YAML front matter pinning ``pass_id`` and ``prompt_version``. Templates
are immutable: editing a template in place is forbidden; a new ``prompt_version``
file plus a fresh QC gate is required. The SHA-256 of the exact template file is
recorded in QC summaries and the approved enrichment manifest so that every
enrichment artifact is bound to the prompt that produced it.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

#: Passes with templates in this package (p4/p5 are Stage C skeletons).
KNOWN_PASSES = ("p1a", "p1b", "p2", "p3", "p4", "p5")


def prompt_path(
    pass_id: str,
    prompt_version: str,
    *,
    base_dir: Path | None = None,
) -> Path:
    """Path of the template file for ``(pass_id, prompt_version)``."""
    return (base_dir or PROMPTS_DIR) / pass_id / f"{prompt_version}.md"


def prompt_sha256(
    pass_id: str,
    prompt_version: str,
    *,
    base_dir: Path | None = None,
) -> str:
    """SHA-256 of the template file for ``(pass_id, prompt_version)``.

    Raises ``FileNotFoundError`` when the template does not exist.
    """
    return hashlib.sha256(prompt_path(pass_id, prompt_version, base_dir=base_dir).read_bytes()).hexdigest()
