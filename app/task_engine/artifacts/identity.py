"""Artifact identity: stable ids and strict file validation."""
from __future__ import annotations

from pathlib import Path

from app.task_engine.fingerprints import canonical_hash, sha256_file
from app.task_engine.models import ArtifactRef


def make_artifact_id(
    *,
    stage_id: str,
    artifact_kind: str,
    clip_id: str | None,
    normalized_path: str,
) -> str:
    """Produce a stable, collision-resistant artifact_id.

    Uses ``canonical_hash`` over the complete identity tuple so that two
    artifacts for different stages/kinds/clips/paths can never collide.
    """
    return canonical_hash({
        "stage_id": stage_id,
        "artifact_kind": artifact_kind,
        "clip_id": clip_id or "",
        "path": Path(normalized_path).as_posix(),
    })


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_artifact(ref: ArtifactRef) -> bool:
    """Verify that a file exists and matches the recorded size and SHA-256."""
    p = Path(ref.path)
    try:
        if not p.is_file() or p.stat().st_size != ref.size_bytes:
            return False
        return sha256_file(p) == ref.sha256
    except OSError:
        return False


def validate_artifact_strict(ref: ArtifactRef) -> None:
    """Like ``validate_artifact`` but raises on mismatch."""
    p = Path(ref.path)
    if not p.is_file():
        raise FileNotFoundError(f"Artifact file not found: {ref.path}")
    actual_size = p.stat().st_size
    if actual_size != ref.size_bytes:
        raise ValueError(
            f"Artifact size mismatch for {ref.artifact_id} ({ref.path}): "
            f"expected {ref.size_bytes}, got {actual_size}"
        )
    actual_sha = sha256_file(p)
    if actual_sha != ref.sha256:
        raise ValueError(
            f"Artifact SHA-256 mismatch for {ref.artifact_id} ({ref.path}): "
            f"expected {ref.sha256[:16]}..., got {actual_sha[:16]}..."
        )


class ArtifactCollisionError(Exception):
    """Raised when an artifact with the same artifact_id already exists
    but has different field values."""
