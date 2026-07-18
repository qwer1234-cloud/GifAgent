from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.task_engine.fingerprints import sha256_file
from app.task_engine.models import (
    ArtifactRef,
    RetryPolicy,
    StageError,
    StageName,
)
from app.task_engine.repository import TaskRepository
from app.task_engine.stages import StageAdapter, StageContext, StageResult

_RESULT_FILE = ".stage_result.json"


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def classify_error(exc: Exception, stage_name: StageName) -> StageError:
    """Classify an exception into a transient or attention StageError.

    Transient errors are eligible for automatic retry; attention errors
    require human intervention.
    """
    # ------------------------------------------------------------------
    # SQLite busy / locked — transient
    # ``sqlite3.BusyError`` was added in Python 3.13 and is not always
    # available; fall back to checking the message on OperationalError.
    # ------------------------------------------------------------------
    if isinstance(exc, sqlite3.OperationalError):
        msg = str(exc).lower()
        if "busy" in msg or "locked" in msg:
            return StageError("db_busy", str(exc), transient=True)

    # ------------------------------------------------------------------
    # HTTP rate-limit / server error — transient
    # Duck-type for ``response`` attribute so we don't force an httpx
    # dependency at import time.
    # ------------------------------------------------------------------
    if hasattr(exc, "response") and hasattr(exc.response, "status_code"):
        sc = exc.response.status_code
        if sc in (429, 502, 503, 504):
            return StageError(f"http_{sc}", str(exc), transient=True)

    # ------------------------------------------------------------------
    # subprocess.CalledProcessError — ffmpeg specific failures are
    # attention; all others are transient.
    # ------------------------------------------------------------------
    if isinstance(exc, subprocess.CalledProcessError):
        cmd_str = (
            " ".join(exc.cmd)
            if isinstance(exc.cmd, (list, tuple))
            else str(exc.cmd)
        )
        if "ffmpeg" in cmd_str.lower():
            return StageError("ffmpeg_error", str(exc), transient=False)
        return StageError("process_error", str(exc), transient=True)

    # ------------------------------------------------------------------
    # OSError (incl. FileNotFoundError, PermissionError) — classify by
    # message keywords.
    # ------------------------------------------------------------------
    if isinstance(exc, OSError):
        msg = str(exc).lower()
        # Attention: the media file itself is missing or invalid.
        if any(kw in msg for kw in ("no such file", "invalid data", "not a valid")):
            return StageError("invalid_media", str(exc), transient=False)
        # Transient: disk-full or temporary I/O glitch.
        if any(
            kw in msg for kw in ("no space", "disk full", "i/o error", "temporarily")
        ):
            return StageError("io_error", str(exc), transient=True)
        # Default OSError → transient (could be a mount issue, share
        # temporarily unavailable, etc.).
        return StageError("io_error", str(exc), transient=True)

    # ------------------------------------------------------------------
    # ModelNotFoundError — attention
    # Check by class name so we don't force an import of the VLM module.
    # ------------------------------------------------------------------
    exc_class_name = type(exc).__name__
    if exc_class_name == "ModelNotFoundError" or "ModelNotFound" in exc_class_name:
        return StageError("model_not_found", str(exc), transient=False)

    # ------------------------------------------------------------------
    # Checksum mismatch — attention (indicates corrupted artifacts)
    # ------------------------------------------------------------------
    exc_msg = str(exc).lower()
    if "checksum" in exc_msg or "sha256" in exc_msg:
        return StageError("checksum_mismatch", str(exc), transient=False)

    # ------------------------------------------------------------------
    # ffmpeg mentioned in exception message — attention
    # ------------------------------------------------------------------
    if "ffmpeg" in exc_msg:
        return StageError("ffmpeg_error", str(exc), transient=False)

    # ------------------------------------------------------------------
    # Default — transient (most unexpected errors are transient)
    # ------------------------------------------------------------------
    return StageError("unknown", str(exc), transient=True)


class TaskWorker:
    """Single-writer worker that claims and runs pipeline stages.

    A ``TaskWorker`` is the *only* component that should advance a stage's
    lifecycle (pending → leased → running → succeeded / retry_wait /
    needs_attention).  It runs a single stage per ``run_once`` call and
    can be driven in a loop via ``run_forever``.

    Parameters
    ----------
    repo:
        The ``TaskRepository`` that owns the database connection.
    worker_id:
        Unique identifier for this worker (used for lease ownership).
    adapters:
        Mapping from ``StageName`` to ``StageAdapter`` implementations.
    retry_policy:
        Override the default back-off parameters.
    lease_seconds:
        Duration (seconds) a claimed stage remains leased before it can
        be reclaimed by another worker.  Default 90.
    heartbeat_seconds:
        Interval (seconds) between lease-renewal heartbeats.  Defaults to
        ``max(1, lease_seconds // 3)``.  Must be less than ``lease_seconds``.
    db_path:
        Path to the SQLite task database file, used by the heartbeat thread
        to open its own connection.  If not provided, derived from the
        repository connection's PRAGMA database_list.
    """

    _RESULT_FILE = _RESULT_FILE

    def __init__(
        self,
        repo: TaskRepository,
        worker_id: str,
        adapters: dict[StageName, StageAdapter],
        retry_policy: RetryPolicy | None = None,
        lease_seconds: int = 90,
        heartbeat_seconds: int | None = None,
        db_path: str | None = None,
    ) -> None:
        self._repo = repo
        self._worker_id = worker_id
        self._adapters = dict(adapters)
        self._retry_policy = retry_policy or RetryPolicy()
        self._lease_seconds = max(1, lease_seconds)
        self._heartbeat_seconds = heartbeat_seconds or max(1, self._lease_seconds // 3)
        self._db_path = db_path
        # P1-4: Thread-safe lease-lost flag checked before committing stage results.
        self._lease_lost = False
        self._lease_lock = __import__("threading").Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run_once(self, now: datetime | None = None) -> bool:
        """Claim one stage, run it, and record the outcome.

        Also checks for pending jobs that need initialisation (directory
        scanning, video + stage creation) before trying to claim stages.

        Returns ``True`` if a stage was processed (regardless of success
        or failure), or ``False`` if no work was available.
        """
        now = now or _utcnow()

        # 0. Initialise any pending jobs (discover videos, create stages).
        initialized = self._initialize_pending_jobs()
        if initialized > 0:
            return True

        # 1. Claim a stage.  ``None`` means the queue is empty.
        stage = self._repo.claim_stage(self._worker_id, now, lease_seconds=self._lease_seconds)
        if stage is None:
            return False

        # 2-8 as before...
        return self._run_stage(stage, now)

    def drain(self) -> int:
        """Process all available stages (non‑blocking), then return.

        Used by tests and the ``--once`` CLI script.  Does **not** sleep
        when idle — returns 0 immediately.
        """
        count = 0
        while True:
            if self.run_once():
                count += 1
            else:
                break
        return count

    def run_forever(self, poll_seconds: float = 1.0, stop_event=None) -> int:
        """Run ``run_once`` in a loop, sleeping when idle.

        When *stop_event* (a ``threading.Event``) is set the method
        exits after finishing its current iteration.

        Returns the total number of stages processed.
        """
        import time

        count = 0
        while True:
            if stop_event is not None and stop_event.is_set():
                break
            if self.run_once():
                count += 1
            else:
                if stop_event is not None and stop_event.is_set():
                    break
                time.sleep(poll_seconds)
        return count

    def heartbeat(self, stage_id: str, now: datetime | None = None) -> None:
        """Extend the lease on a running stage.

        This is safe to call from a background thread or signal handler.
        If the database is locked the update is silently skipped — the lease
        will expire naturally and the stage will be reclaimed by another
        worker.
        """
        now = now or _utcnow()
        expires = now + timedelta(seconds=self._lease_seconds)
        try:
            self._repo.conn.execute("BEGIN IMMEDIATE")
            self._repo.conn.execute(
                "UPDATE task_stages SET lease_expires_at=? WHERE stage_id=? AND lease_owner=?",
                (_iso(expires), stage_id, self._worker_id),
            )
            self._repo.conn.commit()
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            if "busy" not in msg and "locked" not in msg:
                raise

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _check_cancelled(self, stage, now: datetime) -> bool:
        """Return ``True`` if the job has a pending cancellation command."""
        video_row = self._repo.conn.execute(
            "SELECT job_id FROM task_videos WHERE video_id=?",
            (stage.video_id,),
        ).fetchone()
        if video_row is None:
            return False
        job_id = video_row["job_id"]

        has_cancel = self._repo.conn.execute(
            "SELECT 1 FROM task_commands WHERE job_id=? AND kind='cancel' AND status='pending' LIMIT 1",
            (job_id,),
        ).fetchone()

        if has_cancel:
            now_iso = _iso(now)
            self._repo.conn.execute("BEGIN IMMEDIATE")
            self._repo.conn.execute(
                """UPDATE task_stages
                   SET status='cancelled', lease_owner=NULL,
                       lease_expires_at=NULL, updated_at=?
                   WHERE stage_id=?""",
                (now_iso, stage.stage_id),
            )
            self._repo.conn.commit()
            return True
        return False

    def _build_context(self, stage) -> StageContext:
        """Resolve a ``StageContext`` from the stage record and job config.

        Uses the Artifact resolver to look up the stage's exact inputs
        from ``task_artifacts`` (the database is the single source of truth).
        Does NOT inject ``prev_stage_work_dir`` or any directory-guessing
        into the config — downstream stages must read from
        ``StageContext.inputs``.
        """
        # Look up the video's job and path.
        video_row = self._repo.conn.execute(
            "SELECT job_id, path FROM task_videos WHERE video_id=?",
            (stage.video_id,),
        ).fetchone()
        if video_row is None:
            raise RuntimeError(
                f"Video {stage.video_id!r} not found for stage {stage.stage_id}"
            )
        job_id = video_row["job_id"]
        video_path = Path(video_row["path"])

        # Look up the input_key the stage was created with.
        stage_row = self._repo.conn.execute(
            "SELECT input_key FROM task_stages WHERE stage_id=?",
            (stage.stage_id,),
        ).fetchone()
        if stage_row is None:
            raise RuntimeError(f"Stage {stage.stage_id!r} not found in database")
        input_key = stage_row["input_key"]

        # Load the immutable job config snapshot (never mutated).
        job_row = self._repo.conn.execute(
            "SELECT config_json FROM task_jobs WHERE job_id=?",
            (job_id,),
        ).fetchone()
        if job_row is None:
            raise RuntimeError(f"Job {job_id!r} not found for stage {stage.stage_id}")
        config = json.loads(job_row["config_json"])
        # Phase 3: Normalize config format (handles both historical
        # config_snapshot wrapper and new top-level format).
        from app.quality_lab.config_builder import normalize_task_config
        config = normalize_task_config(config)

        base = Path(config.get("task_work_dir", "data/task_work"))
        work_dir = base / stage.stage_name / stage.stage_id

        # ── Resolve upstream inputs from task_artifacts (the database) ──
        from app.task_engine.artifacts import (
            build_materialize_input_envelope,
            resolve_materialize_inputs,
            resolve_stage_inputs,
            validate_materialize_envelope,
        )

        # P0-2: materialize stage uses a dedicated stage-driven resolver to
        # aggregate ALL terminal gif_clip stages (not a generic resolver).
        # The resolver raises if any SUCCEEDED clip is missing artifacts, and
        # returns an explicit zero_clip result when no gif_clip stages exist.
        if stage.stage_name == "materialize":
            mat_inputs = resolve_materialize_inputs(
                self._repo.conn, stage.video_id,
            )
            envelope = build_materialize_input_envelope(
                mat_inputs, stage.video_id,
            )
            # P1-2: defend against an unknown envelope version before handing
            # it to the subprocess (fail loudly, never silently mis-parse).
            validate_materialize_envelope(envelope)
            # Store the envelope JSON in config so the subprocess adapter
            # can write it for the stage script to read.
            config["_materialize_envelope"] = envelope

            # For the StageContext inputs, pass the raw artifact refs
            # keyed by kind (only succeeded clips have artifacts).
            inputs = dict(mat_inputs.artifacts)
        else:
            inputs = resolve_stage_inputs(
                self._repo.conn, stage.video_id, stage.stage_name,
                clip_id=stage.clip_id,
            )

        return StageContext(
            job_id=job_id,
            video_id=stage.video_id,
            video_path=video_path,
            clip_id=stage.clip_id,
            input_key=input_key,
            work_dir=work_dir,
            config=config,
            stage_id=stage.stage_id,
            inputs=inputs,
        )

    def _try_recover(self, stage, context) -> bool:
        """Try to recover artifacts left by a crashed previous run.

        Returns ``True`` if valid artifacts were found and the stage was
        completed.  Returns ``False`` if no usable artifacts exist (caller
        should re-run the stage).

        Uses ``complete_stage_with_artifacts`` for atomic recovery — all
        artifacts and the stage completion happen in a single transaction.
        """
        result_file = context.work_dir / self._RESULT_FILE
        if not result_file.exists():
            return False

        try:
            with open(result_file, "r") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return False

        # Validate schema_version, stage_id, stage_name.
        if data.get("schema_version") != 1:
            return False
        if data.get("stage_id") != stage.stage_id:
            return False
        if data.get("stage_name") != stage.stage_name:
            return False

        # Validate every artifact file and build ArtifactRef objects.
        from app.task_engine.fingerprints import sha256_file as _sha

        artifacts: list[ArtifactRef] = []
        for a in data.get("artifacts", []):
            path = Path(a["path"])
            if not path.exists():
                return False
            try:
                actual_sha256 = _sha(path)
                if actual_sha256 != a.get("sha256"):
                    return False
                actual_size = path.stat().st_size
                if actual_size != a.get("size_bytes", 0):
                    return False
            except OSError:
                return False

            from app.task_engine.artifacts import make_artifact_id

            stage_id = a.get("stage_id", stage.stage_id)
            kind = a.get("artifact_kind") or f"{stage.stage_name}_manifest"
            artifact_id = a.get("artifact_id") or make_artifact_id(
                stage_id=stage_id,
                artifact_kind=kind,
                clip_id=a.get("clip_id", stage.clip_id),
                normalized_path=str(path),
            )

            artifacts.append(
                ArtifactRef(
                    artifact_id=artifact_id,
                    job_id=context.job_id,
                    video_id=context.video_id,
                    stage_name=stage.stage_name,
                    clip_id=a.get("clip_id", stage.clip_id),
                    path=str(path),
                    sha256=actual_sha256,
                    size_bytes=actual_size,
                    provenance_json=a.get("provenance_json", "{}"),
                    stage_id=stage_id,
                    artifact_kind=kind,
                )
            )

        # All artifacts validated — use atomic completion.
        # P0-1 (fifth-review §3): validate the persisted outcome strictly.
        # A missing outcome (legacy file) is tolerated as ``succeeded``;
        # an UNKNOWN outcome must force a re-run, never silently succeed.
        from app.task_engine.stages import normalize_outcome
        try:
            outcome = normalize_outcome(data.get("outcome"))
        except ValueError:
            return False
        attention_message = data.get("attention_message")

        # Commit via the unified helper so recovery and normal completion
        # share one status path (no second set of outcome semantics).
        try:
            self._commit_stage_result(
                stage, data["output_key"], tuple(artifacts),
                outcome, attention_message,
            )
        except Exception:
            return False
        return True

    def _save_result(self, work_dir: Path, result: StageResult, stage) -> None:
        """Persist a ``StageResult`` to the work directory for crash recovery.

        P0-1 (fifth-review §3): the outcome (and the derived attention
        message) MUST be persisted so a later ``_try_recover`` reproduces
        the same terminal status as the normal commit path - otherwise a
        crash between the result-file write and the DB commit would turn a
        ``needs_attention`` materialize into a false ``succeeded``.
        """
        work_dir.mkdir(parents=True, exist_ok=True)
        result_file = work_dir / self._RESULT_FILE

        from app.task_engine.stages import normalize_outcome
        outcome = normalize_outcome(result.outcome)
        attn_msg = None
        if outcome == "needs_attention":
            attn_msg = self._attention_message(stage, result)

        data = {
            "schema_version": 1,
            "stage_id": stage.stage_id,
            "stage_name": stage.stage_name,
            "output_key": result.output_key,
            "outcome": outcome,
            "attention_message": attn_msg,
            "artifacts": [
                {
                    "artifact_id": a.artifact_id,
                    "job_id": a.job_id,
                    "video_id": a.video_id,
                    "stage_name": a.stage_name,
                    "clip_id": a.clip_id,
                    "path": a.path,
                    "sha256": a.sha256,
                    "size_bytes": a.size_bytes,
                    "provenance_json": a.provenance_json,
                    "stage_id": a.stage_id,
                    "artifact_kind": a.artifact_kind,
                }
                for a in result.artifacts
            ],
            "metrics": dict(result.metrics),
        }
        with open(result_file, "w") as f:
            json.dump(data, f)

    def _attention_message(self, stage, result: StageResult) -> str:
        """Derive a human-readable attention message from the stage result."""
        fc = 0
        if result.metrics:
            fc = result.metrics.get("failed_count", 0)
        return f"{stage.stage_name} completed with {fc} publish failure(s)"

    def _commit_stage_result(
        self,
        stage,
        output_key: str,
        artifacts: tuple[ArtifactRef, ...],
        outcome: str,
        attention_message: str | None = None,
    ) -> None:
        """Commit a stage result in a single atomic transaction (P0-1).

        This is the SINGLE helper used by both the normal completion path
        and the crash-recovery path so the two can never drift in their
        status semantics again.  ``outcome`` is validated by the caller
        (already a strict ``StageOutcome``).
        """
        needs_attn = outcome == "needs_attention"
        msg = attention_message if needs_attn else None
        if needs_attn and not msg:
            msg = f"{stage.stage_name} completed with publish failures"
        self._repo.complete_stage_with_artifacts(
            stage.stage_id, self._worker_id, output_key, artifacts,
            needs_attention=needs_attn, attention_message=msg,
        )

    def _insert_artifacts(
        self, result: StageResult, context: StageContext
    ) -> None:
        """Validate all StageResult artifact files (SHA-256, size, existence).

        Unlike the old implementation, this method does NOT commit to the
        database.  The caller is responsible for calling
        ``complete_stage_with_artifacts`` which handles artifact persistence
        and stage completion in a single atomic transaction.

        Raises ValueError or OSError if any artifact fails validation.
        """
        from app.task_engine.fingerprints import sha256_file as _sha

        for art in result.artifacts:
            art_path = Path(art.path)
            if not art_path.exists():
                raise ValueError(
                    f"Artifact path does not exist: {art.path} "
                    f"(stage={art.stage_name}, artifact_id={art.artifact_id})"
                )
            try:
                actual_sha = _sha(art_path)
            except OSError as exc:
                raise OSError(
                    f"Cannot compute SHA-256 for artifact {art.path}: {exc}"
                ) from exc
            if actual_sha != art.sha256:
                raise ValueError(
                    f"SHA-256 mismatch for artifact {art.path}: "
                    f"recorded={art.sha256[:12]}... actual={actual_sha[:12]}..."
                )
            actual_size = art_path.stat().st_size
            if actual_size != art.size_bytes:
                raise ValueError(
                    f"Size mismatch for artifact {art.path}: "
                    f"recorded={art.size_bytes} actual={actual_size}"
                )

    # ------------------------------------------------------------------
    # Orchestrator integration
    # ------------------------------------------------------------------

    def _initialize_pending_jobs(self) -> int:
        """Discover videos and create stages for any ``pending`` jobs.

        Returns the number of jobs initialised (0 if none were pending).
        Skips jobs whose directory does not exist (they will be handled
        by the API or by manual intervention).
        """
        from app.task_engine.orchestrator import advance_job, initialize_job

        rows = self._repo.conn.execute(
            "SELECT job_id, directory FROM task_jobs WHERE status='pending' LIMIT 10"
        ).fetchall()
        if not rows:
            return 0

        count = 0
        for row in rows:
            job_id = row["job_id"]
            directory = row["directory"]
            # Skip jobs whose directory does not exist — these were created
            # programmatically (e.g. in tests) and will be initialised by
            # whoever created them, or via a retry command.
            if not os.path.isdir(directory):
                continue
            try:
                initialize_job(self._repo, job_id)
                advance_job(self._repo, job_id)
                count += 1
            except Exception:
                self._repo.conn.execute(
                    "UPDATE task_jobs SET status='needs_attention', updated_at=? WHERE job_id=?",
                    (_iso(_utcnow()), job_id),
                )
                self._repo.conn.commit()
        return count

    def _run_stage(self, stage, now: datetime) -> bool:
        """Execute one stage: cancel check → adapter lookup → run → persist."""
        # 2. Check for a pending cancellation command for this job.
        if self._check_cancelled(stage, now):
            return True

        # 3. Look up the stage adapter.
        adapter = self._adapters.get(stage.stage_name)
        if adapter is None:
            error = StageError(
                "unknown_stage",
                f"No adapter configured for stage {stage.stage_name}",
                transient=False,
            )
            self._repo.fail_stage(stage.stage_id, self._worker_id, error)
            return True

        # 4-8. Resolve context, recover, run (with heartbeat), persist, commit.
        import threading

        context = None
        result = None
        heartbeat_stop = threading.Event()
        heartbeat_thread: threading.Thread | None = None

        # Phase 5: Per-stage lease_lost event — state does NOT leak
        # between _run_stage() calls.  The heartbeat thread and main
        # thread share ONLY this local Event, never a persisted attribute.
        lease_lost = threading.Event()

        try:
            context = self._build_context(stage)
            recovered = False
            if stage.attempt_count > 1 and self._try_recover(stage, context):
                recovered = True
                result = None  # No fresh run, but still need post-stage logic.

            if not recovered:
                # Start a background heartbeat thread that uses its own SQLite
                # connection to the SAME database file as the worker.
                # The connection is created *inside* the thread to satisfy
                # SQLite's per-thread requirement.
                db_path = self._db_path
                if db_path is None:
                    row = self._repo.conn.execute(
                        "PRAGMA database_list"
                    ).fetchone()
                    db_path = row["file"] if row else ":memory:"

                lease_sec = self._lease_seconds
                heartbeat_sec = self._heartbeat_seconds

                def _heartbeat_loop():
                    import time
                    own_conn = sqlite3.connect(db_path, timeout=30)
                    own_conn.execute("PRAGMA busy_timeout=30000")
                    own_conn.row_factory = sqlite3.Row
                    try:
                        stage_id = stage.stage_id
                        while not heartbeat_stop.wait(timeout=heartbeat_sec):
                            try:
                                own_conn.execute("BEGIN IMMEDIATE")
                                cur = own_conn.execute(
                                    "UPDATE task_stages SET lease_expires_at=? "
                                    "WHERE stage_id=? AND lease_owner=?"
                                    "  AND status IN ('leased','running')",
                                    (
                                        _iso(_utcnow() + timedelta(seconds=lease_sec)),
                                        stage_id,
                                        self._worker_id,
                                    ),
                                )
                                if cur.rowcount == 0:
                                    # Stage no longer owned by us or already
                                    # terminal — stop heartbeating and notify
                                    # the main thread that the lease is lost.
                                    own_conn.commit()
                                    heartbeat_stop.set()
                                    # Phase 5: per-stage event, not self._lease_lost
                                    lease_lost.set()
                                    return
                                own_conn.commit()
                            except Exception:
                                try:
                                    own_conn.rollback()
                                except Exception:
                                    pass
                    finally:
                        own_conn.close()

                heartbeat_thread = threading.Thread(
                    target=_heartbeat_loop, daemon=True
                )
                heartbeat_thread.start()

                result = adapter.run(context)
                heartbeat_stop.set()
                if heartbeat_thread is not None:
                    heartbeat_thread.join(timeout=5)

                self._save_result(context.work_dir, result, stage)
                # Validate artifacts (file existence, SHA-256, size) outside
                # the transaction to avoid holding the SQLite write lock during I/O.
                self._insert_artifacts(result, context)
                # P1-4: Check lease before committing — if the heartbeat thread
                # detected lease loss, we must not write to a stage we no longer own.
                # Phase 5: Uses per-stage lease_lost Event, not self._lease_lost.
                if lease_lost.is_set():
                    raise RuntimeError(
                        f"Lease lost for stage {stage.stage_id} — "
                        f"another worker may have claimed it"
                    )
                # P0-1: commit via the unified helper so the normal path and
                # crash recovery share one outcome/status semantics.
                from app.task_engine.stages import normalize_outcome
                outcome = normalize_outcome(getattr(result, "outcome", "succeeded"))
                attn_msg = (
                    self._attention_message(stage, result)
                    if outcome == "needs_attention" else None
                )
                self._commit_stage_result(
                    stage, result.output_key, result.artifacts,
                    outcome, attn_msg,
                )
        except Exception as exc:
            heartbeat_stop.set()
            # P1-4: Only fail the stage if we still hold the lease.
            # Phase 5: Uses per-stage lease_lost Event.
            if not lease_lost.is_set():
                error = classify_error(exc, stage.stage_name)
                try:
                    self._repo.fail_stage(stage.stage_id, self._worker_id, error)
                except Exception:
                    pass  # Fail-safe: if fail_stage also fails, just log
            else:
                # Lease was lost — do NOT write anything to the stage.
                pass

        # After stage completes, handle post-stage actions and try to advance the job.
        try:
            self._after_stage(stage, result)
            self._try_advance_job(stage)
        except Exception:
            pass  # Non-fatal — next run_once will retry advancement.

        return True

    def _after_stage(self, stage, result) -> None:
        """Post-stage actions after a stage completes.

        Currently a no-op: fan-out from rank_dedup to gif_clip stages is
        handled by the orchestrator's ``_ensure_gif_clip_stages``, which
        is called from ``advance_job`` after each stage completes.
        """
        pass

    def _try_advance_job(self, stage) -> None:
        """After a stage completes, advance its job (create next stages)."""
        from app.task_engine.orchestrator import advance_job

        video_row = self._repo.conn.execute(
            "SELECT job_id FROM task_videos WHERE video_id=?",
            (stage.video_id,),
        ).fetchone()
        if video_row is not None:
            advance_job(self._repo, video_row["job_id"])
