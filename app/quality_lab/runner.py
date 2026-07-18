"""Phase 2 Task 3: Benchmark experiment runner.

Orchestrates benchmark experiments by creating one task job per
benchmark item and tracking run/item state through the task engine.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

import httpx

from app.quality_lab.models import ExperimentRun, Split

# ---------------------------------------------------------------------------
# Task client protocol
# ---------------------------------------------------------------------------


class TaskClient(Protocol):
    """Protocol for creating and querying task jobs.

    The concrete implementation may call the task HTTP API or go directly
    to the task repository.
    """

    def create_job(
        self,
        directory: str,
        config_json: str,
        video_paths: list[str] | None = None,
    ) -> str:
        """Create a task job for *directory*.

        *video_paths* optionally restricts processing to specific video
        files within the directory.

        Returns the job ID.  If an active job already exists for the
        directory (conflict), returns the *existing* job ID.
        """
        ...

    def get_job(self, job_id: str) -> dict:
        """Return the job description dict.

        Expected keys: ``status``, ``directory``, ``config_json``, and
        optionally ``metrics`` (a dict with ``wall_time_seconds``,
        ``vlm_calls``, ``token_count``, ``artifact_bytes``,
        ``candidate_count``, ``failures``).
        """
        ...

    def cancel_job(self, job_id: str) -> None:
        """Cancel a running or pending job."""
        ...


# ---------------------------------------------------------------------------
# HTTP task client (real implementation)
# ---------------------------------------------------------------------------


class HttpTaskClient:
    """Task client that calls the task HTTP API.

    Used in production to delegate job creation, status queries, and
    cancellation to the running task-engine service.
    """

    def __init__(self, base_url: str = "http://localhost:8000"):
        self._base_url = base_url.rstrip("/")
        self._http = httpx.Client(timeout=10.0)

    # -- create_job --------------------------------------------------------

    def create_job(
        self,
        directory: str,
        config_json: str,
        limit: int = 0,
        extensions: str = "",
        video_paths: list[str] | None = None,
    ) -> str:
        """POST /api/tasks/jobs and return the job ID.

        Handles 409 (active job conflict) by returning the existing job ID
        from the error response so the caller can reuse it.
        """
        payload: dict = {
            "directory": directory,
            "limit": limit,
            "extensions": extensions,
        }
        if config_json:
            try:
                payload["config_json"] = json.loads(config_json)
            except (json.JSONDecodeError, TypeError):
                pass
        if video_paths:
            payload["video_paths"] = video_paths
        try:
            resp = self._http.post(
                f"{self._base_url}/api/tasks/jobs", json=payload
            )
        except httpx.RequestError as exc:
            raise RuntimeError(
                f"Cannot reach task API at {self._base_url}: {exc}"
            ) from exc

        if resp.status_code == 201:
            data = resp.json()
            return data["job_id"]

        if resp.status_code == 409:
            detail = resp.json().get("detail", {})
            if isinstance(detail, dict):
                existing_id = detail.get("existing_job_id")
                if existing_id:
                    return existing_id

        raise RuntimeError(
            f"Task API returned {resp.status_code} for POST /api/tasks/jobs "
            f"(directory={directory!r}): {resp.text}"
        )

    # -- get_job -----------------------------------------------------------

    def get_job(self, job_id: str) -> dict:
        """GET /api/tasks/jobs/{job_id} and return the job dict."""
        try:
            resp = self._http.get(
                f"{self._base_url}/api/tasks/jobs/{job_id}"
            )
        except httpx.RequestError as exc:
            return {"error": f"Connection failed: {exc}"}

        if resp.status_code == 200:
            return dict(resp.json())

        if resp.status_code == 404:
            return {"error": f"Job not found: {job_id}"}

        return {"error": f"Unexpected status {resp.status_code}: {resp.text}"}

    # -- cancel_job --------------------------------------------------------

    def cancel_job(self, job_id: str) -> None:
        """POST /api/tasks/jobs/{job_id}/cancel."""
        try:
            resp = self._http.post(
                f"{self._base_url}/api/tasks/jobs/{job_id}/cancel"
            )
        except httpx.RequestError as exc:
            raise RuntimeError(
                f"Cannot reach task API at {self._base_url}: {exc}"
            ) from exc

        if resp.status_code != 200:
            raise RuntimeError(
                f"Task API returned {resp.status_code} for "
                f"POST /api/tasks/jobs/{job_id}/cancel: {resp.text}"
            )


# ---------------------------------------------------------------------------
# Experiment runner
# ---------------------------------------------------------------------------


class ExperimentRunner:
    """Orchestrates benchmark experiments against the task engine.

    Each benchmark item in the run's split is submitted as a task job.
    The runner never reads batch logs to determine completion — it
    derives state solely from the task client.
    """

    def __init__(self, db: sqlite3.Connection, task_client: TaskClient) -> None:
        self._db = db
        self._task_client = task_client

    # -- Lifecycle --------------------------------------------------------

    def create_run(
        self,
        *,
        manifest_id: str,
        config_id: str,
        split: Split,
    ) -> ExperimentRun:
        """Create a new experiment run in ``pending`` state."""
        run_id = uuid.uuid4().hex
        now = _utcnow()
        self._db.execute(
            """INSERT INTO experiment_runs
               (run_id, manifest_id, config_id, split, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, 'pending', ?, ?)""",
            (run_id, manifest_id, config_id, split, now, now),
        )
        self._db.commit()
        return ExperimentRun(
            run_id=run_id,
            manifest_id=manifest_id,
            config_id=config_id,
            split=split,
            status="pending",
        )

    def submit(self, run_id: str) -> list[str]:
        """Submit all pending items in the run as task jobs.

        Skips items that already have a ``task_job_id`` (idempotent).
        If a single item fails, the remaining items are still processed
        and the run receives a ``partial`` status so the caller can
        retry.

        Returns the list of all task job IDs (newly created + pre-existing).
        """
        from app.quality_lab.config_builder import build_task_config

        row = self._db.execute(
            "SELECT * FROM experiment_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"Run not found: {run_id}")

        manifest_id = row["manifest_id"]
        config_id = row["config_id"]
        run_split = row["split"]

        # Load the experiment config so pipeline stages see the intended
        # parameters, not the global defaults.
        config_row = self._db.execute(
            "SELECT config_json FROM experiment_configs WHERE config_id=?",
            (config_id,),
        ).fetchone()
        if config_row is None:
            raise ValueError(
                f"Experiment config {config_id} not found for run {run_id}"
            )
        try:
            experiment_config = json.loads(config_row["config_json"])
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Experiment config {config_id} has invalid JSON: {exc}"
            ) from exc

        # Load the global config snapshot from the task engine's config_json
        # so we get model names, base paths, etc.
        global_config = {}
        try:
            global_row = self._db.execute(
                "SELECT value FROM app_config WHERE key='adaptive_config'"
            ).fetchone()
        except Exception:
            global_row = None
        if global_row:
            try:
                global_config = json.loads(global_row["value"])
            except (json.JSONDecodeError, TypeError):
                pass

        # Merge: experiment config fully overrides the global base.
        # build_task_config handles deep merging logic per-item.
        merged_config = dict(global_config)
        for key in experiment_config:
            if key in merged_config and isinstance(merged_config[key], dict) and isinstance(experiment_config[key], dict):
                merged_config[key] = dict(merged_config[key])
                merged_config[key].update(experiment_config[key])
            else:
                merged_config[key] = experiment_config[key]

        # Benchmark items matching this split
        items = self._db.execute(
            "SELECT * FROM benchmark_items WHERE manifest_id=? AND split=?",
            (manifest_id, run_split),
        ).fetchall()

        # Items already submitted (idempotent re-run guard)
        existing: dict[str, str] = {}
        for r in self._db.execute(
            "SELECT item_id, task_job_id FROM experiment_items "
            "WHERE run_id=? AND task_job_id IS NOT NULL",
            (run_id,),
        ).fetchall():
            existing[r["item_id"]] = r["task_job_id"]

        job_ids: list[str] = list(existing.values())
        now = _utcnow()
        all_succeeded = True

        for item in items:
            item_id = item["item_id"]
            if item_id in existing:
                continue

            directory = str(Path(item["source_path"]).parent)

            # Build the per-item config using the shared config builder.
            from app.quality_lab.config_builder import build_task_config

            per_item_config = build_task_config(
                base_config=merged_config,
                experiment_overrides=experiment_config,
                video_paths=[item["source_path"]],
                experiment_metadata={
                    "run_id": run_id,
                    "item_id": item_id,
                    "manifest_id": manifest_id,
                    "config_id": config_id,
                },
            )

            # Create a placeholder row first so partial failure doesn't
            # lose the fact that we attempted this item.
            self._db.execute(
                """INSERT OR IGNORE INTO experiment_items
                   (item_id, run_id, status, created_at)
                   VALUES (?, ?, 'pending', ?)""",
                (item_id, run_id, now),
            )
            self._db.commit()

            try:
                job_id = self._task_client.create_job(
                    directory,
                    json.dumps(per_item_config),
                    video_paths=[item["source_path"]],
                )
            except Exception:
                all_succeeded = False
                continue

            job_ids.append(job_id)
            self._db.execute(
                """UPDATE experiment_items
                   SET task_job_id=?, status='running'
                   WHERE item_id=? AND run_id=?""",
                (job_id, item_id, run_id),
            )
            self._db.commit()

        run_status = "running" if all_succeeded else "partial"
        self._db.execute(
            "UPDATE experiment_runs SET status=?, updated_at=? WHERE run_id=?",
            (run_status, now, run_id),
        )
        self._db.commit()
        return job_ids

    def refresh(self, run_id: str) -> ExperimentRun:
        """Query the task engine for the latest state of every item job.

        Item status is derived from the corresponding task job status.
        Metrics (wall time, VLM calls, token count, etc.) are recorded
        when available.  The returned ``ExperimentRun`` carries the
        aggregated run status.
        """
        row = self._db.execute(
            "SELECT * FROM experiment_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"Run not found: {run_id}")

        items = self._db.execute(
            "SELECT * FROM experiment_items WHERE run_id=?", (run_id,)
        ).fetchall()

        now = _utcnow()
        item_statuses: set[str] = set()

        for item in items:
            job_id = item["task_job_id"]
            if job_id is None:
                item_statuses.add("pending")
                continue

            job_info = self._task_client.get_job(job_id)
            if not job_info:
                item_statuses.add("unknown")
                continue

            job_status = job_info.get("status", "unknown")
            item_status = _map_job_to_item_status(job_status)
            item_statuses.add(item_status)
            metrics = job_info.get("metrics", {}) or {}

            failures = metrics.get("failures")
            failure_json = json.dumps(failures) if failures else None

            self._db.execute(
                """UPDATE experiment_items
                   SET status=?, wall_time_seconds=?, vlm_calls=?,
                       token_count=?, artifact_bytes=?, candidate_count=?,
                       failure_info=?
                   WHERE item_id=? AND run_id=?""",
                (
                    item_status,
                    metrics.get("wall_time_seconds"),
                    metrics.get("vlm_calls"),
                    metrics.get("token_count"),
                    metrics.get("artifact_bytes"),
                    metrics.get("candidate_count"),
                    failure_json,
                    item["item_id"],
                    run_id,
                ),
            )
            self._db.commit()

        run_status = _derive_run_status(item_statuses)
        self._db.execute(
            "UPDATE experiment_runs SET status=?, updated_at=? WHERE run_id=?",
            (run_status, now, run_id),
        )
        self._db.commit()

        return ExperimentRun(
            run_id=run_id,
            manifest_id=row["manifest_id"],
            config_id=row["config_id"],
            split=row["split"],
            status=run_status,
        )

    def cancel(self, run_id: str) -> None:
        """Cancel every running job for the run."""
        items = self._db.execute(
            "SELECT * FROM experiment_items WHERE run_id=? AND task_job_id IS NOT NULL",
            (run_id,),
        ).fetchall()

        now = _utcnow()
        for item in items:
            self._task_client.cancel_job(item["task_job_id"])
            self._db.execute(
                "UPDATE experiment_items SET status='cancelled' WHERE item_id=? AND run_id=?",
                (item["item_id"], run_id),
            )
        self._db.commit()

        self._db.execute(
            "UPDATE experiment_runs SET status='cancelled', updated_at=? WHERE run_id=?",
            (now, run_id),
        )
        self._db.commit()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_JOB_TO_ITEM_STATUS = {
    "pending": "pending",
    "leased": "running",
    "running": "running",
    "succeeded": "completed",
    "retry_wait": "running",
    "needs_attention": "failed",
    "cancelled": "cancelled",
}


def _map_job_to_item_status(job_status: str) -> str:
    """Map a task-engine job status to an experiment-item status."""
    return _JOB_TO_ITEM_STATUS.get(job_status, "unknown")


def _derive_run_status(item_statuses: set[str]) -> str:
    """Aggregate item statuses into a single run status."""
    if not item_statuses:
        return "pending"

    # All completed
    if item_statuses == {"completed"}:
        return "completed"

    # All cancelled (no running/pending/failed items)
    if item_statuses == {"cancelled"}:
        return "cancelled"

    # Any failure → partial failure
    if "failed" in item_statuses:
        return "partial_failure"

    # Any still running / pending
    if "running" in item_statuses or "pending" in item_statuses:
        return "running"

    return "unknown"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()
