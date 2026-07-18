from __future__ import annotations

import hashlib
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from app.task_engine.fingerprints import canonical_hash, canonical_json

_REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Provenance:
    git_commit: str
    config_hash: str
    model_versions: dict[str, str]
    prompt_hashes: dict[str, str]
    stage_versions: dict[str, str]


def current_provenance(
    config: dict,
    stage_versions: dict[str, str],
    *,
    prompts: dict[str, str] | None = None,
) -> Provenance:
    return Provenance(
        git_commit=_git_commit(),
        config_hash=canonical_hash(config),
        model_versions=_extract_model_versions(config),
        prompt_hashes=_extract_prompt_hashes(config, prompts),
        stage_versions=dict(stage_versions),
    )


def provenance_to_json(provenance: Provenance) -> str:
    return canonical_json(asdict(provenance))


def _git_commit() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10,
            cwd=_REPO_ROOT,
        )
        commit = result.stdout.strip()
        return commit if result.returncode == 0 and commit else "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def hash_prompt_text(text: str) -> str:
    """Return the SHA-256 hex digest of *text*."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _extract_model_versions(config: dict) -> dict[str, str]:
    """Extract model pins; assumes a two-level config (section -> key -> value)."""
    versions: dict[str, str] = {}
    for section, values in config.items():
        if not isinstance(values, dict):
            continue
        for key, value in values.items():
            if isinstance(value, str) and (key == "model" or key.endswith("_model")):
                versions[f"{section}.{key}"] = value
    return versions


def _extract_prompt_hashes(
    config: dict, prompts: dict[str, str] | None = None
) -> dict[str, str]:
    if prompts is None:
        prompts = config.get("prompts")
    if not isinstance(prompts, dict):
        return {}
    return {
        name: hashlib.sha256(text.encode("utf-8")).hexdigest()
        for name, text in prompts.items()
        if isinstance(text, str)
    }
