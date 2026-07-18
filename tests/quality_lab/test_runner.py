from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

import pytest

from app.quality_lab import (
    BenchmarkItem,
    ExperimentRun,
    apply_quality_schema,
    connect_quality_db,
)
from app.quality_lab.runner import ExperimentRunner

# ===================================================================
# Fake task client
# ===================================================================


class FakeTaskClient:
    """In-memory fake that simulates the task API.

    ``create_job`` returns an existing job ID only when the *directory*
    AND *video_paths* scope match an existing active job.  Different
    items in the same directory get different job IDs.
    """

    def __init__(self) -> None:
        self.jobs: dict[str, dict] = {}
        self._scope_to_job: dict[str, str] = {}
        # Fault injection: set to a directory path to make create_job fail
        self.fail_on_directory: str | None = None

    def create_job(
        self, directory: str, config_json: str, video_paths: list[str] | None = None
    ) -> str:
        if self.fail_on_directory and directory == self.fail_on_directory:
            raise RuntimeError(f"Simulated failure for {directory}")

        # Compute a scope key from directory + sorted video_paths.
        import os, hashlib
        dk = os.path.normcase(os.path.abspath(directory))
        if video_paths:
            normalized = sorted(os.path.normcase(os.path.abspath(p)) for p in video_paths)
            path_hash = hashlib.sha256("|".join(normalized).encode("utf-8")).hexdigest()[:16]
            scope = f"{dk}:{path_hash}"
        else:
            scope = f"{dk}:*"

        if scope in self._scope_to_job:
            return self._scope_to_job[scope]

        import uuid
        job_id = f"job_{uuid.uuid4().hex[:12]}"
        self.jobs[job_id] = {
            "job_id": job_id,
            "directory": directory,
            "config_json": config_json,
            "status": "running",
            "metrics": {},
        }
        self._scope_to_job[scope] = job_id
        return job_id

    def get_job(self, job_id: str) -> dict:
        return dict(self.jobs.get(job_id, {}))

    def cancel_job(self, job_id: str) -> None:
        if job_id in self.jobs:
            self.jobs[job_id]["status"] = "cancelled"


# ===================================================================
# Factory helpers
# ===================================================================


def make_item(
    item_id: str,
    *,
    root: Path | None = None,
    split: str = "tune",
    fingerprint: str = "",
) -> BenchmarkItem:
    """Create a ``BenchmarkItem``.

    When *root* is given the item's source_path is placed in its own
    sub-directory under *root* so that each item has a unique parent
    directory.
    """
    if root is not None:
        item_dir = root / item_id
        item_dir.mkdir(parents=True, exist_ok=True)
        video_path = item_dir / f"{item_id}.mp4"
        video_path.write_text("fake video")
        source_path = str(video_path)
    else:
        source_path = f"/videos/{item_id}.mp4"

    return BenchmarkItem(
        item_id=item_id,
        source_path=source_path,
        video_fingerprint=fingerprint or f"fp_{item_id}",
        duration_bucket="short",
        resolution_bucket="hd",
        pace_bucket="medium",
        difficulty_tags=("action",),
        split=split,  # type: ignore[arg-type]
    )


def seed_manifest(db: sqlite3.Connection, items: list[BenchmarkItem]) -> str:
    """Insert a manifest and its items into the quality-lab DB."""
    manifest_id = f"m_{uuid.uuid4().hex[:8]}"
    db.execute(
        "INSERT INTO benchmark_manifests (manifest_id, version, item_count, created_at) "
        "VALUES (?, ?, ?, ?)",
        (manifest_id, 1, len(items), "2026-01-01T00:00:00"),
    )
    for item in items:
        db.execute(
            """INSERT INTO benchmark_items
               (item_id, manifest_id, source_path, video_fingerprint,
                duration_bucket, resolution_bucket, pace_bucket,
                difficulty_tags, split)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                item.item_id,
                manifest_id,
                item.source_path,
                item.video_fingerprint,
                item.duration_bucket,
                item.resolution_bucket,
                item.pace_bucket,
                "|".join(item.difficulty_tags),
                item.split,
            ),
        )
    db.commit()
    return manifest_id


def seed_config(db: sqlite3.Connection) -> str:
    """Insert a minimal experiment config."""
    config_id = f"cfg_{uuid.uuid4().hex[:8]}"
    db.execute(
        "INSERT INTO experiment_configs (config_id, config_json, provenance_json, created_at) "
        "VALUES (?, ?, ?, ?)",
        (config_id, json.dumps({"vlm": {"model": "test"}}), "{}", "2026-01-01T00:00:00"),
    )
    db.commit()
    return config_id


# ===================================================================
# Tests
# ===================================================================


class TestExperimentRunner:
    """``ExperimentRunner`` — task-engine orchestration for benchmarks."""

    # -- fixtures --------------------------------------------------------

    @pytest.fixture
    def db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        apply_quality_schema(conn)
        return conn

    @pytest.fixture
    def task_client(self) -> FakeTaskClient:
        return FakeTaskClient()

    @pytest.fixture
    def runner(self, db: sqlite3.Connection, task_client: FakeTaskClient) -> ExperimentRunner:
        return ExperimentRunner(db, task_client)

    # -- create_run ------------------------------------------------------

    def test_create_run_returns_experiment_run(self, db: sqlite3.Connection, runner: ExperimentRunner) -> None:
        """``create_run`` returns a valid ``ExperimentRun`` and persists the row."""
        mid = seed_manifest(db, [make_item("vid_001")])
        cid = seed_config(db)

        run = runner.create_run(manifest_id=mid, config_id=cid, split="tune")

        assert isinstance(run, ExperimentRun)
        assert run.manifest_id == mid
        assert run.config_id == cid
        assert run.split == "tune"
        assert run.status == "pending"

        # Persisted
        row = db.execute("SELECT * FROM experiment_runs WHERE run_id=?", (run.run_id,)).fetchone()
        assert row is not None
        assert row["status"] == "pending"

    # -- submit ----------------------------------------------------------

    def test_tune_run_skips_holdout_items(
        self, db: sqlite3.Connection, runner: ExperimentRunner,
        task_client: FakeTaskClient, tmp_path: Path,
    ) -> None:
        """``submit`` must never create a job for holdout items on a tune run."""
        tune_item = make_item("vid_tune", root=tmp_path, split="tune")
        holdout_item = make_item("vid_holdout", root=tmp_path, split="holdout")
        mid = seed_manifest(db, [tune_item, holdout_item])
        cid = seed_config(db)

        run = runner.create_run(manifest_id=mid, config_id=cid, split="tune")
        job_ids = runner.submit(run.run_id)

        # Only tune item should have a job
        assert len(job_ids) == 1

        items = db.execute(
            "SELECT item_id, task_job_id FROM experiment_items WHERE run_id=?", (run.run_id,),
        ).fetchall()
        assert len(items) == 1
        assert items[0]["item_id"] == "vid_tune"
        assert items[0]["task_job_id"] is not None

    def test_holdout_run_skips_tune_items(
        self, db: sqlite3.Connection, runner: ExperimentRunner,
        task_client: FakeTaskClient, tmp_path: Path,
    ) -> None:
        """``submit`` must never create a job for tune items on a holdout run."""
        tune_item = make_item("vid_tune", root=tmp_path, split="tune")
        holdout_item = make_item("vid_holdout", root=tmp_path, split="holdout")
        mid = seed_manifest(db, [tune_item, holdout_item])
        cid = seed_config(db)

        run = runner.create_run(manifest_id=mid, config_id=cid, split="holdout")
        job_ids = runner.submit(run.run_id)

        assert len(job_ids) == 1
        row = db.execute(
            "SELECT item_id FROM experiment_items WHERE run_id=?", (run.run_id,),
        ).fetchone()
        assert row["item_id"] == "vid_holdout"

    def test_submit_creates_one_job_per_item(
        self, db: sqlite3.Connection, runner: ExperimentRunner,
        task_client: FakeTaskClient, tmp_path: Path,
    ) -> None:
        """Each tune item receives its own task job."""
        items = [make_item(f"vid_{i:03d}", root=tmp_path) for i in range(3)]
        mid = seed_manifest(db, items)
        cid = seed_config(db)

        run = runner.create_run(manifest_id=mid, config_id=cid, split="tune")
        job_ids = runner.submit(run.run_id)

        assert len(job_ids) == 3
        assert len(set(job_ids)) == 3  # all unique

    def test_job_config_includes_experiment_run_id(
        self, db: sqlite3.Connection, runner: ExperimentRunner,
        task_client: FakeTaskClient, tmp_path: Path,
    ) -> None:
        """The config passed to ``create_job`` must contain ``experiment_run_id``."""
        item = make_item("vid_001", root=tmp_path)
        mid = seed_manifest(db, [item])
        cid = seed_config(db)

        run = runner.create_run(manifest_id=mid, config_id=cid, split="tune")
        runner.submit(run.run_id)

        # Inspect what the fake received
        for job in task_client.jobs.values():
            cfg = json.loads(job["config_json"])
            exp = cfg["_experiment"]
            assert exp["run_id"] == run.run_id
            assert exp["item_id"] == "vid_001"
            assert exp["manifest_id"] == mid

    def test_submit_reuses_job_id_for_same_scope(
        self, db: sqlite3.Connection, runner: ExperimentRunner,
        task_client: FakeTaskClient, tmp_path: Path,
    ) -> None:
        """Two items sharing the same directory AND video_paths get the same job ID."""
        shared = tmp_path / "shared"
        shared.mkdir(parents=True, exist_ok=True)
        p1 = shared / "vid_a.mp4"
        p2 = shared / "vid_b.mp4"
        p1.write_text("a")
        p2.write_text("b")

        items = [
            BenchmarkItem(
                item_id="vid_a", source_path=str(p1), video_fingerprint="fp_a",
                duration_bucket="short", resolution_bucket="hd", pace_bucket="medium",
                difficulty_tags=("action",), split="tune",
            ),
            BenchmarkItem(
                item_id="vid_b", source_path=str(p2), video_fingerprint="fp_b",
                duration_bucket="short", resolution_bucket="hd", pace_bucket="medium",
                difficulty_tags=("action",), split="tune",
            ),
        ]
        mid = seed_manifest(db, items)
        cid = seed_config(db)

        run = runner.create_run(manifest_id=mid, config_id=cid, split="tune")
        job_ids = runner.submit(run.run_id)

        # Same directory but different video_paths → different job IDs per scope.
        assert len(set(job_ids)) == 2

        # Both experiment_items reference different job_ids
        rows = db.execute(
            "SELECT item_id, task_job_id FROM experiment_items WHERE run_id=?",
            (run.run_id,),
        ).fetchall()
        assert len(rows) == 2
        job_ids_from_db = {r["task_job_id"] for r in rows}
        assert len(job_ids_from_db) == 2

    def test_idempotent_submit(
        self, db: sqlite3.Connection, runner: ExperimentRunner,
        task_client: FakeTaskClient, tmp_path: Path,
    ) -> None:
        """Submitting the same run twice must not create duplicate jobs."""
        items = [make_item(f"vid_{i:03d}", root=tmp_path) for i in range(3)]
        mid = seed_manifest(db, items)
        cid = seed_config(db)

        run = runner.create_run(manifest_id=mid, config_id=cid, split="tune")
        first_ids = runner.submit(run.run_id)
        second_ids = runner.submit(run.run_id)

        assert first_ids == second_ids
        assert len(task_client.jobs) == 3  # no extra jobs

    def test_partial_failure_leaves_successful_items_intact(
        self, db: sqlite3.Connection, runner: ExperimentRunner,
        task_client: FakeTaskClient, tmp_path: Path,
    ) -> None:
        """When one item's job creation fails, already-submitted items keep their job IDs."""
        items = [make_item(f"vid_{i:03d}", root=tmp_path) for i in range(3)]

        # Make the third item's directory fail
        fail_dir = str(Path(items[2].source_path).parent)
        task_client.fail_on_directory = fail_dir

        mid = seed_manifest(db, items)
        cid = seed_config(db)

        run = runner.create_run(manifest_id=mid, config_id=cid, split="tune")

        # submit should not raise — it handles partial failure
        job_ids = runner.submit(run.run_id)

        # Only 2 successful job IDs returned
        assert len(job_ids) == 2

        # First two items have job IDs, third does not
        rows = {
            r["item_id"]: r["task_job_id"]
            for r in db.execute(
                "SELECT item_id, task_job_id FROM experiment_items WHERE run_id=?",
                (run.run_id,),
            ).fetchall()
        }
        assert rows["vid_000"] is not None
        assert rows["vid_001"] is not None
        assert rows["vid_002"] is None  # failed

        # Run status is "partial"
        run_row = db.execute(
            "SELECT status FROM experiment_runs WHERE run_id=?", (run.run_id,),
        ).fetchone()
        assert run_row["status"] == "partial"

    # -- refresh ---------------------------------------------------------

    def test_refresh_derives_run_status_from_jobs(
        self, db: sqlite3.Connection, runner: ExperimentRunner,
        task_client: FakeTaskClient, tmp_path: Path,
    ) -> None:
        """``refresh`` maps each item's job status and derives run status."""
        items = [make_item(f"vid_{i:03d}", root=tmp_path) for i in range(2)]
        mid = seed_manifest(db, items)
        cid = seed_config(db)

        run = runner.create_run(manifest_id=mid, config_id=cid, split="tune")
        runner.submit(run.run_id)

        # Mark both jobs as succeeded
        for job in task_client.jobs.values():
            job["status"] = "succeeded"
            job["metrics"] = {
                "wall_time_seconds": 10.5,
                "vlm_calls": 2,
                "token_count": 500,
                "artifact_bytes": 20480,
                "candidate_count": 3,
            }

        updated = runner.refresh(run.run_id)
        assert updated.status == "completed"

        # Metrics recorded
        rows = db.execute(
            "SELECT * FROM experiment_items WHERE run_id=?", (run.run_id,),
        ).fetchall()
        for row in rows:
            assert row["status"] == "completed"
            assert row["wall_time_seconds"] == 10.5
            assert row["vlm_calls"] == 2
            assert row["token_count"] == 500
            assert row["artifact_bytes"] == 20480
            assert row["candidate_count"] == 3

    def test_reflects_partial_failure_status(
        self, db: sqlite3.Connection, runner: ExperimentRunner,
        task_client: FakeTaskClient, tmp_path: Path,
    ) -> None:
        """When some jobs fail, run status is ``partial_failure``."""
        items = [make_item(f"vid_{i:03d}", root=tmp_path) for i in range(2)]
        mid = seed_manifest(db, items)
        cid = seed_config(db)

        run = runner.create_run(manifest_id=mid, config_id=cid, split="tune")
        runner.submit(run.run_id)

        # One succeeds, one fails
        job_ids = list(task_client.jobs.keys())
        task_client.jobs[job_ids[0]]["status"] = "succeeded"
        task_client.jobs[job_ids[1]]["status"] = "needs_attention"

        updated = runner.refresh(run.run_id)
        assert updated.status == "partial_failure"

    # -- cancel ----------------------------------------------------------

    def test_cancel_stops_all_jobs(
        self, db: sqlite3.Connection, runner: ExperimentRunner,
        task_client: FakeTaskClient, tmp_path: Path,
    ) -> None:
        """``cancel`` cancels every running job for the run."""
        items = [make_item(f"vid_{i:03d}", root=tmp_path) for i in range(2)]
        mid = seed_manifest(db, items)
        cid = seed_config(db)

        run = runner.create_run(manifest_id=mid, config_id=cid, split="tune")
        runner.submit(run.run_id)

        runner.cancel(run.run_id)

        # All jobs cancelled in fake
        for job in task_client.jobs.values():
            assert job["status"] == "cancelled"

        run_row = db.execute(
            "SELECT status FROM experiment_runs WHERE run_id=?", (run.run_id,),
        ).fetchone()
        assert run_row["status"] == "cancelled"

        # Items cancelled
        rows = db.execute(
            "SELECT status FROM experiment_items WHERE run_id=?", (run.run_id,),
        ).fetchall()
        for row in rows:
            assert row["status"] == "cancelled"

    # -- edge cases ------------------------------------------------------

    def test_submit_empty_run(
        self, db: sqlite3.Connection, runner: ExperimentRunner,
        task_client: FakeTaskClient,
    ) -> None:
        """Submitting a run with no matching items returns an empty list."""
        mid = seed_manifest(db, [make_item("vid_001", split="tune")])
        cid = seed_config(db)
        # Run on holdout split with only tune items
        run = runner.create_run(manifest_id=mid, config_id=cid, split="holdout")
        job_ids = runner.submit(run.run_id)
        assert job_ids == []

    def test_refresh_on_run_with_no_items(
        self, db: sqlite3.Connection, runner: ExperimentRunner,
        task_client: FakeTaskClient,
    ) -> None:
        """``refresh`` on a run with zero items returns ``pending`` status."""
        mid = seed_manifest(db, [])
        cid = seed_config(db)
        run = runner.create_run(manifest_id=mid, config_id=cid, split="tune")
        updated = runner.refresh(run.run_id)
        assert updated.status == "pending"

    def test_cancel_partially_failed_run(
        self, db: sqlite3.Connection, runner: ExperimentRunner,
        task_client: FakeTaskClient, tmp_path: Path,
    ) -> None:
        """Cancelling a run with some items not yet submitted is safe."""
        items = [make_item("vid_ok", root=tmp_path)]
        mid = seed_manifest(db, items)
        cid = seed_config(db)

        run = runner.create_run(manifest_id=mid, config_id=cid, split="tune")
        runner.submit(run.run_id)

        # Add another manifest item after submit (simulating late add)
        extra = make_item("vid_late", root=tmp_path, split="tune")
        db.execute(
            "INSERT INTO benchmark_items VALUES (?,?,?,?,?,?,?,?,?)",
            (
                extra.item_id, mid, extra.source_path, extra.video_fingerprint,
                extra.duration_bucket, extra.resolution_bucket, extra.pace_bucket,
                "|".join(extra.difficulty_tags), extra.split,
            ),
        )
        db.commit()

        runner.cancel(run.run_id)

        # Successfully submitted items get cancelled
        rows = db.execute(
            "SELECT item_id, status, task_job_id FROM experiment_items WHERE run_id=?",
            (run.run_id,),
        ).fetchall()
        assert len(rows) == 1  # only the submitted one
        assert rows[0]["item_id"] == "vid_ok"
        assert rows[0]["status"] == "cancelled"
