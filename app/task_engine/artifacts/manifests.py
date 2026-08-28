"""Manifest schema-version machinery and the JSON manifest validator."""
from __future__ import annotations

import json
from pathlib import Path

from app.task_engine.artifacts.quality_schema import (
    _require_v2_field,
    _validate_action_clip_v2,
    _validate_action_guard_v2,
    _validate_gif_export_v2,
    _validate_gif_quality_lineage,
    _validate_quality_assessment,
    _validate_quality_candidate_ledger,
    _validate_quality_summary,
)


# ---------------------------------------------------------------------------
# Manifest validation
# ---------------------------------------------------------------------------

# P1-2: every manifest kind currently speaks schema_version 1.  A per-kind
# ``versions`` override can be added to _MANIFEST_VALIDATORS when a v2 lands.
_SUPPORTED_SCHEMA_VERSIONS: frozenset[int] = frozenset({1})

# P1-2: the materialize input envelope has its own (independent) version.
_MATERIALIZE_ENVELOPE_VERSIONS: frozenset[int] = frozenset({1})


def _supported_versions(specs: dict) -> frozenset[int]:
    """Return the supported schema_version set for a manifest spec."""
    v = specs.get("versions")
    if v:
        return frozenset(v)
    return _SUPPORTED_SCHEMA_VERSIONS


def _validate_schema_version(sv: object, artifact_kind: str, specs: dict) -> None:
    """Validate a single ``schema_version`` value (P1-2).

    Rejects booleans, non-integers, zero/negatives and unknown future
    versions.  The error message always names the artifact kind and the
    supported versions.
    """
    supported = _supported_versions(specs)
    # bool is a subclass of int in Python; reject it explicitly.
    if isinstance(sv, bool) or not isinstance(sv, int):
        raise ValueError(
            f"Manifest {artifact_kind} schema_version must be an integer, "
            f"got {type(sv).__name__} {sv!r}; "
            f"supported versions: {sorted(supported)}"
        )
    if sv <= 0:
        raise ValueError(
            f"Manifest {artifact_kind} schema_version must be a positive "
            f"integer, got {sv}; supported versions: {sorted(supported)}"
        )
    if sv not in supported:
        raise ValueError(
            f"Manifest {artifact_kind} schema_version {sv} is unsupported; "
            f"supported versions: {sorted(supported)}"
        )


def validate_materialize_envelope(envelope: dict) -> None:
    """Validate a materialize input envelope's schema version (P1-2).

    The envelope is built internally by ``build_materialize_input_envelope``
    (currently schema_version 1).  This guard rejects unknown future
    envelope versions defensively, so a mismatched worker/stage pairing
    fails loudly instead of silently mis-parsing the envelope.
    """
    sv = envelope.get("schema_version")
    if isinstance(sv, bool) or not isinstance(sv, int):
        raise ValueError(
            f"materialize envelope schema_version must be an integer, "
            f"got {type(sv).__name__} {sv!r}; "
            f"supported versions: {sorted(_MATERIALIZE_ENVELOPE_VERSIONS)}"
        )
    if sv <= 0:
        raise ValueError(
            f"materialize envelope schema_version must be a positive "
            f"integer, got {sv}; "
            f"supported versions: {sorted(_MATERIALIZE_ENVELOPE_VERSIONS)}"
        )
    if sv not in _MATERIALIZE_ENVELOPE_VERSIONS:
        raise ValueError(
            f"materialize envelope schema_version {sv} is unsupported; "
            f"supported versions: {sorted(_MATERIALIZE_ENVELOPE_VERSIONS)}"
        )


_MANIFEST_VALIDATORS: dict[str, dict] = {
    "discover_manifest": {
        "required_fields": ["schema_version", "stage", "duration_s"],
    },
    "sample_manifest": {
        "required_fields": ["schema_version", "stage", "frame_count", "timestamps", "frame_paths"],
    },
    "vlm_manifest": {
        "required_fields": ["schema_version", "stage", "scored_count", "frames"],
    },
    "refine_manifest": {
        "required_fields": ["schema_version", "stage", "scored_count", "frames"],
    },
    "synthesize_manifest": {
        "required_fields": ["schema_version", "stage", "clips"],
    },
    "rank_dedup_manifest": {
        "versions": [1, 2],
        "required_fields": ["schema_version", "stage", "clips", "clip_count"],
    },
    "gif_clip_manifest": {
        "versions": [1, 2],
        "required_fields": ["schema_version", "stage", "clip_id", "gif_path"],
    },
    "gif_file": {
        "required_fields": [],  # binary file, no JSON schema
    },
    "result": {
        "required_fields": ["schema_version", "stage"],
    },
    "materialize_manifest": {
        "required_fields": ["schema_version", "stage", "gif_count"],
    },
}


def validate_manifest_json(
    raw_bytes: bytes,
    artifact_kind: str,
    expected_stage: StageName | None = None,
    expected_clip_id: str | None = None,
    *,
    candidate_ledger_bytes: bytes | None = None,
    candidate_ledger_ref: dict | None = None,
    upstream_artifact_ref: dict | None = None,
    require_external_quality_ledger: bool = False,
) -> dict:
    """Validate a manifest JSON artifact and return the parsed dict.

    Raises ``ValueError`` on schema violations.
    """
    if not raw_bytes:
        raise ValueError(f"Empty manifest for {artifact_kind}")

    try:
        data = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid JSON in {artifact_kind}: {exc}") from exc

    specs = _MANIFEST_VALIDATORS.get(artifact_kind)
    if specs is None:
        raise ValueError(f"Unknown artifact_kind: {artifact_kind}")

    for field in specs["required_fields"]:
        if field not in data:
            raise ValueError(
                f"Manifest {artifact_kind} missing required field: {field}"
            )

    # P1-2: strict schema_version validation.  schema_version must be a
    # positive int in the supported set.  Booleans (``isinstance(True, int)``
    # is True in Python), strings, zero, negatives and unknown future
    # versions are rejected.  The message names the artifact kind and the
    # supported versions so failures are diagnosable.
    if "schema_version" in data:
        _validate_schema_version(data["schema_version"], artifact_kind, specs)

    if expected_stage is not None and data.get("stage") != expected_stage:
        raise ValueError(
            f"Manifest {artifact_kind} stage mismatch: "
            f"expected {expected_stage}, got {data.get('stage')}"
        )

    if expected_clip_id is not None and data.get("clip_id") != expected_clip_id:
        raise ValueError(
            f"Manifest {artifact_kind} clip_id mismatch: "
            f"expected {expected_clip_id}, got {data.get('clip_id')}"
        )

    # For rank_dedup: verify clip_count == len(clips)
    if artifact_kind == "rank_dedup_manifest":
        clips = data.get("clips", [])
        if not isinstance(clips, list):
            raise ValueError("rank_dedup_manifest clips must be an array")
        clip_count = data.get("clip_count", len(clips))
        if (
            isinstance(clip_count, bool)
            or not isinstance(clip_count, int)
            or clip_count < 0
        ):
            raise ValueError(
                "rank_dedup_manifest clip_count must be a non-negative integer"
            )
        if clip_count != len(clips):
            raise ValueError(
                f"rank_dedup_manifest clip_count ({clip_count}) != "
                f"len(clips) ({len(clips)})"
            )
        for index, clip in enumerate(clips):
            if not isinstance(clip, dict):
                raise ValueError(
                    f"rank_dedup_manifest clips[{index}] must be an object"
                )
        # Verify clip_ids are non-empty strings and unique.
        clip_ids = [c.get("clip_id") for c in clips]
        if any(not isinstance(cid, str) or not cid.strip() for cid in clip_ids):
            raise ValueError(
                "rank_dedup_manifest has an empty clip_id or non-string "
                "clip_id; clip_id must be a non-empty string"
            )
        if len(set(clip_ids)) != len(clip_ids):
            raise ValueError("rank_dedup_manifest has duplicate clip_ids")
        if data.get("schema_version") == 2:
            for index, clip in enumerate(clips):
                _validate_action_clip_v2(
                    clip,
                    context=f"rank_dedup_manifest clips[{index}]",
                )
            action_guard = _require_v2_field(
                data, "action_guard", context="rank_dedup_manifest"
            )
            _validate_action_guard_v2(action_guard)
            root_version = action_guard["action_analysis_version"]
            if any(
                clip["action_analysis_version"] != root_version
                for clip in clips
            ):
                raise ValueError(
                    "rank_dedup_manifest clip action_analysis_version "
                    "must match action_guard action_analysis_version"
                )
            if "quality_moe" in data:
                authoritative_candidates = _validate_quality_candidate_ledger(
                    data["quality_moe"],
                    candidate_ledger_bytes=candidate_ledger_bytes,
                    candidate_ledger_ref=candidate_ledger_ref,
                    upstream_artifact_ref=upstream_artifact_ref,
                    require_external=require_external_quality_ledger,
                )
                if data["quality_moe"].get("enabled") is True:
                    for index, clip in enumerate(clips):
                        assessment = _require_v2_field(
                            clip,
                            "quality_assessment",
                            context=f"rank_dedup_manifest clips[{index}]",
                        )
                        _validate_quality_assessment(
                            assessment,
                            context=f"rank_dedup_manifest clips[{index}] quality_assessment",
                        )
                _validate_quality_summary(
                    data["quality_moe"],
                    clips=clips,
                    authoritative_candidates=authoritative_candidates,
                )

    if (
        artifact_kind == "gif_clip_manifest"
        and data.get("schema_version") == 2
    ):
        _validate_action_clip_v2(data, context="gif_clip_manifest")
        _validate_gif_export_v2(data)
        _validate_gif_quality_lineage(data)

    return data
