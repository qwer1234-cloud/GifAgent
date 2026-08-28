"""Artifact identity, validation, and input resolution.

Phase A: Stable artifact_id generation, dedup insertion with conflict
detection, and a ``resolve_stage_inputs`` resolver that follows the
dependency rules table.
"""

from app.task_engine.artifacts.identity import (
    ArtifactCollisionError,
    make_artifact_id,
    validate_artifact,
    validate_artifact_strict,
)
from app.task_engine.artifacts.kinds import (
    STAGE_ARTIFACT_KINDS,
    STAGE_INPUT_KINDS,
    STAGE_OPTIONAL_INPUT_KINDS,
)
from app.task_engine.artifacts.manifests import (
    validate_manifest_json,
    validate_materialize_envelope,
)
from app.task_engine.artifacts.resolve import (
    GifClipStatus,
    MaterializeInputs,
    build_materialize_input_envelope,
    get_gif_clip_terminal_statuses,
    resolve_all_gif_clip_artifacts,
    resolve_materialize_inputs,
    resolve_stage_inputs,
    validate_rank_manifest_with_db_lineage,
)
from app.task_engine.artifacts.store import (
    insert_artifact_dedup,
    insert_artifacts_batch,
)

__all__ = [
    "ArtifactCollisionError",
    "GifClipStatus",
    "MaterializeInputs",
    "STAGE_ARTIFACT_KINDS",
    "STAGE_INPUT_KINDS",
    "STAGE_OPTIONAL_INPUT_KINDS",
    "build_materialize_input_envelope",
    "get_gif_clip_terminal_statuses",
    "insert_artifact_dedup",
    "insert_artifacts_batch",
    "make_artifact_id",
    "resolve_all_gif_clip_artifacts",
    "resolve_materialize_inputs",
    "resolve_stage_inputs",
    "validate_artifact",
    "validate_artifact_strict",
    "validate_manifest_json",
    "validate_materialize_envelope",
    "validate_rank_manifest_with_db_lineage",
]
