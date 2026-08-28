"""task_artifacts persistence: dedup insertion and stage fetches."""
from __future__ import annotations

import sqlite3

from app.task_engine.artifacts.identity import (
    ArtifactCollisionError,
    validate_artifact_strict,
)
from app.task_engine.models import ArtifactRef


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
