from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

from app.task_engine.models import (
    ArtifactRef,
    CreateJob,
    JobRecord,
    RetryPolicy,
    StageError,
    StageName,
    StageRecord,
    TaskEvent,
    VideoRecord,
)

_ACTIVE_JOB_STATUSES = ("pending", "leased", "running", "retry_wait", "needs_attention")


class TaskEngineError(Exception):
    pass


class ActiveJobConflictError(TaskEngineError):
    def __init__(self, message: str, existing_job_id: str = ""):
        super().__init__(message)
        self.existing_job_id = existing_job_id


class LeaseOwnershipError(TaskEngineError):
    pass


class StageNotFoundError(TaskEngineError):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")


def _directory_key(directory: str) -> str:
    return os.path.normcase(os.path.abspath(directory))


def _scope_key(directory: str, video_paths: list[str] | None) -> str:
    """Compute a stable scope key from directory + sorted video paths.

    Two jobs share the same scope if they target the same directory
    AND the same video_paths (or both have None / empty video_paths).
    """
    dk = _directory_key(directory)
    if not video_paths:
        return f"{dk}:*"
    normalized = sorted(os.path.normcase(os.path.abspath(p)) for p in video_paths)
    import hashlib
    path_hash = hashlib.sha256("|".join(normalized).encode("utf-8")).hexdigest()[:16]
    return f"{dk}:{path_hash}"


def _stage_record(row: sqlite3.Row) -> StageRecord:
    return StageRecord(
        stage_id=row["stage_id"],
        video_id=row["video_id"],
        stage_name=row["stage_name"],
        clip_id=row["clip_id"],
        status=row["status"],
        attempt_count=row["attempt_count"],
    )


class TaskRepository:
    def __init__(
        self, conn: sqlite3.Connection, retry_policy: RetryPolicy | None = None
    ):
        self._conn = conn
        self._retry_policy = retry_policy or RetryPolicy()

    @property
    def conn(self) -> sqlite3.Connection:
        # Exposed so migrations can drive a single outer write transaction;
        # regular callers must prefer the repository methods.
        return self._conn

    def create_job(self, command: CreateJob) -> JobRecord:
        job_id = uuid.uuid4().hex
        now = _iso(_utcnow())
        directory_key = _directory_key(command.directory)
        conn = self._conn
        if hasattr(conn, "in_transaction") and conn.in_transaction:
            conn.commit()

        # Compute the scope key from normalized video paths.
        new_config = json.loads(command.config_json) if command.config_json else {}
        new_video_paths_list: list[str] | None = new_config.get("video_paths")
        new_scope = _scope_key(command.directory, new_video_paths_list)

        conn.execute("BEGIN IMMEDIATE")
        try:
            # Check ALL active jobs for this directory, comparing scope_keys.
            # Same scope_key with an active job means a true conflict.
            active_jobs = self._find_active_jobs(directory_key)
            for active_job in active_jobs:
                existing_job_id = active_job["job_id"]
                existing_cfg = json.loads(active_job["config_json"]) if active_job["config_json"] else {}
                existing_vp: list[str] | None = existing_cfg.get("video_paths")
                existing_scope = _scope_key(command.directory, existing_vp)
                if new_scope == existing_scope:
                    raise ActiveJobConflictError(
                        f"An active job ({existing_job_id}) with the same scope "
                        f"already exists for directory {command.directory!r}",
                        existing_job_id=existing_job_id,
                    )
                # Different scope_keys are allowed — they target different
                # video_paths within the same directory (Quality Lab items).

            try:
                conn.execute(
                    """INSERT INTO task_jobs
                       (job_id, directory, directory_key, config_json, job_limit,
                        extensions, status, created_at, updated_at)
                       VALUES (?,?,?,?,?,?, 'pending', ?, ?)""",
                    (
                        job_id,
                        command.directory,
                        directory_key,
                        command.config_json,
                        command.limit,
                        command.extensions,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                # The partial unique index is a backstop for races against
                # other connections.  Re-check all active jobs.
                active_jobs2 = self._find_active_jobs(directory_key)
                for aj in active_jobs2:
                    aj_cfg = json.loads(aj["config_json"]) if aj["config_json"] else {}
                    aj_vp: list[str] | None = aj_cfg.get("video_paths")
                    aj_scope = _scope_key(command.directory, aj_vp)
                    if new_scope == aj_scope:
                        raise ActiveJobConflictError(
                            f"An active job ({aj['job_id']}) with the same scope "
                            f"already exists for directory {command.directory!r}",
                            existing_job_id=aj["job_id"],
                        ) from exc
                # Re-raise if no scope match found (genuine integrity error).
                raise
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return JobRecord(job_id=job_id, directory=command.directory, status="pending")

    def append_command(self, job_id: str, kind: str, payload: dict) -> str:
        command_id = uuid.uuid4().hex
        try:
            self._conn.execute(
                """INSERT INTO task_commands
                   (command_id, job_id, kind, payload_json, status, created_at)
                   VALUES (?,?,?,?, 'pending', ?)""",
                (command_id, job_id, kind, json.dumps(payload), _iso(_utcnow())),
            )
            self._conn.commit()
        except sqlite3.IntegrityError:
            self._conn.rollback()
            raise
        return command_id

    def add_video(self, job_id: str, path: str, fingerprint: str) -> VideoRecord:
        existing = self._find_video(job_id, path)
        if existing is not None:
            return existing
        video_id = uuid.uuid4().hex
        now = _iso(_utcnow())
        try:
            self._conn.execute(
                """INSERT INTO task_videos
                   (video_id, job_id, path, fingerprint, status, created_at, updated_at)
                   VALUES (?,?,?,?, 'pending', ?, ?)""",
                (video_id, job_id, path, fingerprint, now, now),
            )
            self._conn.commit()
        except sqlite3.IntegrityError:
            self._conn.rollback()
            existing = self._find_video(job_id, path)
            if existing is not None:
                return existing
            raise
        return VideoRecord(
            video_id=video_id,
            job_id=job_id,
            path=path,
            fingerprint=fingerprint,
            status="pending",
        )

    def ensure_stage(
        self,
        video_id: str,
        name: StageName,
        input_key: str,
        clip_id: str | None = None,
    ) -> StageRecord:
        existing = self._find_stage(video_id, name, input_key, clip_id)
        if existing is not None:
            return existing
        stage_id = uuid.uuid4().hex
        now = _iso(_utcnow())
        try:
            self._conn.execute(
                """INSERT INTO task_stages
                   (stage_id, video_id, stage_name, clip_id, input_key, status,
                    attempt_count, created_at, updated_at)
                   VALUES (?,?,?,?,?, 'pending', 0, ?, ?)""",
                (stage_id, video_id, name, clip_id, input_key, now, now),
            )
            self._conn.commit()
        except sqlite3.IntegrityError:
            self._conn.rollback()
            existing = self._find_stage(video_id, name, input_key, clip_id)
            if existing is not None:
                return existing
            raise
        return StageRecord(
            stage_id=stage_id,
            video_id=video_id,
            stage_name=name,
            clip_id=clip_id,
            status="pending",
            attempt_count=0,
        )

    def claim_stage(
        self, worker_id: str, now: datetime, lease_seconds: int = 90
    ) -> StageRecord | None:
        now_iso = _iso(now)
        lease_expires = _iso(now + timedelta(seconds=lease_seconds))
        conn = self._conn
        # Ensure no stale implicit transaction is open before BEGIN IMMEDIATE.
        if hasattr(conn, "in_transaction") and conn.in_transaction:
            conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                """SELECT * FROM task_stages
                   WHERE status = 'pending'
                      OR (status = 'retry_wait' AND retry_at <= ?)
                      OR (status IN ('leased','running') AND lease_expires_at <= ?)
                   ORDER BY created_at ASC, stage_id ASC
                   LIMIT 1""",
                (now_iso, now_iso),
            ).fetchone()
            if row is None:
                conn.rollback()
                return None
            attempt_count = row["attempt_count"] + 1
            conn.execute(
                """UPDATE task_stages
                   SET status='leased', lease_owner=?, lease_expires_at=?,
                       retry_at=NULL, attempt_count=?, updated_at=?
                   WHERE stage_id=?""",
                (worker_id, lease_expires, attempt_count, now_iso, row["stage_id"]),
            )
            self._append_event(
                "stage.claimed",
                {
                    "stage_id": row["stage_id"],
                    "video_id": row["video_id"],
                    "stage_name": row["stage_name"],
                    "worker_id": worker_id,
                    "attempt_count": attempt_count,
                    "lease_expires_at": lease_expires,
                },
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return StageRecord(
            stage_id=row["stage_id"],
            video_id=row["video_id"],
            stage_name=row["stage_name"],
            clip_id=row["clip_id"],
            status="leased",
            attempt_count=attempt_count,
        )

    def complete_stage(self, stage_id: str, worker_id: str, output_key: str) -> None:
        now_iso = _iso(_utcnow())
        conn = self._conn
        if hasattr(conn, "in_transaction") and conn.in_transaction:
            conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._lease_check(stage_id, worker_id)
            conn.execute(
                """UPDATE task_stages
                   SET status='succeeded', output_key=?, lease_owner=NULL,
                       lease_expires_at=NULL, last_error_json=NULL, updated_at=?
                   WHERE stage_id=?""",
                (output_key, now_iso, stage_id),
            )
            self._append_event(
                "stage.completed",
                {
                    "stage_id": stage_id,
                    "video_id": row["video_id"],
                    "stage_name": row["stage_name"],
                    "worker_id": worker_id,
                    "output_key": output_key,
                },
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def complete_stage_with_artifacts(
        self,
        stage_id: str,
        worker_id: str,
        output_key: str,
        artifacts: tuple[ArtifactRef, ...],
        needs_attention: bool = False,
        attention_message: str | None = None,
    ) -> None:
        """Atomically complete a stage and persist its artifacts.

        All artifact file validation (SHA-256, size) must be done by the
        caller BEFORE calling this method to avoid holding the SQLite
        write lock during I/O.

        Inside a single ``BEGIN IMMEDIATE`` transaction:

        1. Verify stage exists, status allows completion, lease_owner matches.
        2. Validate artifact ownership (job/video/stage/clip consistency).
        3. Upsert every artifact with dedup collision detection.
        4. Update stage to succeeded, clear lease.
        5. Write stage.completed event.
        6. One commit.

        If any step fails, the entire transaction rolls back — no partial
        artifacts are left behind.
        """
        import json as _json

        from app.task_engine.artifacts import (
            ArtifactCollisionError,
            insert_artifact_dedup,
        )

        conn = self._conn
        if hasattr(conn, "in_transaction") and conn.in_transaction:
            conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        try:
            # 1. Re-verify stage ownership inside the transaction.
            stage_row = self._lease_check(stage_id, worker_id)

            # 2. Validate artifact ownership consistency.
            video_id = stage_row["video_id"]
            job_row = conn.execute(
                "SELECT job_id FROM task_videos WHERE video_id=?",
                (video_id,),
            ).fetchone()
            if job_row is None:
                raise ValueError(f"Video {video_id} not found for stage {stage_id}")
            job_id = job_row["job_id"]
            stage_name = stage_row["stage_name"]
            stage_clip_id = stage_row["clip_id"] or None

            for ref in artifacts:
                if ref.stage_id != stage_id:
                    raise ValueError(
                        f"Artifact {ref.artifact_id} stage_id {ref.stage_id!r} "
                        f"!= stage {stage_id!r}"
                    )
                if ref.job_id != job_id:
                    raise ValueError(
                        f"Artifact {ref.artifact_id} belongs to job {ref.job_id}, "
                        f"not {job_id}"
                    )
                if ref.video_id != video_id:
                    raise ValueError(
                        f"Artifact {ref.artifact_id} belongs to video {ref.video_id}, "
                        f"not {video_id}"
                    )
                if ref.stage_name != stage_name:
                    raise ValueError(
                        f"Artifact {ref.artifact_id} stage_name {ref.stage_name} "
                        f"!= {stage_name}"
                    )
                # P0-1: Reject empty stage_id — every artifact must be
                # associated with a real stage.
                if not ref.stage_id:
                    raise ValueError(
                        f"Artifact {ref.artifact_id} has empty stage_id; "
                        f"production adapters must set context.stage_id"
                    )
                # P0-1: Reject 'generic' artifact_kind for new records.
                # Existing records with 'generic' are allowed (backwards compat)
                # but new artifacts must carry a specific kind.
                if ref.artifact_kind == "generic":
                    # Check if this artifact_id already exists with 'generic'.
                    existing_kind = conn.execute(
                        "SELECT artifact_kind FROM task_artifacts WHERE artifact_id=?",
                        (ref.artifact_id,),
                    ).fetchone()
                    if existing_kind is None:
                        raise ValueError(
                            f"Artifact {ref.artifact_id} has artifact_kind='generic'; "
                            f"new artifacts must specify a concrete kind"
                        )
                ref_clip = ref.clip_id or None
                if ref_clip != stage_clip_id:
                    raise ValueError(
                        f"Artifact {ref.artifact_id} clip_id {ref_clip!r} "
                        f"!= {stage_clip_id!r}"
                    )

            # 3. Upsert all artifacts (dedup with collision detection).
            for ref in artifacts:
                insert_artifact_dedup(conn, ref)

            # 4. Update stage.  P0-2: needs_attention completes the stage
            # while flagging it for human review (still persists artifacts).
            now_iso = _iso(_utcnow())
            if needs_attention:
                status = "needs_attention"
                err_json = json.dumps({
                    "code": "publish_failure",
                    "message": attention_message or "stage completed with publish failures",
                    "transient": False,
                })
            else:
                status = "succeeded"
                err_json = None
            conn.execute(
                """UPDATE task_stages
                   SET status=?, output_key=?, lease_owner=NULL,
                       lease_expires_at=NULL, last_error_json=?, updated_at=?
                   WHERE stage_id=?""",
                (status, output_key, err_json, now_iso, stage_id),
            )

            # 5. Write event.
            self._append_event(
                "stage.needs_attention" if needs_attention else "stage.completed",
                {
                    "stage_id": stage_id,
                    "video_id": video_id,
                    "stage_name": stage_name,
                    "worker_id": worker_id,
                    "output_key": output_key,
                    "artifact_count": len(artifacts),
                },
            )

            # 6. Single commit.
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def fail_stage(self, stage_id: str, worker_id: str, error: StageError) -> None:
        now = _utcnow()
        now_iso = _iso(now)
        conn = self._conn
        policy = self._retry_policy
        if hasattr(conn, "in_transaction") and conn.in_transaction:
            conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._lease_check(stage_id, worker_id)
            attempts = row["attempt_count"]
            if error.transient and attempts < policy.max_attempts:
                backoff = min(
                    policy.base_delay_seconds * 2 ** (attempts - 1),
                    policy.max_delay_seconds,
                )
                status = "retry_wait"
                retry_at = _iso(now + timedelta(seconds=backoff))
            else:
                status = "needs_attention"
                retry_at = None
            conn.execute(
                """UPDATE task_stages
                   SET status=?, retry_at=?, lease_owner=NULL, lease_expires_at=NULL,
                       last_error_json=?, updated_at=?
                   WHERE stage_id=?""",
                (
                    status,
                    retry_at,
                    json.dumps(
                        {
                            "code": error.code,
                            "message": error.message,
                            "transient": error.transient,
                        }
                    ),
                    now_iso,
                    stage_id,
                ),
            )
            self._append_event(
                "stage.failed",
                {
                    "stage_id": stage_id,
                    "video_id": row["video_id"],
                    "stage_name": row["stage_name"],
                    "worker_id": worker_id,
                    "error_code": error.code,
                    "transient": error.transient,
                    "status": status,
                },
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def list_events(self, *, after_id: int = 0, limit: int = 200) -> list[TaskEvent]:
        limit = max(1, min(limit, 1000))
        rows = self._conn.execute(
            """SELECT event_id, kind, payload_json, created_at FROM task_events
               WHERE event_id > ? ORDER BY event_id ASC LIMIT ?""",
            (after_id, limit),
        ).fetchall()
        return [
            TaskEvent(
                event_id=row["event_id"],
                kind=row["kind"],
                payload=json.loads(row["payload_json"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def _find_active_job_id(self, directory_key: str) -> str | None:
        placeholders = ",".join("?" for _ in _ACTIVE_JOB_STATUSES)
        row = self._conn.execute(
            f"""SELECT job_id FROM task_jobs
                WHERE directory_key=? AND status IN ({placeholders})
                ORDER BY created_at ASC, job_id ASC
                LIMIT 1""",
            (directory_key, *_ACTIVE_JOB_STATUSES),
        ).fetchone()
        return row["job_id"] if row is not None else None

    def _find_active_jobs(self, directory_key: str) -> list[sqlite3.Row]:
        """Return ALL active jobs (any status in _ACTIVE_JOB_STATUSES) for the directory_key."""
        placeholders = ",".join("?" for _ in _ACTIVE_JOB_STATUSES)
        return list(self._conn.execute(
            f"""SELECT job_id, config_json FROM task_jobs
                WHERE directory_key=? AND status IN ({placeholders})
                ORDER BY created_at ASC, job_id ASC""",
            (directory_key, *_ACTIVE_JOB_STATUSES),
        ).fetchall())

    def _find_video(self, job_id: str, path: str) -> VideoRecord | None:
        row = self._conn.execute(
            "SELECT * FROM task_videos WHERE job_id=? AND path=?",
            (job_id, path),
        ).fetchone()
        if row is None:
            return None
        return VideoRecord(
            video_id=row["video_id"],
            job_id=row["job_id"],
            path=row["path"],
            fingerprint=row["fingerprint"],
            status=row["status"],
        )

    def _find_stage(
        self, video_id: str, name: StageName, input_key: str, clip_id: str | None
    ) -> StageRecord | None:
        row = self._conn.execute(
            """SELECT * FROM task_stages
               WHERE video_id=? AND stage_name=?
                 AND COALESCE(clip_id, '')=COALESCE(?, '') AND input_key=?""",
            (video_id, name, clip_id, input_key),
        ).fetchone()
        return _stage_record(row) if row is not None else None

    def _lease_check(self, stage_id: str, worker_id: str) -> sqlite3.Row:
        row = self._conn.execute(
            "SELECT * FROM task_stages WHERE stage_id=?", (stage_id,)
        ).fetchone()
        if row is None:
            raise StageNotFoundError(f"unknown stage {stage_id!r}")
        if row["status"] not in ("leased", "running") or row["lease_owner"] != worker_id:
            raise LeaseOwnershipError(
                f"worker {worker_id!r} does not hold the lease for stage {stage_id!r}"
            )
        return row

    def _append_event(self, kind: str, payload: dict) -> int:
        cursor = self._conn.execute(
            "INSERT INTO task_events (kind, payload_json, created_at) VALUES (?,?,?)",
            (kind, json.dumps(payload), _iso(_utcnow())),
        )
        return cursor.lastrowid
