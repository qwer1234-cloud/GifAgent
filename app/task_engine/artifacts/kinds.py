"""Stage artifact-kind and input-kind dependency tables."""
from __future__ import annotations

from app.task_engine.models import StageName


# ---------------------------------------------------------------------------
# Dependency rules
# ---------------------------------------------------------------------------

# Maps each stage to the artifact kinds it produces.
# Used by the adapter to know which manifest/file kinds to
# associate with artifacts produced by a given stage.
STAGE_ARTIFACT_KINDS: dict[StageName, tuple[str, ...]] = {
    "discover": ("discover_manifest",),
    "sample": ("sample_manifest", "sample_frames"),
    "vlm": ("vlm_manifest",),
    "refine": ("refine_manifest",),
    "synthesize": ("synthesize_manifest",),
    "rank_dedup": ("rank_dedup_manifest", "rank_candidate_ledger"),
    "gif_clip": ("gif_file", "gif_clip_manifest"),
    "materialize": ("result", "materialize_manifest", "pbf_file"),
}

# Maps each stage to the input keys it requires.
# Each value is a tuple of (artifact_kind, ...) that must exist for that
# stage to run.
STAGE_INPUT_KINDS: dict[StageName, tuple[str, ...]] = {
    "discover": (),
    "sample": ("discover_manifest",),
    "vlm": ("sample_manifest", "sample_frames"),
    "refine": ("vlm_manifest", "discover_manifest"),
    "synthesize": ("refine_manifest",),
    "rank_dedup": ("synthesize_manifest",),
    "gif_clip": ("rank_dedup_manifest",),
    "materialize": ("gif_file", "gif_clip_manifest"),
}

# Quality-enabled rank manifests bind these sidecars into their immutable
# lineage.  They are optional here solely so legacy schema-v1 rank manifests
# remain readable; schema-v2 validation still requires and verifies them.
STAGE_OPTIONAL_INPUT_KINDS: dict[StageName, tuple[str, ...]] = {
    "gif_clip": ("rank_candidate_ledger", "synthesize_manifest"),
}

# Maps input key names to the stage_name that produces them.
_INPUT_PRODUCER: dict[str, StageName] = {
    "discover_manifest": "discover",
    "sample_manifest": "sample",
    "sample_frames": "sample",
    "vlm_manifest": "vlm",
    "refine_manifest": "refine",
    "synthesize_manifest": "synthesize",
    "rank_dedup_manifest": "rank_dedup",
    "rank_candidate_ledger": "rank_dedup",
    "gif_file": "gif_clip",
    "gif_clip_manifest": "gif_clip",
}
