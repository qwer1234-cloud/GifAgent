from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from app.task_engine.models import ArtifactRef, StageName

# P0-1/P1-2 (fifth-review §3/§6): the only legal terminal outcomes for a
# stage that completed without raising.  ``succeeded`` is the default; a
# stage that produced partial output but needs human review (e.g.
# materialize with unrecoverable publish conflicts) returns
# ``needs_attention`` so the worker marks the stage accordingly while still
# persisting the produced artifacts.  Any other value is a contract
# violation and must NEVER silently map to ``succeeded``.
StageOutcome = Literal["succeeded", "needs_attention"]
_VALID_OUTCOMES: frozenset[str] = frozenset({"succeeded", "needs_attention"})


def normalize_outcome(value: object) -> StageOutcome:
    """Coerce a raw outcome value to the strict Literal.

    Accepts the two legal values (str).  ``None`` and a missing value
    map to ``"succeeded"`` for backward compatibility with result files
    written before outcome existed.  Any other value raises ``ValueError``
    so a typo or future unknown value can never silently succeed.
    """
    if value is None:
        return "succeeded"
    if value in _VALID_OUTCOMES:
        return value  # type: ignore[return-value]
    raise ValueError(
        f"Unknown stage outcome {value!r}; "
        f"expected one of {sorted(_VALID_OUTCOMES)}"
    )


@dataclass(frozen=True)
class StageResult:
    output_key: str
    artifacts: tuple[ArtifactRef, ...]
    metrics: dict[str, int | float | str]
    # P0-2: explicit terminal outcome for stages that complete without
    # raising but still need human attention (e.g. materialize that
    # published what it could but had unrecoverable publish conflicts).
    # ``"succeeded"`` (default) -> the worker marks the stage succeeded.
    # ``"needs_attention"`` -> the worker marks the stage needs_attention
    # while still persisting the produced artifacts (partial output).
    outcome: StageOutcome = "succeeded"


@dataclass(frozen=True)
class StageContext:
    job_id: str
    video_id: str
    video_path: Path
    clip_id: str | None
    input_key: str
    work_dir: Path
    config: dict
    stage_id: str = ""
    inputs: dict[str, tuple[ArtifactRef, ...]] | None = None


class StageAdapter(Protocol):
    """Protocol that every stage adapter must implement."""

    name: StageName
    version: str

    def run(self, context: StageContext) -> StageResult: ...
