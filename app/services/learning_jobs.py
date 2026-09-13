"""Background learning jobs: eligibility, atomic leases, and recoverable stages.

The worker advances one verifiable stage per run (SNAPSHOT -> TRAIN ->
QUALIFY -> COMPLETE). Scientific outcomes (NOT_QUALIFIED) complete the job;
execution failures retry with backoff; input or artifact hash drift stops the
job as NEEDS_ATTENTION. The worker can only create DRAFT models and
qualification results -- never PUBLISHED, deployments, or config changes.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, cast

from app.services.curve_preference import canonical_json_sha256

DEFAULT_LEARNING_POLICY: dict[str, Any] = {
    "min_new_effective_candidates": 100,
    "min_new_source_groups": 20,
    "min_positive_candidates": 20,
    "min_negative_candidates": 20,
    "min_interval_hours": 24,
    "max_concurrent_jobs": 1,
    "promotion_policy": "manual",
    "lease_seconds": 1800,
    "retry_delays_s": (30, 120, 600),
    "max_attempts": 3,
}

_RETRY_DELAYS_S = (30, 120, 600)


@dataclass(frozen=True)
class LearningStats:
    new_effective_candidates: int
    new_source_groups: int
    positive_candidates: int
    negative_candidates: int
    hours_since_last_job: float | None


@dataclass(frozen=True)
class EnqueueResult:
    status: str
    job_id: str | None
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class LearningJob:
    job_id: str
    stage: str
    owner: str
    input_snapshot_sha256: str
    policy_sha256: str
    lease_token: str = ""


@dataclass(frozen=True)
class LearningStageResult:
    outcome: str
    next_stage: str | None
    artifact_path: str | None
    artifact_sha256: str | None
    error_code: str | None


def learning_eligibility(
    stats: Mapping[str, Any],
    policy: Mapping[str, Any] | None = None,
) -> list[str]:
    """Check whether sufficient new feedback, diversity, and interval elapsed."""
    pol = dict(DEFAULT_LEARNING_POLICY)
    if policy:
        pol.update(policy)

    reasons: list[str] = []
    if int(stats.get("new_effective_candidates", 0)) < int(pol["min_new_effective_candidates"]):
        reasons.append("insufficient_new_feedback")

    if int(stats.get("new_source_groups", 0)) < int(pol["min_new_source_groups"]):
        reasons.append("insufficient_new_sources")

    if int(stats.get("positive_candidates", 0)) < int(pol["min_positive_candidates"]):
        reasons.append("insufficient_positive_feedback")

    if int(stats.get("negative_candidates", 0)) < int(pol["min_negative_candidates"]):
        reasons.append("insufficient_negative_feedback")

    hours_since = stats.get("hours_since_last_job")
    if hours_since is not None and float(hours_since) < float(pol["min_interval_hours"]):
        reasons.append("interval_too_short")

    return reasons


def learning_policy_from_config() -> tuple[bool, dict[str, Any]]:
    """Read the frozen ``curve_learning`` config section.

    Returns ``(enabled, policy)``. ``enabled`` defaults to False: learning
    queueing is OFF unless the frozen config explicitly enables it.
    """
    try:
        from app.config import get

        section = get("curve_learning", None)
    except Exception:
        section = None
    if isinstance(section, dict):
        policy = {
            key: value
            for key, value in section.items()
            if key != "enabled" and key != "promotion_policy"
        }
        enabled = bool(section.get("enabled", False))
    else:
        enabled = False
        policy = {}
    return enabled, policy


def _effective_event_watermark(conn: sqlite3.Connection) -> tuple[str, dict[str, int]]:
    """Hash the current effective scoring-event set and count classes."""
    from app.services.preference_events import load_latest_scoring_events

    previous_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        events = load_latest_scoring_events(conn)
    finally:
        conn.row_factory = previous_factory

    entries = []
    counts = {"positive": 0, "negative": 0}
    sources: set[str] = set()
    for key in sorted(events):
        event = events[key]
        payload = {
            "event_id": event.get("event_id"),
            "target_type": event.get("target_type"),
            "target_id": event.get("target_id"),
            "rating": event.get("rating"),
            "source_video_sha256": event.get("source_video_sha256"),
            "created_at": event.get("created_at"),
            "event_kind": event.get("event_kind"),
            "supersedes_event_id": event.get("supersedes_event_id"),
        }
        entries.append(canonical_json_sha256(payload))
        rating = str(event.get("rating"))
        if rating in ("like", "favorite"):
            counts["positive"] += 1
            sources.add(str(event.get("source_video_sha256")))
        elif rating == "dislike":
            counts["negative"] += 1
            sources.add(str(event.get("source_video_sha256")))
    watermark = hashlib.sha256(
        json.dumps(entries, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    counts["source_groups"] = len(sources)
    return watermark, counts


def _last_succeeded_job_created_at(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT created_at FROM learning_jobs WHERE status = 'SUCCEEDED' "
        "ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    return row[0] if row else None


def collect_learning_stats(
    conn: sqlite3.Connection,
    policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Compute real eligibility stats from events since the last success."""
    _ = policy  # thresholds are applied by learning_eligibility
    from app.services.preference_events import load_latest_scoring_events

    previous_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        events = load_latest_scoring_events(conn)
    finally:
        conn.row_factory = previous_factory

    last_created_at = _last_succeeded_job_created_at(conn)
    since_epoch = 0.0
    if last_created_at:
        try:
            since_epoch = datetime.fromisoformat(
                str(last_created_at).replace("Z", "+00:00")
            ).timestamp()
        except ValueError:
            since_epoch = 0.0

    new_candidates = 0
    new_sources: set[str] = set()
    positive = 0
    negative = 0
    for _key in sorted(events):
        event = events[_key]
        rating = str(event.get("rating"))
        if rating not in ("like", "favorite", "dislike"):
            continue
        created_at = str(event.get("created_at") or "")
        try:
            event_epoch = datetime.fromisoformat(
                created_at.replace("Z", "+00:00")
            ).timestamp()
        except ValueError:
            event_epoch = 0.0
        if event_epoch <= since_epoch:
            continue
        new_candidates += 1
        new_sources.add(str(event.get("source_video_sha256")))
        if rating in ("like", "favorite"):
            positive += 1
        else:
            negative += 1

    hours_since: float | None = None
    if last_created_at:
        hours_since = max(
            0.0,
            (time.time() - since_epoch) / 3600.0,
        )

    return {
        "new_effective_candidates": new_candidates,
        "new_source_groups": len(new_sources),
        "positive_candidates": positive,
        "negative_candidates": negative,
        "hours_since_last_job": hours_since,
    }


def enqueue_learning(
    conn: sqlite3.Connection,
    input_snapshot_sha256: str,
    policy_sha256: str,
    job_type: str = "curve_preference",
) -> str:
    """Enqueue a learning job uniquely identified by input snapshot + policy hash."""
    job_id = hashlib.sha256(
        f"{input_snapshot_sha256}:{policy_sha256}".encode("utf-8")
    ).hexdigest()[:32]

    conn.execute(
        """
        INSERT OR IGNORE INTO learning_jobs (
            job_id, job_type, input_snapshot_sha256, policy_sha256, stage, status
        ) VALUES (?, ?, ?, ?, 'SNAPSHOT', 'PENDING')
        """,
        (job_id, job_type, input_snapshot_sha256, policy_sha256),
    )
    conn.commit()
    return job_id


def maybe_enqueue_learning(
    conn: sqlite3.Connection,
    policy: Mapping[str, Any] | None = None,
) -> EnqueueResult:
    """Compute real eligibility stats and enqueue when they justify training."""
    stats = collect_learning_stats(conn, policy)
    reasons = learning_eligibility(stats, policy)
    if reasons:
        return EnqueueResult("INELIGIBLE", None, tuple(reasons))

    watermark, _counts = _effective_event_watermark(conn)
    pol = dict(DEFAULT_LEARNING_POLICY)
    if policy:
        pol.update(policy)
    pol["promotion_policy"] = "manual"  # never configurable to auto
    policy_sha256 = canonical_json_sha256(pol)

    job_id = enqueue_learning(conn, watermark, policy_sha256)
    existing = conn.execute(
        "SELECT created_at, updated_at FROM learning_jobs WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    status = "EXISTING" if existing and existing[0] != existing[1] else "ENQUEUED"
    return EnqueueResult(status, job_id, ())


def claim_learning_job(
    conn: sqlite3.Connection,
    owner: str,
    now_epoch: float,
    lease_seconds: float = 1800,
) -> LearningJob | None:
    """Atomically claim one claimable job (CAS on status + lease owner).

    Two workers racing on the same job see exactly one ``rowcount == 1``;
    the loser finds nothing to claim.
    """
    max_concurrent = int(DEFAULT_LEARNING_POLICY["max_concurrent_jobs"])
    conn.execute("BEGIN IMMEDIATE")
    try:
        running = conn.execute(
            "SELECT COUNT(*) FROM learning_jobs WHERE status = 'RUNNING' "
            "AND lease_expires_epoch > ?",
            (int(now_epoch),),
        ).fetchone()[0]
        if running >= max_concurrent:
            conn.rollback()
            return None

        row = conn.execute(
            """
            SELECT job_id, stage, status, owner, input_snapshot_sha256,
                   policy_sha256, lease_expires_epoch, lease_token
            FROM learning_jobs
            WHERE status = 'PENDING'
               OR (status = 'RETRY_WAIT' AND (next_retry_epoch IS NULL OR next_retry_epoch <= ?))
               OR (status = 'RUNNING' AND lease_expires_epoch IS NOT NULL AND lease_expires_epoch <= ?)
            ORDER BY created_at ASC
            LIMIT 1
            """,
            (int(now_epoch), int(now_epoch)),
        ).fetchone()
        if row is None:
            conn.rollback()
            return None

        job_id, stage, status, previous_owner = row[0], row[1], row[2], row[3]
        original_lease = row[6]
        previous_token = row[7]
        # A fresh, unguessable claim token: the owner string alone is NOT a
        # credential (two workers may share a default owner name), so every
        # later mutation must present THIS token.
        lease_token = uuid.uuid4().hex
        updated = conn.execute(
            """
            UPDATE learning_jobs
            SET status = 'RUNNING', owner = ?, lease_token = ?,
                lease_expires_epoch = ?,
                attempts = attempts + 1, updated_at = ?
            WHERE job_id = ? AND status = ? AND owner IS ? AND lease_token IS ? AND (
                lease_expires_epoch IS ? OR lease_expires_epoch = ?
            )
            """,
            (
                owner,
                lease_token,
                int(now_epoch + lease_seconds),
                _now_iso(),
                job_id,
                status,
                previous_owner,
                previous_token,
                original_lease,
                original_lease,
            ),
        ).rowcount
        if updated != 1:
            conn.rollback()
            return None

        final = conn.execute(
            "SELECT stage, input_snapshot_sha256, policy_sha256 FROM learning_jobs "
            "WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        conn.commit()
        return LearningJob(
            job_id=job_id,
            stage=str(final[0]),
            owner=owner,
            input_snapshot_sha256=str(final[1]),
            policy_sha256=str(final[2]),
            lease_token=lease_token,
        )
    except BaseException:
        conn.rollback()
        raise


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso_to_epoch(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _load_stage_package(conn: sqlite3.Connection, job_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT result_package_path, result_package_sha256 FROM learning_jobs "
        "WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    if not row or not row[0]:
        return None
    try:
        raw = Path(row[0]).read_bytes()
    except OSError:
        return None
    if hashlib.sha256(raw).hexdigest() != row[1]:
        return None
    try:
        return cast(dict[str, Any], json.loads(raw.decode("utf-8")))
    except ValueError:
        return None


class LearningLeaseLost(PermissionError):
    """A stale worker tried to mutate a job it no longer owns."""


def _save_stage_package(
    conn: sqlite3.Connection,
    job_id: str,
    package: dict[str, Any],
    work_dir: Path,
    expected_owner: str,
    expected_token: str = "",
) -> tuple[str, str]:
    """Persist the stage package under a REAL lease CAS.

    The DB row is updated only while the caller's ``lease_token`` is still
    the credential of a RUNNING, unexpired lease; the official package file
    is replaced only after that CAS succeeds. An expired worker -- or a
    same-named worker whose lease was re-claimed -- can neither corrupt the
    row nor the file the new owner will read.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    package_path = work_dir / f"job-{job_id}.stages.json"
    raw = json.dumps(package, sort_keys=True, separators=(",", ":")).encode("utf-8")

    now_epoch = int(time.time())
    # The token is the credential: as long as THIS claim still owns the
    # RUNNING job (nobody else re-claimed), the save also RENEWS the lease so
    # a legitimately long stage is not lost to its own expiry. If another
    # worker re-claimed, the token differs and the stale save is refused.
    updated = conn.execute(
        "UPDATE learning_jobs SET result_package_sha256 = ?, "
        "lease_expires_epoch = ? "
        "WHERE job_id = ? AND owner = ? AND lease_token = ? "
        "AND status = 'RUNNING'",
        (
            hashlib.sha256(raw).hexdigest(),
            int(now_epoch + int(DEFAULT_LEARNING_POLICY["lease_seconds"])),
            job_id,
            expected_owner,
            expected_token,
        ),
    ).rowcount
    if updated != 1:
        raise LearningLeaseLost(
            f"job {job_id} lease no longer held by {expected_owner!r}"
        )

    # Unique temp name: two writers must never share a scratch file.
    tmp = package_path.with_name(
        f".{package_path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex[:8]}"
    )
    try:
        tmp.write_bytes(raw)
        tmp.replace(package_path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise

    package_sha = hashlib.sha256(raw).hexdigest()
    conn.execute(
        "UPDATE learning_jobs SET result_package_path = ?, result_package_sha256 = ? "
        "WHERE job_id = ? AND owner = ? AND lease_token = ? AND status = 'RUNNING'",
        (
            str(package_path.resolve()),
            package_sha,
            job_id,
            expected_owner,
            expected_token,
        ),
    )
    return str(package_path.resolve()), package_sha


def _verify_artifact(artifact: Mapping[str, Any] | None) -> str | None:
    """Return an error code when a recorded stage artifact drifted."""
    if not artifact:
        return None
    path = artifact.get("path")
    expected = artifact.get("sha256")
    if not path or not expected:
        return None
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return "artifact_missing"
    if hashlib.sha256(raw).hexdigest() != expected:
        return "artifact_hash_mismatch"
    return None


def _retry_delay(attempts: int) -> int:
    index = min(max(attempts - 1, 0), len(_RETRY_DELAYS_S) - 1)
    return _RETRY_DELAYS_S[index]


def execute_learning_stage(
    conn: sqlite3.Connection,
    job: LearningJob,
    services: Mapping[str, Any],
) -> LearningStageResult:
    """Execute exactly one stage of the learning job.

    A stage only advances when its artifact exists, its hash matches, and the
    completion was recorded. Hash drift stops the job as NEEDS_ATTENTION.
    """
    work_dir = Path(services.get("work_dir") or "data/learning_runs")
    policy = dict(DEFAULT_LEARNING_POLICY)
    if services.get("policy"):
        policy.update(services["policy"])
    package = _load_stage_package(conn, job.job_id)
    if package is None:
        package = {"stages": {}}
    return _execute_stage_with_budget(conn, job, services, policy, work_dir, package)


def _stage_retry(
    conn: sqlite3.Connection,
    job: LearningJob,
    package: dict[str, Any],
    work_dir: Path,
    policy: Mapping[str, Any],
    error_code: str,
) -> LearningStageResult:
    """Consume one attempt of THIS stage's retry budget.

    Budgets are per stage: a successful SNAPSHOT or TRAIN never eats into the
    QUALIFY retry allowance.
    """
    stage_attempts = int((package.get("stage_attempts") or {}).get(job.stage, 0)) + 1
    package.setdefault("stage_attempts", {})[job.stage] = stage_attempts
    _save_stage_package(conn, job.job_id, package, work_dir, job.owner, job.lease_token)
    if stage_attempts >= int(policy["max_attempts"]):
        return LearningStageResult("NEEDS_ATTENTION", None, None, None, error_code)
    return LearningStageResult("RETRY", None, None, None, error_code)


def _execute_stage_with_budget(
    conn: sqlite3.Connection,
    job: LearningJob,
    services: Mapping[str, Any],
    policy: dict[str, Any],
    work_dir: Path,
    package: dict[str, Any],
) -> LearningStageResult:
    stage = job.stage

    # Recovery: every previously completed stage artifact must still verify.
    for done_stage, artifact in sorted(package.get("stages", {}).items()):
        drift = _verify_artifact(artifact)
        if drift:
            return LearningStageResult(
                "NEEDS_ATTENTION", None, None, None, f"{done_stage.lower()}_{drift}"
            )
    completed_stages = set(package.get("stages", {}))

    cancelled = conn.execute(
        "SELECT status FROM learning_jobs WHERE job_id = ?",
        (job.job_id,),
    ).fetchone()[0]
    if cancelled == "CANCELLED":
        return LearningStageResult("NEEDS_ATTENTION", None, None, None, "job_cancelled")

    if stage == "SNAPSHOT":
        watermark, _counts = _effective_event_watermark(conn)
        if watermark != job.input_snapshot_sha256:
            return LearningStageResult(
                "NEEDS_ATTENTION", None, None, None, "snapshot_not_found"
            )
        try:
            snapshot = services["build_training_snapshot"](
                conn, policy, work_dir / job.job_id
            )
        except Exception:
            return _stage_retry(
                conn, job, package, work_dir, policy, "snapshot_build_failed"
            )
        artifact = {
            "path": str(Path(snapshot.snapshot_dir, "manifest.json").resolve()),
            "sha256": snapshot.manifest_sha256,
        }
        package["stages"]["SNAPSHOT"] = artifact
        package["snapshot"] = {
            "dir": snapshot.snapshot_dir,
            "manifest_sha256": snapshot.manifest_sha256,
            "train_row_count": snapshot.train_row_count,
            "development_row_count": snapshot.development_row_count,
            "final_holdout_row_count": snapshot.final_holdout_row_count,
        }
        _save_stage_package(conn, job.job_id, package, work_dir, job.owner, job.lease_token)
        return LearningStageResult(
            "ADVANCE", "TRAIN", artifact["path"], artifact["sha256"], None
        )

    if stage == "TRAIN":
        if "SNAPSHOT" not in completed_stages:
            return LearningStageResult(
                "NEEDS_ATTENTION", None, None, None, "snapshot_not_found"
            )
        snapshot_info = package.get("snapshot") or {}
        snapshot_dir = snapshot_info.get("dir")
        if not snapshot_dir or not Path(snapshot_dir, "manifest.json").is_file():
            return LearningStageResult(
                "NEEDS_ATTENTION", None, None, None, "snapshot_not_found"
            )
        try:
            result = services["train_draft"](
                Path(snapshot_dir), work_dir / job.job_id, "curve-source-v1"
            )
        except Exception:
            return _stage_retry(
                conn, job, package, work_dir, policy, "train_failed"
            )
        if result.get("status") != "DRAFT":
            return LearningStageResult(
                "NEEDS_ATTENTION", None, None, None,
                str(result.get("error_code") or "train_failed"),
            )
        try:
            services["register_draft"](conn, result)
        except Exception:
            return _stage_retry(
                conn, job, package, work_dir, policy, "register_failed"
            )
        artifact = {
            "path": str(result["model_path"]),
            "sha256": str(result["model_sha256"]),
        }
        package["stages"]["TRAIN"] = artifact
        package["model_version"] = result["model_version"]
        _save_stage_package(conn, job.job_id, package, work_dir, job.owner, job.lease_token)
        return LearningStageResult(
            "ADVANCE", "QUALIFY", artifact["path"], artifact["sha256"], None
        )

    if stage == "QUALIFY":
        if "TRAIN" not in completed_stages:
            return LearningStageResult(
                "NEEDS_ATTENTION", None, None, None, "model_not_found"
            )
        train_artifact = package["stages"]["TRAIN"]
        drift = _verify_artifact(train_artifact)
        if drift:
            return LearningStageResult(
                "NEEDS_ATTENTION", None, None, None, f"train_{drift}"
            )
        model_version = package.get("model_version")
        db_path = services.get("db_path")
        if not model_version or not db_path:
            return LearningStageResult(
                "NEEDS_ATTENTION", None, None, None, "qualification_inputs_missing"
            )
        try:
            summary = services["execute_qualification_run"](
                db_path=Path(db_path),
                model_version=str(model_version),
                work_dir=work_dir / job.job_id / "qualification",
            )
        except Exception:
            return _stage_retry(
                conn, job, package, work_dir, policy, "qualify_failed"
            )
        package["qualification"] = {
            "qualification_id": summary.get("qualification_id"),
            "status": summary.get("status"),
            "reasons": list(summary.get("reasons") or []),
            "run_dir": summary.get("run_dir"),
        }
        package["stages"]["QUALIFY"] = {
            "path": str(Path(summary.get("run_dir") or "", "report.md")),
            "sha256": hashlib.sha256(
                (Path(summary["run_dir"]) / "report.md").read_bytes()
            ).hexdigest()
            if summary.get("run_dir")
            else "",
        }
        _save_stage_package(conn, job.job_id, package, work_dir, job.owner, job.lease_token)
        # NOT_QUALIFIED is a scientific outcome, not an execution error.
        return LearningStageResult("ADVANCE", "COMPLETE", None, None, None)

    if stage == "COMPLETE":
        return LearningStageResult("COMPLETE", None, None, None, None)

    return LearningStageResult("NEEDS_ATTENTION", None, None, None, "unknown_stage")


def _apply_stage_result(
    conn: sqlite3.Connection,
    job: LearningJob,
    result: LearningStageResult,
    now_epoch: float,
) -> dict[str, Any]:
    lease_cas = "job_id = ? AND lease_token = ? AND status = 'RUNNING'"
    lease_params = (job.job_id, job.lease_token)
    if result.outcome == "ADVANCE" and result.next_stage:
        # Advance the stage AND release the lease in one statement: the next
        # poll (this worker or another) claims the job immediately instead of
        # waiting out the full lease on a job that is not executing anything.
        updated = conn.execute(
            "UPDATE learning_jobs SET stage = ?, status = 'PENDING', "
            "owner = NULL, lease_token = NULL, lease_expires_epoch = NULL, "
            "updated_at = ? WHERE " + lease_cas,
            (result.next_stage, _now_iso(), *lease_params),
        ).rowcount
        if updated != 1:
            # The new holder owns the job; leave its state untouched.
            return {"status": "LEASE_LOST", "stage": job.stage, "error_code": "lease_lost"}
        return {"status": "ADVANCED", "stage": result.next_stage, "error_code": None}

    if result.outcome == "COMPLETE":
        conn.execute(
            "UPDATE learning_jobs SET status = 'SUCCEEDED', updated_at = ? "
            "WHERE " + lease_cas,
            (_now_iso(), *lease_params),
        )
        return {"status": "SUCCEEDED", "stage": "COMPLETE", "error_code": None}

    if result.outcome == "RETRY":
        # The per-stage attempt budget is enforced by execute_learning_stage;
        # reaching here means retries remain for THIS stage.
        attempts = conn.execute(
            "SELECT attempts FROM learning_jobs WHERE job_id = ?", (job.job_id,)
        ).fetchone()[0]
        delay = _retry_delay(attempts)
        conn.execute(
            "UPDATE learning_jobs SET status = 'RETRY_WAIT', next_retry_epoch = ?, "
            "lease_expires_epoch = NULL, owner = NULL, lease_token = NULL, "
            "error_code = ?, updated_at = ? WHERE " + lease_cas,
            (int(now_epoch + delay), result.error_code, _now_iso(), *lease_params),
        )
        return {
            "status": "RETRY_WAIT",
            "stage": job.stage,
            "error_code": result.error_code,
        }

    conn.execute(
        "UPDATE learning_jobs SET status = 'NEEDS_ATTENTION', error_code = ?, "
        "lease_expires_epoch = NULL, lease_token = NULL, updated_at = ? "
        "WHERE " + lease_cas,
        (result.error_code, _now_iso(), *lease_params),
    )
    return {
        "status": "NEEDS_ATTENTION",
        "stage": job.stage,
        "error_code": result.error_code,
    }


def default_services(conn: sqlite3.Connection) -> dict[str, Any]:
    """Real stage services bound to the worker's library database."""
    db_path = None
    try:
        from app.db import DB_PATH

        db_path = str(DB_PATH)
    except Exception:
        db_path = None

    from app.services.curve_training import (
        build_training_snapshot,
        register_draft,
        train_draft,
    )

    return {
        "build_training_snapshot": build_training_snapshot,
        "train_draft": train_draft,
        "register_draft": register_draft,
        "execute_qualification_run": _execute_qualification_run_default,
        "db_path": db_path,
        "work_dir": "data/learning_runs",
    }


def _execute_qualification_run_default(**kwargs: Any) -> dict[str, Any]:
    from app.services.curve_preference_qualification import execute_qualification_run

    return execute_qualification_run(**kwargs)


def run_learning_once(
    conn: sqlite3.Connection,
    now_epoch: float | None = None,
    owner: str = "worker",
    now_utc: str | None = None,
    lease_seconds: float = 1800,
    services: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Claim one job, execute one stage, and record the outcome.

    ``now_utc`` (ISO) is accepted for backwards compatibility and converted
    to an integer epoch; new callers pass ``now_epoch``.
    """
    if now_epoch is None:
        if now_utc is not None:
            epoch = _iso_to_epoch(now_utc)
            now_epoch = epoch if epoch is not None else time.time()
        else:
            now_epoch = time.time()

    job = claim_learning_job(conn, owner, now_epoch, lease_seconds)
    if job is None:
        return {"status": "IDLE", "job_id": None, "stage": None}

    stage_services = dict(services) if services else default_services(conn)
    try:
        result = execute_learning_stage(conn, job, stage_services)
        applied = _apply_stage_result(conn, job, result, now_epoch)
    except LearningLeaseLost:
        # A stale worker lost its claim mid-stage: report and keep polling.
        # The job and its stage package belong to the current holder and are
        # left completely untouched.
        conn.rollback()
        return {
            "status": "LEASE_LOST",
            "job_id": job.job_id,
            "job_type": "curve_preference",
            "stage": job.stage,
            "error_code": "lease_lost",
        }
    conn.commit()
    return {
        "status": applied["status"],
        "job_id": job.job_id,
        "job_type": "curve_preference",
        "stage": applied.get("stage"),
        "error_code": applied.get("error_code"),
    }
