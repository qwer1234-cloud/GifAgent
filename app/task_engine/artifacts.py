"""Artifact identity, validation, and input resolution.

Phase A: Stable artifact_id generation, dedup insertion with conflict
detection, and a ``resolve_stage_inputs`` resolver that follows the
dependency rules table.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from app.task_engine.fingerprints import canonical_hash, sha256_file
from app.task_engine.models import ArtifactRef, StageName

# ---------------------------------------------------------------------------
# Artifact identity
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Dedup insertion with conflict detection
# ---------------------------------------------------------------------------


class ArtifactCollisionError(Exception):
    """Raised when an artifact with the same artifact_id already exists
    but has different field values."""


def insert_artifact_dedup(
    conn: sqlite3.Connection,
    ref: ArtifactRef,
) -> bool:
    """Insert an artifact, or verify it matches the existing record.

    Returns ``True`` if a new row was inserted.
    Returns ``False`` if an identical row already existed (idempotent).
    Raises ``ArtifactCollisionError`` if the same artifact_id exists with
    different field values.

    This function assumes it is called within an existing transaction
    (it does NOT commit).
    """
    existing = conn.execute(
        "SELECT * FROM task_artifacts WHERE artifact_id=?",
        (ref.artifact_id,),
    ).fetchone()

    if existing is not None:
        # Verify all identity fields match exactly.
        for field in (
            "job_id", "video_id", "stage_name", "clip_id",
            "path", "sha256", "size_bytes",
        ):
            expected = getattr(ref, field)
            actual = existing[field]
            if field == "clip_id":
                expected = expected or ""  # NULL vs '' equivalence
                actual = actual or ""
            if str(expected) != str(actual):
                raise ArtifactCollisionError(
                    f"Artifact collision: artifact_id={ref.artifact_id!r} "
                    f"exists with {field}={actual!r}, "
                    f"new value={expected!r}"
                )
        return False  # idempotent

    from datetime import datetime, timezone

    conn.execute(
        """INSERT INTO task_artifacts
           (artifact_id, job_id, video_id, stage_name, clip_id,
            path, sha256, size_bytes, provenance_json, created_at,
            stage_id, artifact_kind)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            ref.artifact_id,
            ref.job_id,
            ref.video_id,
            ref.stage_name,
            ref.clip_id,
            str(ref.path),
            ref.sha256,
            ref.size_bytes,
            ref.provenance_json,
            datetime.now(timezone.utc).isoformat(),
            ref.stage_id,
            ref.artifact_kind,
        ),
    )
    return True


def insert_artifacts_batch(
    conn: sqlite3.Connection,
    artifacts: tuple[ArtifactRef, ...],
) -> int:
    """Insert a batch of artifacts with dedup validation.

    Returns the number of newly inserted rows.

    All artifact files must pass ``validate_artifact_strict`` first,
    or this raises.
    """
    count = 0
    for ref in artifacts:
        validate_artifact_strict(ref)
        if insert_artifact_dedup(conn, ref):
            count += 1
    return count


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
    "rank_dedup": ("rank_dedup_manifest",),
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

# Maps input key names to the stage_name that produces them.
_INPUT_PRODUCER: dict[str, StageName] = {
    "discover_manifest": "discover",
    "sample_manifest": "sample",
    "sample_frames": "sample",
    "vlm_manifest": "vlm",
    "refine_manifest": "refine",
    "synthesize_manifest": "synthesize",
    "rank_dedup_manifest": "rank_dedup",
    "gif_file": "gif_clip",
    "gif_clip_manifest": "gif_clip",
}


def _fetch_artifacts_for_stage(
    conn: sqlite3.Connection,
    video_id: str,
    producer_stage_name: str,
    artifact_kind: str,
    clip_id: str | None = None,
) -> list[ArtifactRef]:
    """Fetch artifacts of a given kind produced by a specific stage.

    Only returns artifacts from stages whose status is ``'succeeded'``.
    Failed, cancelled, or in-progress stage artifacts are excluded.

    When ``clip_id`` is provided, only artifacts matching that clip are
    returned.
    """
    if clip_id is not None:
        rows = conn.execute(
            """SELECT a.* FROM task_artifacts a
               JOIN task_stages s ON a.stage_id = s.stage_id
               WHERE a.video_id=? AND a.stage_name=? AND a.artifact_kind=?
                 AND a.clip_id=? AND s.status='succeeded'
               ORDER BY a.created_at ASC""",
            (video_id, producer_stage_name, artifact_kind, clip_id),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT a.* FROM task_artifacts a
               JOIN task_stages s ON a.stage_id = s.stage_id
               WHERE a.video_id=? AND a.stage_name=? AND a.artifact_kind=?
                 AND s.status='succeeded'
               ORDER BY a.created_at ASC""",
            (video_id, producer_stage_name, artifact_kind),
        ).fetchall()

    results: list[ArtifactRef] = []
    for row in rows:
        ref = ArtifactRef(
            artifact_id=row["artifact_id"],
            job_id=row["job_id"],
            video_id=row["video_id"],
            stage_name=row["stage_name"],
            clip_id=row["clip_id"],
            path=row["path"],
            sha256=row["sha256"],
            size_bytes=row["size_bytes"],
            provenance_json=row["provenance_json"],
            stage_id=row["stage_id"] or "",
            artifact_kind=row["artifact_kind"],
        )
        results.append(ref)
    return results


def resolve_stage_inputs(
    conn: sqlite3.Connection,
    video_id: str,
    stage_name: StageName,
    clip_id: str | None = None,
) -> dict[str, tuple[ArtifactRef, ...]]:
    """Resolve all inputs a stage needs from the task_artifacts table.

    Returns a dict mapping input key names (e.g. ``"discover_manifest"``)
    to tuples of ``ArtifactRef`` objects.  For the ``gif_clip`` stage,
    only artifacts matching the current ``clip_id`` are returned for
    clip-specific kinds (like ``gif_file``).

    Non-clip-specific artifacts (like ``rank_dedup_manifest``) are always
    returned regardless of clip_id.

    All returned artifacts are re-validated (file existence, size, SHA-256).
    If any artifact fails validation, a ``FileNotFoundError`` or
    ``ValueError`` is raised.
    """
    kinds = STAGE_INPUT_KINDS.get(stage_name, ())
    if not kinds:
        return {}

    # Kinds that are clip-specific (should filter by clip_id).
    _CLIP_KINDS = frozenset({"gif_file", "gif_clip_manifest", "sample_frames"})

    result: dict[str, tuple[ArtifactRef, ...]] = {}
    for kind in kinds:
        producer = _INPUT_PRODUCER.get(kind)
        if producer is None:
            continue
        # Only filter by clip_id for clip-specific artifact kinds.
        filter_clip = clip_id if kind in _CLIP_KINDS else None
        refs = _fetch_artifacts_for_stage(
            conn, video_id, producer, kind, clip_id=filter_clip,
        )
        for ref in refs:
            validate_artifact_strict(ref)
        if not refs:
            raise FileNotFoundError(
                f"No artifact of kind {kind!r} found for video {video_id!r}"
            )
        result[kind] = tuple(refs)
    return result


def resolve_all_gif_clip_artifacts(
    conn: sqlite3.Connection,
    video_id: str,
) -> dict[str, list[ArtifactRef]]:
    """Return all gif_file and gif_clip_manifest artifacts for a video,
    grouped by clip_id, for use by the materialize stage.

    Only returns artifacts from gif_clip stages whose status is
    ``'succeeded'``.  Failed or cancelled gif_clip artifacts are excluded.
    """
    rows = conn.execute(
        """SELECT a.* FROM task_artifacts a
           JOIN task_stages s ON a.stage_id = s.stage_id
           WHERE a.video_id=? AND a.stage_name='gif_clip'
             AND a.artifact_kind IN ('gif_file', 'gif_clip_manifest')
             AND s.status='succeeded'
           ORDER BY a.created_at ASC""",
        (video_id,),
    ).fetchall()

    by_clip: dict[str, list[ArtifactRef]] = {}
    for row in rows:
        ref = ArtifactRef(
            artifact_id=row["artifact_id"],
            job_id=row["job_id"],
            video_id=row["video_id"],
            stage_name=row["stage_name"],
            clip_id=row["clip_id"],
            path=row["path"],
            sha256=row["sha256"],
            size_bytes=row["size_bytes"],
            provenance_json=row["provenance_json"],
            stage_id=row["stage_id"] or "",
            artifact_kind=row["artifact_kind"],
        )
        cid = ref.clip_id or "__no_clip__"
        by_clip.setdefault(cid, []).append(ref)
    return by_clip


def get_gif_clip_terminal_statuses(
    conn: sqlite3.Connection,
    video_id: str,
) -> list[dict]:
    """Return terminal status summaries for ALL gif_clip stages of a video.

    Used by the worker to pass comprehensive status info to the materialize
    stage so it can report succeeded/failed/cancelled clips.

    Each entry is a dict with ``clip_id`` and ``status``.  Only includes
    stages that have reached a terminal state (succeeded / failed /
    cancelled / needs_attention).  Non-terminal stages (pending / leased /
    running) are excluded — the materialize stage is only created after
    all gif_clip stages are terminal.
    """
    rows = conn.execute(
        """SELECT clip_id, status FROM task_stages
           WHERE video_id=? AND stage_name='gif_clip'
             AND status IN ('succeeded','failed','cancelled','needs_attention')
           ORDER BY created_at ASC""",
        (video_id,),
    ).fetchall()
    return [
        {"clip_id": r["clip_id"] or "", "status": r["status"]}
        for r in rows
    ]


@dataclass(frozen=True)
class GifClipStatus:
    """Terminal status summary for a single gif_clip stage.

    Carried in the materialize input envelope so the materialize stage can
    report succeeded / needs_attention / cancelled / failed clips without
    re-deriving status from artifact rows.
    """

    stage_id: str
    clip_id: str
    status: str
    attempt_count: int
    last_error: str | None


@dataclass(frozen=True)
class MaterializeInputs:
    """Stage-driven materialize inputs returned by ``resolve_materialize_inputs``.

    ``artifacts`` only contains entries for SUCCEEDED gif_clip stages (each
    validated to have exactly one ``gif_file`` + one ``gif_clip_manifest``).
    ``stage_statuses`` carries EVERY terminal gif_clip stage so the envelope
    can report partial failures.  ``zero_clip`` is True only when no gif_clip
    stages exist at all (explicit zero-clip semantics), never inferred from
    "no succeeded artifacts found".
    """

    artifacts: dict[str, tuple[ArtifactRef, ...]]
    stage_statuses: tuple[GifClipStatus, ...]
    zero_clip: bool


# gif_clip terminal statuses aggregated by the resolver.
_GIF_CLIP_TERMINAL = ("succeeded", "failed", "cancelled", "needs_attention")


def _assert_zero_clip_proven(conn: sqlite3.Connection, video_id: str) -> None:
    """P1-1 (fifth-review §5): prove a zero-clip materialize came from a
    real rank_dedup manifest that declared ``clip_count=0``.

    Without this check, a lost gif_clip fan-out (e.g. partial migration or
    manual recovery) would silently look like a zero-clip success.  Raises
    ``ValueError`` if no rank_dedup manifest exists or if its declared
    clip_count is non-zero.
    """
    row = conn.execute(
        """SELECT a.*, s.status AS stage_status, s.stage_name AS ref_stage_name
           FROM task_artifacts a
           JOIN task_stages s ON a.stage_id = s.stage_id
           WHERE a.video_id=? AND a.artifact_kind='rank_dedup_manifest'
             AND s.status='succeeded'
           ORDER BY a.created_at DESC LIMIT 1""",
        (video_id,),
    ).fetchone()
    if row is None:
        raise ValueError(
            f"Cannot prove zero-clip for video {video_id!r}: no succeeded "
            f"rank_dedup_manifest artifact found"
        )
    if row["ref_stage_name"] != "rank_dedup":
        raise ValueError(
            f"rank_dedup_manifest for video {video_id!r} belongs to "
            f"stage {row['ref_stage_name']!r}, not 'rank_dedup'"
        )

    # Build ArtifactRef and strictly validate file integrity.
    ref = ArtifactRef(
        artifact_id=row["artifact_id"],
        job_id=row["job_id"],
        video_id=row["video_id"],
        stage_name=row["stage_name"],
        clip_id=row["clip_id"],
        path=row["path"],
        sha256=row["sha256"],
        size_bytes=row["size_bytes"],
        provenance_json=row["provenance_json"],
        stage_id=row["stage_id"] or "",
        artifact_kind=row["artifact_kind"],
    )
    validate_artifact_strict(ref)

    # Validate the manifest JSON schema and stage.
    manifest_path = Path(ref.path)
    raw = manifest_path.read_bytes()
    manifest = validate_manifest_json(
        raw, "rank_dedup_manifest", expected_stage="rank_dedup",
    )

    declared = manifest.get("clip_count")
    clips = manifest.get("clips", [])
    if declared is None:
        declared = len(clips)
    if declared != 0 or clips:
        raise ValueError(
            f"rank_dedup_manifest for video {video_id!r} declares "
            f"clip_count={declared} (len(clips)={len(clips)}); cannot treat "
            f"as zero-clip while gif_clip fan-out produced no stages"
        )


def resolve_materialize_inputs(
    conn: sqlite3.Connection,
    video_id: str,
) -> MaterializeInputs:
    """Resolve all inputs needed by the materialize stage (stage-driven).

    The query starts from SUCCEEDED ``gif_clip`` *stages*, not from
    ``task_artifacts``.  This is the critical P0-1 fix: a succeeded clip
    that is missing its artifacts cannot hide behind an empty JOIN result.

    For every succeeded gif_clip stage the resolver requires:

    * exactly one ``gif_file`` artifact whose ``stage_id`` matches,
    * exactly one ``gif_clip_manifest`` artifact whose ``stage_id`` matches,
    * both artifacts' ``clip_id`` equal to the stage's ``clip_id``,
    * both files exist with matching size and SHA-256,
    * the manifest's ``clip_id`` / ``gif_path`` / ``sha256`` agree with
      the ``gif_file``.

    Any missing or duplicate artifact raises ``ValueError`` - the resolver
    never silently returns a partial set.  ``failed`` / ``cancelled`` /
    ``needs_attention`` gif_clip stages are aggregated into
    ``stage_statuses`` but do not require artifacts.

    ``zero_clip`` is True only when NO gif_clip stages exist at all (the
    rank_dedup manifest declared zero clips and materialize was created
    directly).  It is never inferred from "no succeeded artifacts found".

    Raises ``FileNotFoundError`` if an artifact file is missing.
    Raises ``ValueError`` if a succeeded clip's artifacts are incomplete,
    duplicated, or inconsistent.
    """
    # P1-1 (fifth-review §5): scan ALL gif_clip stages first (not only
    # terminal ones) so a non-terminal stage (pending / leased / running /
    # retry_wait) can never masquerade as a false zero-clip success.
    all_clip_rows = conn.execute(
        """SELECT stage_id, clip_id, status, attempt_count, last_error_json
           FROM task_stages
           WHERE video_id=? AND stage_name='gif_clip'
           ORDER BY created_at ASC, stage_id ASC""",
        (video_id,),
    ).fetchall()

    # No gif_clip stages at all -> the explicit zero-clip path.  But the
    # resolver must PROVE this came from a rank_dedup manifest that
    # declared clip_count=0; otherwise a lost fan-out would silently
    # become a false zero-clip success.
    if not all_clip_rows:
        _assert_zero_clip_proven(conn, video_id)
        return MaterializeInputs(
            artifacts={}, stage_statuses=(), zero_clip=True,
        )

    # Reject any non-terminal gif_clip: fan-out is not finished and the
    # materialize stage must not resolve inputs yet.
    non_terminal = [r for r in all_clip_rows
                    if r["status"] not in _GIF_CLIP_TERMINAL]
    if non_terminal:
        offenders = ", ".join(
            f"{r['stage_id']}={r['status']}" for r in non_terminal
        )
        raise ValueError(
            f"Cannot resolve materialize inputs for video {video_id!r}: "
            f"non-terminal gif_clip stage(s) present ({offenders}); "
            f"wait for fan-out to finish"
        )

    stage_rows = all_clip_rows

    # Fetch artifacts for the SUCCEEDED stages only (failed/cancelled/
    # needs_attention stages are not required to have produced artifacts).
    succeeded_stage_ids = [
        r["stage_id"] for r in stage_rows if r["status"] == "succeeded"
    ]

    gif_files_by_cid: dict[str, ArtifactRef] = {}
    manifests_by_cid: dict[str, ArtifactRef] = {}

    if succeeded_stage_ids:
        placeholders = ",".join("?" for _ in succeeded_stage_ids)
        rows = conn.execute(
            f"""SELECT * FROM task_artifacts
               WHERE video_id=? AND stage_name='gif_clip'
                 AND artifact_kind IN ('gif_file', 'gif_clip_manifest')
                 AND stage_id IN ({placeholders})
               ORDER BY created_at ASC""",
            (video_id, *succeeded_stage_ids),
        ).fetchall()

        for row in rows:
            ref = ArtifactRef(
                artifact_id=row["artifact_id"],
                job_id=row["job_id"],
                video_id=row["video_id"],
                stage_name=row["stage_name"],
                clip_id=row["clip_id"],
                path=row["path"],
                sha256=row["sha256"],
                size_bytes=row["size_bytes"],
                provenance_json=row["provenance_json"],
                stage_id=row["stage_id"] or "",
                artifact_kind=row["artifact_kind"],
            )
            cid = ref.clip_id or ""
            if ref.artifact_kind == "gif_file":
                if cid in gif_files_by_cid:
                    raise ValueError(
                        f"Duplicate gif_file for clip {cid!r}: "
                        f"{gif_files_by_cid[cid].artifact_id} and {ref.artifact_id}"
                    )
                gif_files_by_cid[cid] = ref
            elif ref.artifact_kind == "gif_clip_manifest":
                if cid in manifests_by_cid:
                    raise ValueError(
                        f"Duplicate gif_clip_manifest for clip {cid!r}: "
                        f"{manifests_by_cid[cid].artifact_id} and {ref.artifact_id}"
                    )
                manifests_by_cid[cid] = ref

    # Validate every SUCCEEDED stage has a complete, consistent pair.
    for r in stage_rows:
        if r["status"] != "succeeded":
            continue
        stage_id = r["stage_id"]
        cid = r["clip_id"] or ""
        gif_ref = gif_files_by_cid.get(cid)
        man_ref = manifests_by_cid.get(cid)

        if gif_ref is None:
            raise ValueError(
                f"Succeeded gif_clip stage {stage_id!r} (clip {cid!r}) "
                f"has no gif_file artifact"
            )
        if man_ref is None:
            raise ValueError(
                f"Succeeded gif_clip stage {stage_id!r} (clip {cid!r}) "
                f"has no gif_clip_manifest artifact"
            )
        if gif_ref.stage_id != stage_id:
            raise ValueError(
                f"gif_file for clip {cid!r} belongs to stage "
                f"{gif_ref.stage_id!r}, not the succeeded stage {stage_id!r}"
            )
        if man_ref.stage_id != stage_id:
            raise ValueError(
                f"gif_clip_manifest for clip {cid!r} belongs to stage "
                f"{man_ref.stage_id!r}, not the succeeded stage {stage_id!r}"
            )

        validate_artifact_strict(gif_ref)
        validate_artifact_strict(man_ref)

        # Cross-check the manifest references the correct gif_path + sha.
        try:
            with open(man_ref.path, "r", encoding="utf-8") as f:
                manifest_data = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            raise ValueError(
                f"Cannot read gif_clip_manifest for clip {cid!r}: {exc}"
            ) from exc
        if manifest_data.get("clip_id") != cid:
            raise ValueError(
                f"gif_clip_manifest clip_id mismatch for {cid!r}: "
                f"manifest says {manifest_data.get('clip_id')!r}"
            )
        manifest_sha = manifest_data.get("sha256")
        if manifest_sha and manifest_sha != gif_ref.sha256:
            raise ValueError(
                f"gif_clip_manifest SHA-256 mismatch for clip {cid!r}: "
                f"manifest says {manifest_sha[:16]}..., "
                f"gif_file says {gif_ref.sha256[:16]}..."
            )

    # Deterministic ordering by clip_id for reproducible hashes.
    succeeded_cids = sorted(gif_files_by_cid)
    artifacts: dict[str, tuple[ArtifactRef, ...]] = {
        "gif_file": tuple(gif_files_by_cid[c] for c in succeeded_cids),
        "gif_clip_manifest": tuple(manifests_by_cid[c] for c in succeeded_cids),
    }

    # Build the complete terminal-status list (all gif_clip stages).
    stage_statuses: list[GifClipStatus] = []
    for r in stage_rows:
        last_error: str | None = None
        raw_err = r["last_error_json"]
        if raw_err:
            try:
                ej = json.loads(raw_err)
                if isinstance(ej, dict):
                    last_error = ej.get("message")
            except (json.JSONDecodeError, TypeError):
                last_error = None
        stage_statuses.append(GifClipStatus(
            stage_id=r["stage_id"],
            clip_id=r["clip_id"] or "",
            status=r["status"],
            attempt_count=r["attempt_count"],
            last_error=last_error,
        ))

    return MaterializeInputs(
        artifacts=artifacts,
        stage_statuses=tuple(stage_statuses),
        zero_clip=False,
    )


def build_materialize_input_envelope(
    materialize_inputs: MaterializeInputs,
    video_id: str,
) -> dict:
    """Build the versioned input envelope for the materialize stage.

    P1-1: ``stage_statuses`` is taken verbatim from the resolver's complete
    terminal-status list (succeeded / needs_attention / cancelled /
    failed).  It is NEVER derived from the gif_file artifacts - deriving
    status from artifacts was the bug that masked succeeded clips whose
    artifacts had gone missing.

    The envelope is a JSON-serializable dict with structure::

        {
          "schema_version": 1,
          "stage": "materialize",
          "artifacts": {
            "gif_file": [<serialized ArtifactRef>, ...],
            "gif_clip_manifest": [<serialized ArtifactRef>, ...]
          },
          "stage_statuses": [
            {"stage_id": "...", "clip_id": "...", "status": "...",
             "attempt_count": 1, "last_error": null}, ...
          ]
        }

    Statuses are sorted by (status, clip_id, stage_id) for a reproducible
    envelope hash.
    """
    gif_files = materialize_inputs.artifacts.get("gif_file", ())
    gif_manifests = materialize_inputs.artifacts.get("gif_clip_manifest", ())

    def _serialize(ref: ArtifactRef) -> dict:
        return {
            "artifact_id": ref.artifact_id,
            "stage_id": ref.stage_id,
            "artifact_kind": ref.artifact_kind,
            "clip_id": ref.clip_id,
            "path": ref.path,
            "sha256": ref.sha256,
            "size_bytes": ref.size_bytes,
        }

    # P1-1: use the resolver's complete stage_statuses (all terminal
    # gif_clip stages), re-sorted deterministically for a stable hash.
    statuses = sorted(
        (
            {
                "stage_id": s.stage_id,
                "clip_id": s.clip_id,
                "status": s.status,
                "attempt_count": s.attempt_count,
                "last_error": s.last_error,
            }
            for s in materialize_inputs.stage_statuses
        ),
        key=lambda d: (d["status"], d["clip_id"], d["stage_id"]),
    )

    return {
        "schema_version": 1,
        "stage": "materialize",
        "artifacts": {
            "gif_file": [_serialize(r) for r in gif_files],
            "gif_clip_manifest": [_serialize(r) for r in gif_manifests],
        },
        "stage_statuses": statuses,
    }


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
        "required_fields": ["schema_version", "stage", "clips", "clip_count"],
    },
    "gif_clip_manifest": {
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
        clip_count = data.get("clip_count", len(clips))
        if clip_count != len(clips):
            raise ValueError(
                f"rank_dedup_manifest clip_count ({clip_count}) != "
                f"len(clips) ({len(clips)})"
            )
        # Verify clip_ids are non-empty and unique
        clip_ids = [c.get("clip_id", "") for c in clips]
        if any(not cid for cid in clip_ids):
            raise ValueError("rank_dedup_manifest has a clip with empty clip_id")
        if len(set(clip_ids)) != len(clip_ids):
            raise ValueError("rank_dedup_manifest has duplicate clip_ids")

    return data
