"""Job lifecycle orchestrator — wires job creation, video discovery, stage chain, and state aggregation.

The orchestrator is the single authority for transitioning a job through its
lifecycle.  The API only creates the pending job envelope; the orchestrator
(typically called from the worker loop) initialises it, drives stage execution,
and aggregates terminal states.

Flow per job::

    pending (created by API)
      -> discovering (orchestrator scans directory, creates stages)
      -> running (stages are being executed)
      -> completed | failed | needs_attention | cancelled
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from app.task_engine.fingerprints import fingerprint_video
from app.task_engine.models import StageName
from app.task_engine.repository import TaskRepository

# ---------------------------------------------------------------------------
# Video discovery
# ---------------------------------------------------------------------------

_STAGE_ORDER: tuple[StageName, ...] = (
    "discover",
    "sample",
    "vlm",
    "refine",
    "synthesize",
    "rank_dedup",
    "gif_clip",
    "materialize",
)

_NEXT_STAGE: dict[StageName, StageName | None] = {}
for i, name in enumerate(_STAGE_ORDER):
    _NEXT_STAGE[name] = _STAGE_ORDER[i + 1] if i + 1 < len(_STAGE_ORDER) else None

_TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled", "needs_attention"})


def discover_videos(directory: str, extensions: str | None = None) -> list[str]:
    """Discover video files in a directory, sorted by name for stability."""
    raw = extensions or ""
    wanted = _parse_extensions(raw) if raw.strip() else {".mp4", ".mkv", ".avi", ".mov", ".webm", ".ts"}
    root = Path(directory)
    if not root.is_dir():
        return []
    files: list[str] = []
    for child in sorted(root.iterdir()):
        if child.is_file() and (not wanted or child.suffix.lower() in wanted):
            files.append(str(child.resolve()))
    return files


def _parse_extensions(extensions: str) -> set[str]:
    return {
        f".{ext.strip().lower().lstrip('.')}"
        for ext in extensions.split(",")
        if ext.strip()
    }


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def initialize_job(repo: TaskRepository, job_id: str) -> list[str]:
    """Discover videos for a ``pending`` job and create video + stage rows.

    Returns the list of discovered video paths.  Sets job status to ``running``
    if videos were found, or ``succeeded`` (empty directory).

    If the job's ``config_json`` contains a ``video_paths`` list, discovery is
    scoped to those specific paths (used by Quality Lab single-video items).

    Safe to call multiple times — already-initialised jobs are skipped.
    """
    job = repo.conn.execute(
        "SELECT status, directory, extensions, job_limit, config_json FROM task_jobs WHERE job_id=?",
        (job_id,),
    ).fetchone()
    if job is None:
        raise ValueError(f"Job not found: {job_id}")
    if job["status"] != "pending":
        return _videos_for_job(repo, job_id)

    directory = job["directory"]
    extensions = job["extensions"] or ""
    job_limit = job["job_limit"] or 0

    videos = discover_videos(directory, extensions)

    # Honour the video_paths allowlist from the job config.
    config = json.loads(job["config_json"]) if job["config_json"] else {}
    video_paths = config.get("video_paths") or []
    if video_paths:
        abs_paths = {os.path.abspath(p).replace("\\", "/").lower() for p in video_paths}
        videos = [v for v in videos if v.replace("\\", "/").lower() in abs_paths]

    if job_limit and job_limit < len(videos):
        videos = videos[:job_limit]

    if not videos:
        _set_job_status(repo, job_id, "succeeded")
        _write_event(repo, "job.completed", {"job_id": job_id, "reason": "empty_directory", "video_count": 0})
        return []

    # Compute cheap fingerprints during initialisation.
    _set_job_status(repo, job_id, "running")
    for path in videos:
        fp = fingerprint_video(path) if os.path.isfile(path) else ""
        video = repo.add_video(job_id, path, fp)
        repo.ensure_stage(video.video_id, "discover", f"input:{os.path.basename(path)}")

    _write_event(repo, "job.initialized", {"job_id": job_id, "video_count": len(videos), "directory": directory})
    return videos


def advance_job(repo: TaskRepository, job_id: str) -> str:
    """Check each video's progress and create next stages where needed.

    Returns the current job status after advancing.
    """
    job = repo.conn.execute(
        "SELECT status FROM task_jobs WHERE job_id=?",
        (job_id,),
    ).fetchone()
    if job is None:
        raise ValueError(f"Job not found: {job_id}")

    current_status = job["status"]

    # Check for cancellation command (always, even for terminal jobs).
    cancel = repo.conn.execute(
        "SELECT command_id FROM task_commands WHERE job_id=? AND kind='cancel' AND status='pending' LIMIT 1",
        (job_id,),
    ).fetchone()
    if cancel is not None:
        _cancel_job(repo, job_id, cancel["command_id"])
        return "cancelled"

    # Check for retry command (always, even for terminal jobs — retry
    # should reset a terminal job back to running).
    retry = repo.conn.execute(
        "SELECT command_id FROM task_commands WHERE job_id=? AND kind='retry' AND status='pending' LIMIT 1",
        (job_id,),
    ).fetchone()
    if retry is not None:
        _retry_job(repo, job_id, retry["command_id"])
        # After retry, the job may be back to running. Re-read status.
        job = repo.conn.execute(
            "SELECT status FROM task_jobs WHERE job_id=?", (job_id,),
        ).fetchone()
        current_status = job["status"] if job else current_status

    if current_status in _TERMINAL_STATES:
        return current_status

    # For each video, create the next pending stage if the current one succeeded.
    videos = repo.conn.execute(
        "SELECT video_id, status FROM task_videos WHERE job_id=? ORDER BY path ASC",
        (job_id,),
    ).fetchall()

    for v in videos:
        if v["status"] not in _TERMINAL_STATES and v["status"] in ("running", "pending", "leased"):
            _advance_video_stages(repo, v["video_id"], job_id)

    # Re-aggregate AFTER advancing: _aggregate_video_status may have moved
    # videos into a terminal state during the loop above, so the terminal
    # check must read fresh video statuses rather than the stale snapshot.
    videos = repo.conn.execute(
        "SELECT video_id, status FROM task_videos WHERE job_id=? ORDER BY path ASC",
        (job_id,),
    ).fetchall()

    all_terminal = True
    has_failure = False
    has_attention = False
    for v in videos:
        vstatus = v["status"]
        if vstatus in _TERMINAL_STATES:
            if vstatus == "failed":
                has_failure = True
            elif vstatus == "needs_attention":
                has_attention = True
            continue
        all_terminal = False

    # Re-read current status after advancing stages.
    job = repo.conn.execute(
        "SELECT status FROM task_jobs WHERE job_id=?",
        (job_id,),
    ).fetchone()
    current_status = job["status"]

    if all_terminal:
        if has_attention:
            final = "needs_attention"
        elif has_failure:
            final = "needs_attention"
        else:
            final = "succeeded"
        _set_job_status(repo, job_id, final)
        _write_event(repo, f"job.{final}", {"job_id": job_id})
        return final

    return current_status


def _advance_video_stages(repo: TaskRepository, video_id: str, job_id: str) -> None:
    """Create the next stage for a video when its current stage succeeded.

    Each stage creates only its immediate successor.  ``rank_dedup`` is
    special: the worker creates per-clip ``gif_clip`` stages after
    reading the clip manifest, so this function merely notes that
    rank_dedup completed.  ``materialize`` is gated on all ``gif_clip``
    stages reaching a terminal state.
    """
    stages = repo.conn.execute(
        "SELECT s.stage_name, s.status FROM task_stages s WHERE s.video_id=? ORDER BY s.created_at ASC",
        (video_id,),
    ).fetchall()

    if not stages:
        path_row = repo.conn.execute(
            "SELECT path FROM task_videos WHERE video_id=?",
            (video_id,),
        ).fetchone()
        input_key = f"input:{os.path.basename(path_row['path'])}" if path_row else "input:unknown"
        repo.ensure_stage(video_id, "discover", input_key)
        return

    last_completed = None
    for s in stages:
        if s["status"] == "succeeded":
            last_completed = s["stage_name"]

    if last_completed is not None:
        next_stage = _NEXT_STAGE.get(last_completed)
        if next_stage is not None:
            # For rank_dedup -> gif_clip: the worker creates per-clip
            # gif_clip stages after reading the clip manifest.  Do NOT
            # create a single gif_clip here.
            if last_completed == "rank_dedup":
                # Emit an event so the worker knows to create gif_clip stages.
                # Also try fallback: read artifacts and create gif_clip stages
                # if the worker hasn't done it yet.
                _write_event(repo, "rank_dedup.completed", {
                    "video_id": video_id, "job_id": job_id,
                    "action": "create_gif_clip_stages",
                })
                try:
                    _ensure_gif_clip_stages(repo, video_id, job_id)
                except ValueError as exc:
                    # P1-3: If the rank_dedup stage is already succeeded but
                    # its manifest is permanently invalid/missing, this is a
                    # hard error.  Aggregate the video and job to
                    # needs_attention rather than silently passing.
                    rank_row = repo.conn.execute(
                        "SELECT status FROM task_stages WHERE video_id=? AND stage_name='rank_dedup'",
                        (video_id,),
                    ).fetchone()
                    if rank_row and rank_row["status"] == "succeeded":
                        _write_event(repo, "rank_dedup.manifest_error", {
                            "video_id": video_id,
                            "job_id": job_id,
                            "error": str(exc),
                            "severity": "needs_attention",
                        })
                        repo.conn.execute(
                            "UPDATE task_videos SET status='needs_attention', updated_at=? WHERE video_id=?",
                            (_now_iso(), video_id),
                        )
                        repo.conn.commit()
                    else:
                        # Manifest not yet available — retry on next advance_job call.
                        pass
                else:
                    _check_create_materialize(repo, video_id, job_id)
            elif last_completed == "gif_clip":
                # After each gif_clip completes, check whether all
                # gif_clip stages are terminal; if so, create materialize.
                _check_create_materialize(repo, video_id, job_id)
            else:
                next_exists = repo.conn.execute(
                    "SELECT 1 FROM task_stages WHERE video_id=? AND stage_name=? LIMIT 1",
                    (video_id, next_stage),
                ).fetchone()
                if not next_exists:
                    repo.ensure_stage(video_id, next_stage, f"from:{last_completed}")
                    _write_event(repo, "stage.created", {
                        "video_id": video_id, "job_id": job_id,
                        "stage_name": next_stage,
                    })

    _aggregate_video_status(repo, video_id, job_id)


def _ensure_gif_clip_stages(
    repo: TaskRepository, video_id: str, job_id: str
) -> None:
    """Ensure gif_clip stages exist after rank_dedup completes.

    Reads the ``rank_dedup_manifest`` artifact from ``task_artifacts``
    (the database is the single source of truth).  Creates one gif_clip
    stage per clip_id found.

    If the manifest declares zero clips, creates a materialize stage
    directly (no gif_clip placeholder).

    Raises ValueError if the manifest is missing, unreadable, or
    does not contain the required fields.
    """
    existing = repo.conn.execute(
        "SELECT COUNT(*) FROM task_stages WHERE video_id=? AND stage_name='gif_clip'",
        (video_id,),
    ).fetchone()[0]
    if existing > 0:
        return

    # Read the rank_dedup_manifest from task_artifacts (not from work_dir).
    art_rows = repo.conn.execute(
        """SELECT * FROM task_artifacts
           WHERE video_id=? AND artifact_kind='rank_dedup_manifest'
           ORDER BY created_at DESC LIMIT 1""",
        (video_id,),
    ).fetchall()

    if not art_rows:
        raise ValueError(
            f"No rank_dedup_manifest artifact found for video {video_id}"
        )

    art_row = art_rows[0]
    manifest_path = Path(art_row["path"])
    if not manifest_path.exists():
        raise ValueError(
            f"rank_dedup_manifest file not found at {manifest_path} "
            f"for video {video_id}"
        )

    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        raise ValueError(
            f"Cannot read rank_dedup_manifest at {manifest_path}: {exc}"
        ) from exc

    # Validate manifest structure.
    from app.task_engine.artifacts import validate_manifest_json

    validate_manifest_json(
        json.dumps(manifest).encode("utf-8"),
        "rank_dedup_manifest",
        expected_stage="rank_dedup",
    )

    clips = manifest.get("clips", [])
    clip_count = manifest.get("clip_count", len(clips))

    if clip_count == 0 or not clips:
        # Zero-clip result is valid — emit an event and create
        # materialize directly so the chain can terminate cleanly.
        _write_event(repo, "rank_dedup.completed", {
            "video_id": video_id, "job_id": job_id,
            "clip_count": 0, "action": "zero_clips_materialize_direct",
        })
        # Create materialize directly, with input_key derived from the
        # rank_dedup artifact identity.
        input_key = f"from:rank_dedup:{art_row['artifact_id']}"
        repo.ensure_stage(video_id, "materialize", input_key)
        return

    for clip in clips:
        cid = clip.get("clip_id", "")
        if not cid:
            raise ValueError(
                f"Clip in rank_dedup manifest has no clip_id: {clip}"
            )
        repo.ensure_stage(
            video_id, "gif_clip", f"from:rank_dedup:clip:{cid}", clip_id=cid
        )

    _write_event(repo, "rank_dedup.completed", {
        "video_id": video_id, "job_id": job_id,
        "clip_count": len(clips), "action": "create_gif_clip_stages",
    })


def _check_create_materialize(
    repo: TaskRepository, video_id: str, job_id: str
) -> None:
    """Create a ``materialize`` stage if all ``gif_clip`` stages are terminal.

    Only creates materialize when at least one gif_clip stage existed and
    all of them have reached a terminal state (succeeded / failed /
    cancelled / needs_attention).  Does nothing if no gif_clip stages
    exist yet (they are created by the worker after rank_dedup).
    """
    gif_clips = repo.conn.execute(
        "SELECT status FROM task_stages WHERE video_id=? AND stage_name='gif_clip'",
        (video_id,),
    ).fetchall()

    if not gif_clips:
        # No gif_clip stages yet — worker has not created them or
        # rank_dedup produced zero clips.
        return

    all_terminal = all(r["status"] in _TERMINAL_STATES for r in gif_clips)
    if not all_terminal:
        return

    existing = repo.conn.execute(
        "SELECT 1 FROM task_stages WHERE video_id=? AND stage_name='materialize' LIMIT 1",
        (video_id,),
    ).fetchone()

    if not existing:
        # Find a succeeded gif_clip to derive input_key from.
        gif_succeeded = repo.conn.execute(
            "SELECT stage_id FROM task_stages WHERE video_id=? AND stage_name='gif_clip' AND status='succeeded' LIMIT 1",
            (video_id,),
        ).fetchone()
        input_key = (
            f"from:gif_clip:{gif_succeeded['stage_id']}"
            if gif_succeeded
            else "from:gif_clip"
        )
        repo.ensure_stage(video_id, "materialize", input_key)
        _write_event(repo, "stage.created", {
            "video_id": video_id, "job_id": job_id,
            "stage_name": "materialize",
        })


def _aggregate_video_status(repo: TaskRepository, video_id: str, job_id: str) -> None:
    """Set video status from its stages.

    Aggregation priority:
      1. needs_attention / failed  (highest)
      2. cancelled
      3. running / leased / retry_wait / pending
      4. succeeded  (lowest — only when ALL required stages succeeded)

    A video is only ``succeeded`` when ALL gif_clip stages have succeeded
    (or no gif_clip stages exist and all other stages succeeded).
    Partial GIF success (some succeeded, some cancelled/failed) =
    ``needs_attention``.
    """
    if hasattr(repo.conn, "in_transaction") and repo.conn.in_transaction:
        repo.conn.commit()

    # --- Priority 1: failures / needs_attention ---
    gif_clip_failures = repo.conn.execute(
        """SELECT COUNT(*) FROM task_stages
           WHERE video_id=? AND stage_name='gif_clip'
             AND status IN ('failed', 'needs_attention', 'cancelled')""",
        (video_id,),
    ).fetchone()[0]

    if gif_clip_failures > 0:
        # Check if there are also succeeded gif_clips (partial success).
        gif_clip_succeeded = repo.conn.execute(
            """SELECT COUNT(*) FROM task_stages
               WHERE video_id=? AND stage_name='gif_clip' AND status='succeeded'""",
            (video_id,),
        ).fetchone()[0]
        repo.conn.execute(
            "UPDATE task_videos SET status='needs_attention', updated_at=? WHERE video_id=?",
            (_now_iso(), video_id),
        )
        repo.conn.commit()
        return

    # Check for any needs_attention/failed stages (not just gif_clip).
    attention_count = repo.conn.execute(
        """SELECT COUNT(*) FROM task_stages
           WHERE video_id=? AND status IN ('failed', 'needs_attention')""",
        (video_id,),
    ).fetchone()[0]
    if attention_count > 0:
        repo.conn.execute(
            "UPDATE task_videos SET status='needs_attention', updated_at=? WHERE video_id=?",
            (_now_iso(), video_id),
        )
        repo.conn.commit()
        return

    # --- Priority 2: cancelled ---
    cancelled_count = repo.conn.execute(
        "SELECT COUNT(*) FROM task_stages WHERE video_id=? AND status='cancelled'",
        (video_id,),
    ).fetchone()[0]
    if cancelled_count > 0:
        repo.conn.execute(
            "UPDATE task_videos SET status='cancelled', updated_at=? WHERE video_id=?",
            (_now_iso(), video_id),
        )
        repo.conn.commit()
        return

    # --- Priority 3/4: running vs succeeded ---
    active_count = repo.conn.execute(
        """SELECT COUNT(*) FROM task_stages
           WHERE video_id=?
             AND status NOT IN ('succeeded', 'failed', 'cancelled', 'needs_attention')""",
        (video_id,),
    ).fetchone()[0]

    if active_count == 0:
        # All stages terminal and none failed/cancelled/attention.
        repo.conn.execute(
            "UPDATE task_videos SET status='succeeded', updated_at=? WHERE video_id=?",
            (_now_iso(), video_id),
        )
    else:
        repo.conn.execute(
            "UPDATE task_videos SET status='running', updated_at=? WHERE video_id=?",
            (_now_iso(), video_id),
        )
    repo.conn.commit()


# ---------------------------------------------------------------------------
# Cancel / Retry — each function owns its own transaction.
# Use _set_job_status_unsafe() to avoid double-commit.
# ---------------------------------------------------------------------------


def _cancel_job(repo: TaskRepository, job_id: str, command_id: str) -> None:
    """Cancel all non-terminal stages for a job.  Owns its own transaction."""
    _ensure_no_open_txn(repo)
    repo.conn.execute("BEGIN IMMEDIATE")
    try:
        now = _now_iso()
        repo.conn.execute(
            "UPDATE task_stages SET status='cancelled', updated_at=? WHERE video_id IN "
            "(SELECT video_id FROM task_videos WHERE job_id=?) AND status NOT IN ('succeeded','failed','cancelled','needs_attention')",
            (now, job_id),
        )
        repo.conn.execute(
            "UPDATE task_videos SET status='cancelled', updated_at=? WHERE job_id=? AND status NOT IN ('succeeded','failed','cancelled','needs_attention')",
            (now, job_id),
        )
        repo.conn.execute("UPDATE task_jobs SET status='cancelled', updated_at=? WHERE job_id=?", (now, job_id))
        repo.conn.execute("UPDATE task_commands SET status='completed' WHERE command_id=?", (command_id,))
        repo.conn.execute(
            "INSERT INTO task_events (kind, payload_json, created_at) VALUES (?,?,?)",
            ("job.cancelled", json.dumps({"job_id": job_id}), now),
        )
        repo.conn.commit()
    except Exception:
        repo.conn.rollback()
        raise


def _retry_job(repo: TaskRepository, job_id: str, command_id: str) -> None:
    """Reset failed/needs_attention stages and videos to pending.  Owns its own transaction."""
    _ensure_no_open_txn(repo)
    repo.conn.execute("BEGIN IMMEDIATE")
    try:
        now = _now_iso()
        repo.conn.execute(
            "UPDATE task_stages SET status='pending', lease_owner=NULL, lease_expires_at=NULL, "
            "attempt_count=0, last_error_json=NULL, updated_at=? WHERE video_id IN "
            "(SELECT video_id FROM task_videos WHERE job_id=?) AND status IN ('failed','needs_attention')",
            (now, job_id),
        )
        repo.conn.execute(
            "UPDATE task_videos SET status='pending', updated_at=? WHERE job_id=? AND status IN ('failed','needs_attention')",
            (now, job_id),
        )
        repo.conn.execute("UPDATE task_jobs SET status='running', updated_at=? WHERE job_id=?", (now, job_id))
        repo.conn.execute("UPDATE task_commands SET status='completed' WHERE command_id=?", (command_id,))
        repo.conn.execute(
            "INSERT INTO task_events (kind, payload_json, created_at) VALUES (?,?,?)",
            ("job.retrying", json.dumps({"job_id": job_id}), now),
        )
        repo.conn.commit()
    except Exception:
        repo.conn.rollback()
        raise


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _set_job_status(repo: TaskRepository, job_id: str, status: str) -> None:
    _ensure_no_open_txn(repo)
    repo.conn.execute("UPDATE task_jobs SET status=?, updated_at=? WHERE job_id=?", (status, _now_iso(), job_id))
    repo.conn.commit()


def _write_event(repo: TaskRepository, kind: str, payload: dict) -> None:
    _ensure_no_open_txn(repo)
    repo.conn.execute("INSERT INTO task_events (kind, payload_json, created_at) VALUES (?,?,?)", (kind, json.dumps(payload), _now_iso()))
    repo.conn.commit()


def _ensure_no_open_txn(repo: TaskRepository) -> None:
    """Close any pending implicit transaction if one exists."""
    if hasattr(repo.conn, "in_transaction") and repo.conn.in_transaction:
        repo.conn.commit()


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")


def _videos_for_job(repo: TaskRepository, job_id: str) -> list[str]:
    return [
        row["path"]
        for row in repo.conn.execute(
            "SELECT path FROM task_videos WHERE job_id=? ORDER BY path ASC",
            (job_id,),
        ).fetchall()
    ]
