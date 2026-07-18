"""Import legacy batch queue/checkpoint state into the task engine.

Stem-to-directory association rule (deterministic, no disk stat-ing):
1. Queue jobs are processed in file order and deduplicated by canonical
   directory (os.path.normcase(abspath(directory)), same key the repository
   uses); the first job wins and later jobs for the same directory merge
   into it (their claimed "videos" stems are still honored).
2. A queue job may optionally list "videos": [stem, ...] to claim specific
   checkpoint stems.
3. Checkpoint stems not claimed by any job attach to the FIRST unique job
   directory.
4. If there are no queue jobs at all, a single fallback job is created for
   checkpoint["last_run"]["dir"], else for the explicit ``directory``
   argument (CLI: --directory); if both are absent and there are stems to
   place, ValueError is raised.

Checkpoint entries are classified by status, not by section: "ok" and
"dedup_skipped" are reusable (imported as succeeded materialize stages with
output_key "legacy-import"); "failed" and "timeout" import as pending
stages; any other status is skipped. Video paths are synthesized as
<directory>/<stem>.<first job extension, default .mp4> and are never
stat-ed. Missing queue/state files are treated as empty; a missing
checkpoint file raises FileNotFoundError.

Idempotency: the migration key is the SHA-256 (via canonical_hash) of the
three source file hashes. The key plus the full report JSON is inserted
into task_migrations in the SAME transaction as the imported rows; a
repeat call finds the key and returns the stored report verbatim (chosen
over deterministic recomputation because it is exact and trivially
consistent). Legacy source files are never renamed, truncated, or deleted;
timestamped byte-for-byte backups are written to backup_dir before the
write transaction opens. If the import fails, that attempt's backups are
left in place (safe — a retry writes a fresh timestamped set).
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.task_engine.fingerprints import canonical_hash, sha256_file
from app.task_engine.repository import _ACTIVE_JOB_STATUSES, _directory_key
from app.task_engine.repository import TaskRepository

REUSABLE_STATUSES = frozenset({"ok", "dedup_skipped"})
PENDING_STATUSES = frozenset({"failed", "timeout"})
WORKER_ID = "legacy-import"
OUTPUT_KEY = "legacy-import"
DEFAULT_EXTENSION = ".mp4"


@dataclass(frozen=True)
class ImportReport:
    migration_id: str
    jobs_created: int
    videos_reused: int
    videos_pending: int
    backups: tuple[str, ...]


@dataclass(frozen=True)
class PlannedVideo:
    stem: str
    fingerprint: str
    reusable: bool


@dataclass(frozen=True)
class PlannedJob:
    directory: str
    limit: int
    extensions: str
    legacy_job_id: str
    legacy_job_status: str
    videos: tuple[PlannedVideo, ...]


@dataclass(frozen=True)
class LegacyImportPlan:
    migration_id: str
    jobs: tuple[PlannedJob, ...]
    videos_reused: int
    videos_pending: int


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_json(path: Path) -> dict:
    with path.open(encoding="utf-8-sig") as f:
        data = json.load(f)
    return data if isinstance(data, dict) else {}


def _file_hash(path: Path) -> str:
    if not path.exists():
        return hashlib.sha256(b"").hexdigest()
    return sha256_file(path)


def _first_extension(extensions: str) -> str:
    for part in extensions.split(","):
        ext = part.strip().lower()
        if ext:
            return ext if ext.startswith(".") else f".{ext}"
    return DEFAULT_EXTENSION


def _checkpoint_videos(checkpoint: dict) -> dict[str, PlannedVideo]:
    videos: dict[str, PlannedVideo] = {}
    for section in ("completed", "retryable"):
        entries = checkpoint.get(section) or {}
        if not isinstance(entries, dict):
            continue
        for stem, info in entries.items():
            if stem in videos or not isinstance(info, dict):
                continue
            status = info.get("status")
            if status in REUSABLE_STATUSES:
                reusable = True
            elif status in PENDING_STATUSES:
                reusable = False
            else:
                continue
            videos[stem] = PlannedVideo(
                stem=stem,
                fingerprint=info.get("fingerprint") or "",
                reusable=reusable,
            )
    return videos


def plan_legacy_import(
    queue_path: Path,
    state_path: Path,
    checkpoint_path: Path,
    directory: str | None = None,
) -> LegacyImportPlan:
    queue_path = Path(queue_path)
    state_path = Path(state_path)
    checkpoint_path = Path(checkpoint_path)
    migration_id = canonical_hash({
        "kind": "legacy-task-state-import",
        "queue": _file_hash(queue_path),
        "state": _file_hash(state_path),
        "checkpoint": _file_hash(checkpoint_path),
    })

    queue = _load_json(queue_path) if queue_path.exists() else {}
    state = _load_json(state_path) if state_path.exists() else {}
    checkpoint = _load_json(checkpoint_path)

    # Historical batch_queue_state.json shape is {"status", "current_job_id",
    # "jobs": {job_id: {"status": ...}}}; older exports used "completed".
    state_jobs = state.get("jobs")
    if not isinstance(state_jobs, dict):
        state_jobs = state.get("completed")
    if not isinstance(state_jobs, dict):
        state_jobs = {}

    remaining = _checkpoint_videos(checkpoint)
    jobs: list[PlannedJob] = []
    job_index: dict[str, int] = {}
    claimed: list[list[PlannedVideo]] = []

    raw_jobs = queue.get("jobs") or []
    if not isinstance(raw_jobs, list):
        raw_jobs = []
    for raw in raw_jobs:
        if not isinstance(raw, dict) or not raw.get("directory"):
            continue
        directory = str(raw["directory"])
        key = _directory_key(directory)
        if key in job_index:
            index = job_index[key]
        else:
            index = len(jobs)
            job_index[key] = index
            legacy_job_id = str(raw.get("job_id") or "")
            status_info = state_jobs.get(legacy_job_id) or {}
            jobs.append(PlannedJob(
                directory=directory,
                limit=int(raw.get("limit") or 0),
                extensions=str(raw.get("extensions") or ""),
                legacy_job_id=legacy_job_id,
                legacy_job_status=str(status_info.get("status") or ""),
                videos=(),
            ))
            claimed.append([])
        for stem in raw.get("videos") or []:
            video = remaining.pop(str(stem), None)
            if video is not None:
                claimed[index].append(video)

    if not jobs and remaining:
        last_run = checkpoint.get("last_run") or {}
        fallback_dir = last_run.get("dir") if isinstance(last_run, dict) else None
        if not fallback_dir:
            fallback_dir = directory
        if not fallback_dir:
            raise ValueError(
                "no queue jobs and no checkpoint last_run.dir: "
                f"cannot place {len(remaining)} legacy video(s); "
                "pass the legacy video directory explicitly "
                "(CLI: --directory)"
            )
        jobs.append(PlannedJob(
            directory=str(fallback_dir),
            limit=0,
            extensions="",
            legacy_job_id="",
            legacy_job_status="",
            videos=(),
        ))
        claimed.append([])

    if jobs:
        claimed[0].extend(remaining.values())

    planned_jobs = tuple(
        PlannedJob(
            directory=job.directory,
            limit=job.limit,
            extensions=job.extensions,
            legacy_job_id=job.legacy_job_id,
            legacy_job_status=job.legacy_job_status,
            videos=tuple(claimed[i]),
        )
        for i, job in enumerate(jobs)
    )
    all_videos = [v for job in planned_jobs for v in job.videos]
    return LegacyImportPlan(
        migration_id=migration_id,
        jobs=planned_jobs,
        videos_reused=sum(1 for v in all_videos if v.reusable),
        videos_pending=sum(1 for v in all_videos if not v.reusable),
    )


def _make_backups(sources: list[Path], backup_dir: Path) -> tuple[str, ...]:
    existing = [p for p in sources if p.exists()]
    if not existing:
        return ()
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%f")
    backups = []
    for src in existing:
        dest = backup_dir / f"{src.name}.{stamp}.bak"
        shutil.copyfile(src, dest)
        backups.append(str(dest))
    return tuple(backups)


def _find_active_job_id(conn, directory_key: str) -> str | None:
    statuses = ",".join(f"'{s}'" for s in _ACTIVE_JOB_STATUSES)
    row = conn.execute(
        f"""SELECT job_id FROM task_jobs
            WHERE directory_key=? AND status IN ({statuses})
            ORDER BY created_at ASC, job_id ASC LIMIT 1""",
        (directory_key,),
    ).fetchone()
    return row["job_id"] if row is not None else None


def _append_event(conn, kind: str, payload: dict) -> None:
    conn.execute(
        "INSERT INTO task_events (kind, payload_json, created_at) VALUES (?,?,?)",
        (kind, json.dumps(payload), _utcnow_iso()),
    )


def _complete_stage(conn, stage_id: str, video_id: str, output_key: str) -> None:
    conn.execute(
        """UPDATE task_stages
           SET status='succeeded', output_key=?, lease_owner=NULL,
               lease_expires_at=NULL, retry_at=NULL, last_error_json=NULL,
               updated_at=?
           WHERE stage_id=?""",
        (output_key, _utcnow_iso(), stage_id),
    )
    _append_event(conn, "stage.completed", {
        "stage_id": stage_id,
        "video_id": video_id,
        "stage_name": "materialize",
        "worker_id": WORKER_ID,
        "output_key": output_key,
    })


def _import_job(conn, job: PlannedJob) -> bool:
    now = _utcnow_iso()
    job_id = _find_active_job_id(conn, _directory_key(job.directory))
    created = False
    if job_id is None:
        job_id = uuid.uuid4().hex
        config_json = json.dumps({
            "source": "legacy-import",
            "legacy_job_id": job.legacy_job_id,
            "legacy_job_status": job.legacy_job_status,
            "limit": job.limit,
            "extensions": job.extensions,
        }, sort_keys=True)
        conn.execute(
            """INSERT INTO task_jobs
               (job_id, directory, directory_key, config_json, job_limit,
                extensions, status, created_at, updated_at)
               VALUES (?,?,?,?,?,?, 'pending', ?, ?)""",
            (job_id, job.directory, _directory_key(job.directory),
             config_json, job.limit, job.extensions, now, now),
        )
        created = True

    extension = _first_extension(job.extensions)
    base_dir = job.directory.rstrip("/\\")
    for video in job.videos:
        path = f"{base_dir}/{video.stem}{extension}"
        row = conn.execute(
            "SELECT video_id FROM task_videos WHERE job_id=? AND path=?",
            (job_id, path),
        ).fetchone()
        if row is None:
            video_id = uuid.uuid4().hex
            conn.execute(
                """INSERT INTO task_videos
                   (video_id, job_id, path, fingerprint, status,
                    created_at, updated_at)
                   VALUES (?,?,?,?, 'pending', ?, ?)""",
                (video_id, job_id, path, video.fingerprint, now, now),
            )
        else:
            video_id = row["video_id"]

        input_key = f"legacy:{video.fingerprint or video.stem}"
        stage = conn.execute(
            """SELECT stage_id, status FROM task_stages
               WHERE video_id=? AND stage_name='materialize'
                 AND COALESCE(clip_id, '')='' AND input_key=?""",
            (video_id, input_key),
        ).fetchone()
        if stage is None:
            stage_id = uuid.uuid4().hex
            status = "succeeded" if video.reusable else "pending"
            conn.execute(
                """INSERT INTO task_stages
                   (stage_id, video_id, stage_name, clip_id, input_key,
                    output_key, status, attempt_count, created_at, updated_at)
                   VALUES (?,?, 'materialize', NULL, ?, ?, ?, 0, ?, ?)""",
                (stage_id, video_id, input_key,
                 OUTPUT_KEY if video.reusable else None, status, now, now),
            )
            if video.reusable:
                _append_event(conn, "stage.completed", {
                    "stage_id": stage_id,
                    "video_id": video_id,
                    "stage_name": "materialize",
                    "worker_id": WORKER_ID,
                    "output_key": OUTPUT_KEY,
                })
        elif video.reusable and stage["status"] != "succeeded":
            _complete_stage(conn, stage["stage_id"], video_id, OUTPUT_KEY)
    return created


def _stored_report(report_json: str) -> ImportReport:
    stored = json.loads(report_json)
    return ImportReport(
        migration_id=stored["migration_id"],
        jobs_created=stored["jobs_created"],
        videos_reused=stored["videos_reused"],
        videos_pending=stored["videos_pending"],
        backups=tuple(stored["backups"]),
    )


def _select_stored_report(conn, migration_id: str) -> ImportReport | None:
    row = conn.execute(
        "SELECT report_json FROM task_migrations WHERE migration_id=?",
        (migration_id,),
    ).fetchone()
    return _stored_report(row["report_json"]) if row is not None else None


def import_legacy_state(
    repo: TaskRepository,
    *,
    queue_path: Path,
    state_path: Path,
    checkpoint_path: Path,
    backup_dir: Path,
    directory: str | None = None,
) -> ImportReport:
    queue_path = Path(queue_path)
    state_path = Path(state_path)
    checkpoint_path = Path(checkpoint_path)
    backup_dir = Path(backup_dir)
    plan = plan_legacy_import(queue_path, state_path, checkpoint_path,
                              directory=directory)

    conn = repo.conn
    stored = _select_stored_report(conn, plan.migration_id)
    if stored is not None:
        return stored

    backups = _make_backups([queue_path, state_path, checkpoint_path], backup_dir)

    jobs_created = 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        # Re-check inside the write transaction so a concurrent importer
        # cannot pass the pre-transaction check and duplicate the import.
        stored = _select_stored_report(conn, plan.migration_id)
        if stored is not None:
            conn.rollback()
            return stored
        for job in plan.jobs:
            if _import_job(conn, job):
                jobs_created += 1
        report = ImportReport(
            migration_id=plan.migration_id,
            jobs_created=jobs_created,
            videos_reused=plan.videos_reused,
            videos_pending=plan.videos_pending,
            backups=backups,
        )
        conn.execute(
            """INSERT INTO task_migrations (migration_id, report_json, applied_at)
               VALUES (?,?,?)""",
            (plan.migration_id, json.dumps({
                "migration_id": report.migration_id,
                "jobs_created": report.jobs_created,
                "videos_reused": report.videos_reused,
                "videos_pending": report.videos_pending,
                "backups": list(report.backups),
            }), _utcnow_iso()),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        # Lost a race against a concurrent importer: the partial unique
        # index on task_migrations.migration_id rejected our insert.
        conn.rollback()
        stored = _select_stored_report(conn, plan.migration_id)
        if stored is not None:
            return stored
        raise
    except Exception:
        conn.rollback()
        raise
    return report
