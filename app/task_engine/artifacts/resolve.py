"""Resolvers that assemble stage inputs from task_artifacts."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from app.task_engine.artifacts.identity import validate_artifact_strict
from app.task_engine.artifacts.kinds import (
    STAGE_INPUT_KINDS,
    STAGE_OPTIONAL_INPUT_KINDS,
    _INPUT_PRODUCER,
)
from app.task_engine.artifacts.manifests import validate_manifest_json
from app.task_engine.artifacts.store import _fetch_artifacts_for_stage
from app.task_engine.models import ArtifactRef, StageName


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
    required_kinds = STAGE_INPUT_KINDS.get(stage_name, ())
    optional_kinds = STAGE_OPTIONAL_INPUT_KINDS.get(stage_name, ())
    kinds = required_kinds + optional_kinds
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
        if not refs and kind in required_kinds:
            raise FileNotFoundError(
                f"No artifact of kind {kind!r} found for video {video_id!r}"
            )
        if refs:
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


def _latest_succeeded_artifact_ref(
    conn: sqlite3.Connection,
    video_id: str,
    *,
    stage_name: str,
    artifact_kind: str,
) -> ArtifactRef:
    row = conn.execute(
        """SELECT a.* FROM task_artifacts a
           JOIN task_stages s ON a.stage_id=s.stage_id
           WHERE a.video_id=? AND a.stage_name=? AND a.artifact_kind=?
             AND s.status='succeeded'
           ORDER BY a.created_at DESC LIMIT 1""",
        (video_id, stage_name, artifact_kind),
    ).fetchone()
    if row is None:
        raise ValueError(
            f"Missing succeeded {artifact_kind} for video {video_id!r}"
        )
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
    return ref


def validate_rank_manifest_with_db_lineage(
    conn: sqlite3.Connection,
    video_id: str,
    raw_bytes: bytes,
) -> dict:
    """Validate rank output against separately registered immutable inputs."""
    rank_ref = _latest_succeeded_artifact_ref(
        conn,
        video_id,
        stage_name="rank_dedup",
        artifact_kind="rank_dedup_manifest",
    )
    if hashlib.sha256(raw_bytes).hexdigest() != rank_ref.sha256:
        raise ValueError("rank_dedup_manifest SHA-256 mismatch")
    try:
        preview = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        preview = None
    if not isinstance(preview, dict) or preview.get("schema_version") != 2 or (
        "quality_moe" not in preview
    ):
        return validate_manifest_json(
            raw_bytes, "rank_dedup_manifest", expected_stage="rank_dedup"
        )
    ledger_ref = _latest_succeeded_artifact_ref(
        conn,
        video_id,
        stage_name="rank_dedup",
        artifact_kind="rank_candidate_ledger",
    )
    upstream_ref = _latest_succeeded_artifact_ref(
        conn,
        video_id,
        stage_name="synthesize",
        artifact_kind="synthesize_manifest",
    )
    return validate_manifest_json(
        raw_bytes,
        "rank_dedup_manifest",
        expected_stage="rank_dedup",
        candidate_ledger_bytes=Path(ledger_ref.path).read_bytes(),
        candidate_ledger_ref=ledger_ref.__dict__,
        upstream_artifact_ref=upstream_ref.__dict__,
        require_external_quality_ledger=True,
    )


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
    manifest = validate_rank_manifest_with_db_lineage(conn, video_id, raw)

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
