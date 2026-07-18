from __future__ import annotations

import json
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from unittest.mock import MagicMock

import pytest

from app.task_engine import (
    CreateJob,
    RetryPolicy,
    StageError,
    TaskRepository,
    TaskWorker,
    classify_error,
    connect_task_db,
)
from app.task_engine.models import StageName
from app.task_engine.stages import StageAdapter, StageContext, StageResult

# ---------------------------------------------------------------------------
# Shared constants and helpers
# ---------------------------------------------------------------------------

T0 = datetime(2026, 7, 17, tzinfo=timezone.utc)


def make_repo(tmp_path: Path) -> tuple[TaskRepository, sqlite3.Connection]:
    conn = connect_task_db(tmp_path / "task.db")
    return TaskRepository(conn), conn


def stage_status(conn: sqlite3.Connection, stage_id: str) -> str:
    row = conn.execute(
        "SELECT status FROM task_stages WHERE stage_id=?", (stage_id,)
    ).fetchone()
    return row[0] if row else ""


def _ensure_discover_artifact(
    conn: sqlite3.Connection,
    *,
    job_id: str,
    video_id: str,
    stage_id: str,
    work_dir: Path,
) -> str:
    """Create a discover_manifest artifact in the DB for P0-2 testing.

    Many unit tests create non-discover stages in isolation.  Since the
    resolver now requires proper upstream artifacts (P0-2), this helper
    seeds a minimal discover manifest so downstream stages can resolve
    their inputs.
    """
    from datetime import datetime, timezone
    from app.task_engine.artifacts import make_artifact_id
    from app.task_engine.fingerprints import sha256_file

    work_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = work_dir / "discover_manifest.json"
    manifest = {
        "schema_version": 1,
        "stage": "discover",
        "duration_s": 120.0,
        "video_path": str(work_dir / "test.mp4"),
        "video_name": "test",
    }
    manifest_path.write_text(json.dumps(manifest))

    artifact_id = make_artifact_id(
        stage_id=stage_id,
        artifact_kind="discover_manifest",
        clip_id=None,
        normalized_path=str(manifest_path),
    )
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT OR IGNORE INTO task_artifacts
           (artifact_id, job_id, video_id, stage_name, clip_id,
            path, sha256, size_bytes, provenance_json, created_at,
            stage_id, artifact_kind)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            artifact_id, job_id, video_id, "discover", None,
            str(manifest_path), sha256_file(manifest_path),
            manifest_path.stat().st_size, "{}", now,
            stage_id, "discover_manifest",
        ),
    )
    conn.commit()
    return str(manifest_path)


# ---------------------------------------------------------------------------
# Mock adapter
# ---------------------------------------------------------------------------


class MockAdapter:
    """Configurable fake ``StageAdapter`` for unit tests.

    Parameters
    ----------
    name:
        The ``StageName`` this adapter pretends to serve.
    version:
        Version string.
    raise_exc:
        If not ``None``, the adapter raises this exception from ``run()``.
        When ``raise_exc`` is ``None`` the adapter returns *result*.
    result:
        The ``StageResult`` returned on success.  A sensible default is
        created when not provided.
    """

    def __init__(
        self,
        name: StageName = "discover",
        version: str = "1",
        raise_exc: Exception | None = None,
        result: StageResult | None = None,
    ) -> None:
        self._name = name
        self._version = version
        self.raise_exc = raise_exc
        self._result = result or StageResult(
            output_key="test_output", artifacts=(), metrics={}
        )
        self.called_with: list[StageContext] = []

    @property
    def name(self) -> StageName:
        return self._name

    @property
    def version(self) -> str:
        return self._version

    def run(self, context: StageContext) -> StageResult:
        self.called_with.append(context)
        if self.raise_exc is not None:
            raise self.raise_exc
        return self._result


# ---------------------------------------------------------------------------
# classify_error unit tests
# ---------------------------------------------------------------------------


class TestClassifyError:
    def test_sqlite_busy_is_transient(self):
        err = classify_error(sqlite3.OperationalError("database is locked"), "sample")
        assert err.code == "db_busy"
        assert err.transient is True

    def test_http_429_is_transient(self):
        class MockResp:
            status_code = 429

        class MockHTTPError(Exception):
            def __init__(self, msg):
                super().__init__(msg)
                self.response = MockResp()

        err = classify_error(MockHTTPError("too many requests"), "sample")
        assert err.code == "http_429"
        assert err.transient is True

    def test_http_502_is_transient(self):
        class MockResp:
            status_code = 502

        class MockHTTPError(Exception):
            def __init__(self, msg):
                super().__init__(msg)
                self.response = MockResp()

        err = classify_error(MockHTTPError("bad gateway"), "sample")
        assert err.code == "http_502"
        assert err.transient is True

    def test_http_503_is_transient(self):
        class MockResp:
            status_code = 503

        class MockHTTPError(Exception):
            def __init__(self, msg):
                super().__init__(msg)
                self.response = MockResp()

        err = classify_error(MockHTTPError("unavailable"), "sample")
        assert err.code == "http_503"
        assert err.transient is True

    def test_http_504_is_transient(self):
        class MockResp:
            status_code = 504

        class MockHTTPError(Exception):
            def __init__(self, msg):
                super().__init__(msg)
                self.response = MockResp()

        err = classify_error(MockHTTPError("gateway timeout"), "sample")
        assert err.code == "http_504"
        assert err.transient is True

    def test_http_non_transient_status_not_mapped(self):
        """A non-retryable status code should fall through to default."""
        class MockResp:
            status_code = 403

        class MockHTTPError(Exception):
            def __init__(self, msg):
                super().__init__(msg)
                self.response = MockResp()

        err = classify_error(MockHTTPError("forbidden"), "sample")
        # 403 is NOT in {429, 502, 503, 504} so it should be unknown/transient
        assert err.code == "unknown"
        assert err.transient is True

    def test_oserror_no_such_file_is_attention(self):
        err = classify_error(FileNotFoundError("No such file: /x.mp4"), "sample")
        assert err.code == "invalid_media"
        assert err.transient is False

    def test_oserror_invalid_data_is_attention(self):
        err = classify_error(OSError("Invalid data found"), "sample")
        assert err.code == "invalid_media"
        assert err.transient is False

    def test_oserror_disk_full_is_transient(self):
        err = classify_error(OSError("No space left on device"), "sample")
        assert err.code == "io_error"
        assert err.transient is True

    def test_oserror_io_error_is_transient(self):
        err = classify_error(OSError("Input/output error"), "sample")
        assert err.code == "io_error"
        assert err.transient is True

    def test_oserror_default_is_transient(self):
        err = classify_error(PermissionError("Access denied"), "sample")
        assert err.code == "io_error"
        assert err.transient is True

    def test_ffmpeg_called_process_error_is_attention(self):
        err = classify_error(
            subprocess.CalledProcessError(1, ["ffmpeg", "-i", "x.mp4"]), "sample"
        )
        assert err.code == "ffmpeg_error"
        assert err.transient is False

    def test_non_ffmpeg_called_process_error_is_transient(self):
        err = classify_error(
            subprocess.CalledProcessError(1, ["some_tool", "--flag"]), "sample"
        )
        assert err.code == "process_error"
        assert err.transient is True

    def test_model_not_found_by_class_name_is_attention(self):
        class ModelNotFoundError(Exception):
            pass

        err = classify_error(ModelNotFoundError("model not available"), "vlm")
        assert err.code == "model_not_found"
        assert err.transient is False

    def test_checksum_mismatch_is_attention(self):
        err = classify_error(RuntimeError("sha256 checksum mismatch"), "sample")
        assert err.code == "checksum_mismatch"
        assert err.transient is False

    def test_checksum_mismatch_via_string(self):
        err = classify_error(RuntimeError("Checksum verification failed"), "sample")
        assert err.code == "checksum_mismatch"
        assert err.transient is False

    def test_unknown_exception_defaults_to_transient(self):
        err = classify_error(ValueError("something weird"), "sample")
        assert err.code == "unknown"
        assert err.transient is True


# ---------------------------------------------------------------------------
# TaskWorker behaviour tests
# ---------------------------------------------------------------------------


class TestTaskWorkerRunOnce:
    def test_returns_false_when_no_work(self, tmp_path: Path):
        """No stages in the database → run_once returns False."""
        repo, _ = make_repo(tmp_path)
        worker = TaskWorker(repo, "worker-1", {})
        assert worker.run_once(now=T0) is False

    def test_happy_path(self, tmp_path: Path):
        """Claim a pending stage, run the adapter, complete it."""
        repo, conn = make_repo(tmp_path)
        job = repo.create_job(
            CreateJob(
                directory="C:/video",
                config_json=json.dumps({"task_work_dir": str(tmp_path / "work")}),
            )
        )
        video = repo.add_video(job.job_id, "C:/video/a.mp4", "fp-a")
        stage = repo.ensure_stage(video.video_id, "discover", "input-a")

        adapters = {"discover": MockAdapter()}
        worker = TaskWorker(repo, "worker-1", adapters)

        result = worker.run_once(now=T0)
        assert result is True

        # Stage should be succeeded
        assert stage_status(conn, stage.stage_id) == "succeeded"

        # Adapter should have been called with a valid StageContext
        assert len(adapters["discover"].called_with) == 1
        ctx = adapters["discover"].called_with[0]
        assert ctx.video_id == video.video_id
        assert ctx.job_id == job.job_id
        assert ctx.clip_id is None
        assert ctx.input_key == "input-a"

    def test_passes_clip_id_to_context(self, tmp_path: Path, monkeypatch):
        """clip_id from the stage record flows into StageContext."""
        repo, conn = make_repo(tmp_path)
        job = repo.create_job(
            CreateJob(
                directory="C:/video",
                config_json=json.dumps({"task_work_dir": str(tmp_path / "work")}),
            )
        )
        video = repo.add_video(job.job_id, "C:/video/a.mp4", "fp-a")
        stage = repo.ensure_stage(
            video.video_id, "gif_clip", "input-a", clip_id="clip-1"
        )

        # Mock the resolver to avoid needing rank_dedup artifacts for
        # this unit test.  The test only verifies clip_id propagation.
        monkeypatch.setattr(
            "app.task_engine.artifacts.resolve_stage_inputs",
            lambda *a, **kw: {},
        )

        adapters = {"gif_clip": MockAdapter(name="gif_clip")}
        worker = TaskWorker(repo, "worker-1", adapters)
        worker.run_once(now=T0)

        ctx = adapters["gif_clip"].called_with[0]
        assert ctx.clip_id == "clip-1"

    def test_work_dir_in_context(self, tmp_path: Path):
        """StageContext.work_dir matches config + stage_name + stage_id."""
        repo, _ = make_repo(tmp_path)
        base_dir = tmp_path / "task_work"
        job = repo.create_job(
            CreateJob(
                directory="C:/video",
                config_json=json.dumps({"task_work_dir": str(base_dir)}),
            )
        )
        video = repo.add_video(job.job_id, "C:/video/a.mp4", "fp-a")
        stage = repo.ensure_stage(video.video_id, "discover", "input-a")

        adapters = {"discover": MockAdapter()}
        worker = TaskWorker(repo, "worker-1", adapters)
        worker.run_once(now=T0)

        ctx = adapters["discover"].called_with[0]
        expected = base_dir / "discover" / stage.stage_id
        assert ctx.work_dir == expected

    def test_save_result_file_after_success(self, tmp_path: Path):
        """After a successful run, .stage_result.json should exist."""
        repo, _ = make_repo(tmp_path)
        base_dir = tmp_path / "task_work"
        job = repo.create_job(
            CreateJob(
                directory="C:/video",
                config_json=json.dumps({"task_work_dir": str(base_dir)}),
            )
        )
        video = repo.add_video(job.job_id, "C:/video/a.mp4", "fp-a")
        stage = repo.ensure_stage(video.video_id, "discover", "input-a")

        adapters = {"discover": MockAdapter()}
        worker = TaskWorker(repo, "worker-1", adapters)
        worker.run_once(now=T0)

        result_file = base_dir / "discover" / stage.stage_id / ".stage_result.json"
        assert result_file.exists()
        data = json.loads(result_file.read_text())
        assert data["output_key"] == "test_output"

    def test_transient_error_goes_to_retry_wait(self, tmp_path: Path):
        """A transient error results in retry_wait status."""
        repo, conn = make_repo(tmp_path)
        job = repo.create_job(
            CreateJob(
                directory="C:/video",
                config_json=json.dumps({"task_work_dir": str(tmp_path / "work")}),
            )
        )
        video = repo.add_video(job.job_id, "C:/video/a.mp4", "fp-a")
        stage = repo.ensure_stage(video.video_id, "discover", "input-a")

        adapters = {
            "discover": MockAdapter(
                raise_exc=sqlite3.OperationalError("database is locked")
            )
        }
        worker = TaskWorker(repo, "worker-1", adapters)
        result = worker.run_once(now=T0)

        assert result is True
        assert stage_status(conn, stage.stage_id) == "retry_wait"

    def test_attention_error_goes_to_needs_attention(self, tmp_path: Path):
        """A non-transient error results in needs_attention status."""
        repo, conn = make_repo(tmp_path)
        job = repo.create_job(
            CreateJob(
                directory="C:/video",
                config_json=json.dumps({"task_work_dir": str(tmp_path / "work")}),
            )
        )
        video = repo.add_video(job.job_id, "C:/video/a.mp4", "fp-a")
        stage = repo.ensure_stage(video.video_id, "discover", "input-a")

        adapters = {
            "discover": MockAdapter(
                raise_exc=FileNotFoundError("No such file: /x.mp4")
            )
        }
        worker = TaskWorker(repo, "worker-1", adapters)
        result = worker.run_once(now=T0)

        assert result is True
        assert stage_status(conn, stage.stage_id) == "needs_attention"

    def test_build_context_db_error_caught_and_fails_stage(
        self, tmp_path: Path, monkeypatch
    ):
        """_build_context raising sqlite3.OperationalError is caught and retry_wait."""
        repo, conn = make_repo(tmp_path)
        job = repo.create_job(
            CreateJob(
                directory="C:/video",
                config_json=json.dumps({"task_work_dir": str(tmp_path / "work")}),
            )
        )
        video = repo.add_video(job.job_id, "C:/video/a.mp4", "fp-a")
        stage = repo.ensure_stage(video.video_id, "discover", "input-a")

        adapters = {"discover": MockAdapter()}
        worker = TaskWorker(repo, "worker-1", adapters)

        def _crashing_build(self, stage):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(TaskWorker, "_build_context", _crashing_build)

        # Should NOT raise -- caught by run_once try/except
        result = worker.run_once(now=T0)
        assert result is True

        # Stage should be retry_wait (transient error)
        assert stage_status(conn, stage.stage_id) == "retry_wait"

    def test_cancellation_marks_stage_cancelled(self, tmp_path: Path):
        """A pending cancel command → stage is cancelled, adapter not called."""
        repo, conn = make_repo(tmp_path)
        job = repo.create_job(
            CreateJob(
                directory="C:/video",
                config_json=json.dumps({"task_work_dir": str(tmp_path / "work")}),
            )
        )
        video = repo.add_video(job.job_id, "C:/video/a.mp4", "fp-a")
        stage = repo.ensure_stage(video.video_id, "discover", "input-a")

        # Add a cancel command
        repo.append_command(job.job_id, "cancel", {"reason": "user_request"})

        mock_adapter = MockAdapter()
        adapters = {"discover": mock_adapter}
        worker = TaskWorker(repo, "worker-1", adapters)
        result = worker.run_once(now=T0)

        assert result is True
        assert stage_status(conn, stage.stage_id) == "cancelled"
        # Adapter should NOT have been called
        assert len(mock_adapter.called_with) == 0

    def test_unknown_adapter_fails_with_attention(self, tmp_path: Path):
        """Stage with no matching adapter → needs_attention."""
        repo, conn = make_repo(tmp_path)
        job = repo.create_job(
            CreateJob(
                directory="C:/video",
                config_json=json.dumps({"task_work_dir": str(tmp_path / "work")}),
            )
        )
        video = repo.add_video(job.job_id, "C:/video/a.mp4", "fp-a")
        repo.ensure_stage(video.video_id, "discover", "input-a")

        # No adapter for "sample"
        worker = TaskWorker(repo, "worker-1", {})
        result = worker.run_once(now=T0)

        assert result is True
        # The stage should have a lease owner — fail_stage clears it
        row = conn.execute(
            "SELECT status, last_error_json FROM task_stages WHERE stage_name='discover'"
        ).fetchone()
        assert row["status"] == "needs_attention"
        assert "unknown_stage" in row["last_error_json"]

    def test_expired_lease_recovery_valid(self, tmp_path: Path):
        """Reclaim an expired-lease stage with valid artifacts → recover."""
        repo, conn = make_repo(tmp_path)
        base_dir = tmp_path / "task_work"
        job = repo.create_job(
            CreateJob(
                directory="C:/video",
                config_json=json.dumps({"task_work_dir": str(base_dir)}),
            )
        )
        video = repo.add_video(job.job_id, "C:/video/a.mp4", "fp-a")
        stage = repo.ensure_stage(video.video_id, "discover", "input-a")

        # First claim — simulate work that created artifacts + result file
        first_claim = repo.claim_stage("worker-a", T0)
        ctx_work_dir = (
            base_dir / first_claim.stage_name / first_claim.stage_id
        )
        ctx_work_dir.mkdir(parents=True)

        # Create a fake artifact
        artifact_path = ctx_work_dir / "output.mp4"
        artifact_path.write_text("fake-video-content")
        from app.task_engine.fingerprints import sha256_file

        artifact_sha = sha256_file(artifact_path)

        # Write the .stage_result.json as if a real run had completed
        result_data = {
            "schema_version": 1,
            "stage_id": first_claim.stage_id,
            "stage_name": "discover",
            "output_key": "recovered_output",
            "artifacts": [
                {
                    "artifact_id": "a1",
                    "job_id": job.job_id,
                    "video_id": video.video_id,
                    "stage_name": "discover",
                    "clip_id": None,
                    "path": str(artifact_path),
                    "sha256": artifact_sha,
                    "size_bytes": artifact_path.stat().st_size,
                    "provenance_json": "{}",
                    "stage_id": first_claim.stage_id,
                    "artifact_kind": "discover_manifest",
                }
            ],
            "metrics": {},
        }
        (ctx_work_dir / ".stage_result.json").write_text(
            json.dumps(result_data)
        )

        # Expire the lease
        conn.execute(
            "UPDATE task_stages SET lease_expires_at=? WHERE stage_id=?",
            ("2020-01-01T00:00:00.000000+00:00", stage.stage_id),
        )
        conn.commit()

        # Second worker claims the expired stage
        mock_adapter = MockAdapter()
        adapters = {"discover": mock_adapter}
        worker = TaskWorker(repo, "worker-b", adapters)
        result = worker.run_once(now=T0 + timedelta(hours=1))

        assert result is True
        # Stage should be succeeded via recovery
        assert stage_status(conn, stage.stage_id) == "succeeded"
        # Adapter should NOT have been called (recovery path was taken)
        assert len(mock_adapter.called_with) == 0

    def test_expired_lease_recovery_missing_artifact_reruns(
        self, tmp_path: Path
    ):
        """Reclaim with a result file but missing artifact file → rerun."""
        repo, conn = make_repo(tmp_path)
        base_dir = tmp_path / "task_work"
        job = repo.create_job(
            CreateJob(
                directory="C:/video",
                config_json=json.dumps({"task_work_dir": str(base_dir)}),
            )
        )
        video = repo.add_video(job.job_id, "C:/video/a.mp4", "fp-a")
        stage = repo.ensure_stage(video.video_id, "discover", "input-a")

        # First claim with artifact result file pointing to non-existent path
        first_claim = repo.claim_stage("worker-a", T0)
        ctx_work_dir = (
            base_dir / first_claim.stage_name / first_claim.stage_id
        )
        ctx_work_dir.mkdir(parents=True)

        # Write result file pointing to a missing artifact
        missing_path = ctx_work_dir / "missing.mp4"
        result_data = {
            "output_key": "bad_output",
            "artifacts": [
                {
                    "artifact_id": "a1",
                    "job_id": job.job_id,
                    "video_id": video.video_id,
                    "stage_name": "discover",
                    "clip_id": None,
                    "path": str(missing_path),
                    "sha256": "0" * 64,
                    "size_bytes": 100,
                    "provenance_json": "{}",
                }
            ],
            "metrics": {},
        }
        (ctx_work_dir / ".stage_result.json").write_text(
            json.dumps(result_data)
        )

        # Expire the lease
        conn.execute(
            "UPDATE task_stages SET lease_expires_at=? WHERE stage_id=?",
            ("2020-01-01T00:00:00.000000+00:00", stage.stage_id),
        )
        conn.commit()

        # Second worker — recovery should fail → rerun
        mock_adapter = MockAdapter()
        adapters = {"discover": mock_adapter}
        worker = TaskWorker(repo, "worker-b", adapters)
        result = worker.run_once(now=T0 + timedelta(hours=1))

        assert result is True
        assert stage_status(conn, stage.stage_id) == "succeeded"
        # Adapter SHOULD have been called (recovery failed, rerun happened)
        assert len(mock_adapter.called_with) == 1

    def test_expired_lease_recovery_checksum_mismatch_reruns(
        self, tmp_path: Path
    ):
        """Reclaim with a result file but wrong sha256 → rerun."""
        repo, conn = make_repo(tmp_path)
        base_dir = tmp_path / "task_work"
        job = repo.create_job(
            CreateJob(
                directory="C:/video",
                config_json=json.dumps({"task_work_dir": str(base_dir)}),
            )
        )
        video = repo.add_video(job.job_id, "C:/video/a.mp4", "fp-a")
        stage = repo.ensure_stage(video.video_id, "discover", "input-a")

        first_claim = repo.claim_stage("worker-a", T0)
        ctx_work_dir = (
            base_dir / first_claim.stage_name / first_claim.stage_id
        )
        ctx_work_dir.mkdir(parents=True)

        artifact_path = ctx_work_dir / "output.mp4"
        artifact_path.write_text("original-content")

        # Result file says wrong sha256
        result_data = {
            "output_key": "bad_output",
            "artifacts": [
                {
                    "artifact_id": "a1",
                    "job_id": job.job_id,
                    "video_id": video.video_id,
                    "stage_name": "discover",
                    "clip_id": None,
                    "path": str(artifact_path),
                    "sha256": "0" * 64,
                    "size_bytes": artifact_path.stat().st_size,
                    "provenance_json": "{}",
                }
            ],
            "metrics": {},
        }
        (ctx_work_dir / ".stage_result.json").write_text(
            json.dumps(result_data)
        )

        # Expire the lease
        conn.execute(
            "UPDATE task_stages SET lease_expires_at=? WHERE stage_id=?",
            ("2020-01-01T00:00:00.000000+00:00", stage.stage_id),
        )
        conn.commit()

        mock_adapter = MockAdapter()
        adapters = {"discover": mock_adapter}
        worker = TaskWorker(repo, "worker-b", adapters)
        result = worker.run_once(now=T0 + timedelta(hours=1))

        assert result is True
        assert stage_status(conn, stage.stage_id) == "succeeded"
        assert len(mock_adapter.called_with) == 1

    def test_reclaim_without_artifact_file_reruns(self, tmp_path: Path):
        """Expired lease with no artifacts at all → rerun."""
        repo, conn = make_repo(tmp_path)
        base_dir = tmp_path / "task_work"
        job = repo.create_job(
            CreateJob(
                directory="C:/video",
                config_json=json.dumps({"task_work_dir": str(base_dir)}),
            )
        )
        video = repo.add_video(job.job_id, "C:/video/a.mp4", "fp-a")
        stage = repo.ensure_stage(video.video_id, "discover", "input-a")

        # First claim — create work dir but no result file
        first_claim = repo.claim_stage("worker-a", T0)
        ctx_work_dir = (
            base_dir / first_claim.stage_name / first_claim.stage_id
        )
        ctx_work_dir.mkdir(parents=True)

        # Expire the lease
        conn.execute(
            "UPDATE task_stages SET lease_expires_at=? WHERE stage_id=?",
            ("2020-01-01T00:00:00.000000+00:00", stage.stage_id),
        )
        conn.commit()

        mock_adapter = MockAdapter()
        adapters = {"discover": mock_adapter}
        worker = TaskWorker(repo, "worker-b", adapters)
        result = worker.run_once(now=T0 + timedelta(hours=1))

        assert result is True
        assert stage_status(conn, stage.stage_id) == "succeeded"
        assert len(mock_adapter.called_with) == 1

    def test_concurrent_cancel_between_claim_and_run(self, tmp_path: Path):
        """Cancel command inserted concurrently is respected."""
        repo, conn = make_repo(tmp_path)
        job = repo.create_job(
            CreateJob(
                directory="C:/video",
                config_json=json.dumps({"task_work_dir": str(tmp_path / "work")}),
            )
        )
        video = repo.add_video(job.job_id, "C:/video/a.mp4", "fp-a")
        repo.ensure_stage(video.video_id, "discover", "input-a")

        # Insert a cancel command (simulating concurrent cancellation)
        repo.append_command(job.job_id, "cancel", {"reason": "concurrent"})

        mock_adapter = MockAdapter()
        worker = TaskWorker(repo, "worker-1", {"discover": mock_adapter})
        result = worker.run_once(now=T0)

        assert result is True
        assert len(mock_adapter.called_with) == 0


class TestTaskWorkerDrain:
    def test_drain_processes_all_stages(self, tmp_path: Path, monkeypatch):
        """drain processes all pending stages and returns count."""
        repo, conn = make_repo(tmp_path)
        job = repo.create_job(
            CreateJob(
                directory="C:/video",
                config_json=json.dumps({"task_work_dir": str(tmp_path / "work")}),
            )
        )
        video = repo.add_video(job.job_id, "C:/video/a.mp4", "fp-a")
        repo.ensure_stage(video.video_id, "discover", "input-a")
        repo.ensure_stage(video.video_id, "vlm", "input-b")

        # Mock resolver so vlm stage doesn't need real upstream artifacts.
        monkeypatch.setattr(
            "app.task_engine.artifacts.resolve_stage_inputs",
            lambda *a, **kw: {},
        )

        adapters = {
            "discover": MockAdapter(
                name="discover", result=StageResult("sample_out", (), {})
            ),
            "vlm": MockAdapter(
                name="vlm", result=StageResult("vlm_out", (), {})
            ),
        }
        worker = TaskWorker(repo, "worker-1", adapters)
        count = worker.drain()

        assert count >= 2  # orchestrator may create extra advanced stages
        # Verify the stages we explicitly created completed.
        assert stage_status(conn, repo.ensure_stage(video.video_id, "discover", "input-a").stage_id) == "succeeded"
        assert stage_status(conn, repo.ensure_stage(video.video_id, "vlm", "input-b").stage_id) == "succeeded"

    def test_returns_zero_when_idle(self, tmp_path: Path):
        """run_forever on empty DB returns 0."""
        repo, _ = make_repo(tmp_path)
        worker = TaskWorker(repo, "worker-1", {})
        assert worker.drain() == 0


class TestTaskWorkerHeartbeat:
    def test_extends_lease(self, tmp_path: Path):
        """heartbeat pushes lease_expires_at forward."""
        repo, conn = make_repo(tmp_path)
        job = repo.create_job(
            CreateJob(
                directory="C:/video",
                config_json=json.dumps({"task_work_dir": str(tmp_path / "work")}),
            )
        )
        video = repo.add_video(job.job_id, "C:/video/a.mp4", "fp-a")
        repo.ensure_stage(video.video_id, "discover", "input-a")

        claimed = repo.claim_stage("worker-1", T0)

        worker = TaskWorker(repo, "worker-1", {"discover": MockAdapter()})
        worker.heartbeat(claimed.stage_id, now=T0 + timedelta(seconds=30))

        row = conn.execute(
            "SELECT lease_expires_at FROM task_stages WHERE stage_id=?",
            (claimed.stage_id,),
        ).fetchone()
        expires = datetime.fromisoformat(row[0])
        # Original expiry was T0 + 90s, heartbeat at T0 + 30s moves it
        # to T0 + 30s + 90s = T0 + 120s
        expected_delta = (expires - T0).total_seconds()
        assert abs(expected_delta - 120) < 2.0

    def test_requires_correct_owner(self, tmp_path: Path):
        """heartbeat only extends the lease for the owning worker."""
        repo, conn = make_repo(tmp_path)
        job = repo.create_job(
            CreateJob(
                directory="C:/video",
                config_json=json.dumps({"task_work_dir": str(tmp_path / "work")}),
            )
        )
        video = repo.add_video(job.job_id, "C:/video/a.mp4", "fp-a")
        repo.ensure_stage(video.video_id, "discover", "input-a")

        claimed = repo.claim_stage("worker-1", T0)
        original_expiry = conn.execute(
            "SELECT lease_expires_at FROM task_stages WHERE stage_id=?",
            (claimed.stage_id,),
        ).fetchone()[0]

        # Wrong worker tries to heartbeat
        worker = TaskWorker(repo, "worker-2", {})
        worker.heartbeat(claimed.stage_id, now=T0 + timedelta(seconds=30))

        # Lease should NOT have changed
        current_expiry = conn.execute(
            "SELECT lease_expires_at FROM task_stages WHERE stage_id=?",
            (claimed.stage_id,),
        ).fetchone()[0]
        assert current_expiry == original_expiry

    def test_heartbeat_uses_immediate_transaction(self, tmp_path: Path):
        """heartbeat uses BEGIN IMMEDIATE before the UPDATE."""
        repo, conn = make_repo(tmp_path)
        job = repo.create_job(
            CreateJob(
                directory="C:/video",
                config_json=json.dumps({"task_work_dir": str(tmp_path / "work")}),
            )
        )
        video = repo.add_video(job.job_id, "C:/video/a.mp4", "fp-a")
        repo.ensure_stage(video.video_id, "discover", "input-a")
        claimed = repo.claim_stage("worker-1", T0)

        worker = TaskWorker(repo, "worker-1", {"discover": MockAdapter()})

        # Wrap the connection so we can spy on execute calls
        mock_conn = MagicMock(wraps=conn)
        repo._conn = mock_conn

        worker.heartbeat(claimed.stage_id)

        immediate_calls = [
            c for c in mock_conn.execute.call_args_list
            if c[0]
            and isinstance(c[0][0], str)
            and c[0][0].strip().upper() == "BEGIN IMMEDIATE"
        ]
        assert len(immediate_calls) == 1, (
            "heartbeat should call BEGIN IMMEDIATE exactly once"
        )

    def test_heartbeat_silently_ignores_db_locked(self, tmp_path: Path):
        """heartbeat silently ignores sqlite3.OperationalError('database is locked')."""
        repo, conn = make_repo(tmp_path)
        job = repo.create_job(
            CreateJob(
                directory="C:/video",
                config_json=json.dumps({"task_work_dir": str(tmp_path / "work")}),
            )
        )
        video = repo.add_video(job.job_id, "C:/video/a.mp4", "fp-a")
        repo.ensure_stage(video.video_id, "discover", "input-a")
        claimed = repo.claim_stage("worker-1", T0)

        worker = TaskWorker(repo, "worker-1", {"discover": MockAdapter()})

        # Open a second connection and hold a RESERVED lock so that the
        # worker's BEGIN IMMEDIATE will hit "database is locked".
        conn2 = sqlite3.connect(str(tmp_path / "task.db"))
        conn2.execute("BEGIN IMMEDIATE")

        # Reduce busy timeout on the worker connection to avoid a 5 s wait
        conn.execute("PRAGMA busy_timeout=100")

        # Should NOT raise (silently ignored); the lease will expire and
        # another worker will reclaim the stage.
        try:
            worker.heartbeat(claimed.stage_id)
        finally:
            conn2.rollback()
            conn2.close()


class TestStageErrorFields:
    def test_transient_error_has_correct_fields(self):
        err = StageError("db_busy", "database is locked", transient=True)
        assert err.code == "db_busy"
        assert err.message == "database is locked"
        assert err.transient is True

    def test_attention_error_has_correct_fields(self):
        err = StageError(
            "invalid_media", "file not found", transient=False
        )
        assert err.code == "invalid_media"
        assert err.message == "file not found"
        assert err.transient is False


class TestCheckCancelled:
    """_check_cancelled uses BEGIN IMMEDIATE before the UPDATE."""

    def test_uses_immediate_transaction(self, tmp_path: Path):
        """_check_cancelled uses BEGIN IMMEDIATE before the cancellation UPDATE."""
        repo, conn = make_repo(tmp_path)
        job = repo.create_job(
            CreateJob(
                directory="C:/video",
                config_json=json.dumps({"task_work_dir": str(tmp_path / "work")}),
            )
        )
        video = repo.add_video(job.job_id, "C:/video/a.mp4", "fp-a")
        stage = repo.ensure_stage(video.video_id, "discover", "input-a")
        repo.append_command(job.job_id, "cancel", {"reason": "test"})

        claimed = repo.claim_stage("worker-1", T0)
        worker = TaskWorker(repo, "worker-1", {"discover": MockAdapter()})

        # Wrap the connection so we can spy on execute calls
        mock_conn = MagicMock(wraps=conn)
        repo._conn = mock_conn

        worker._check_cancelled(claimed, T0)

        immediate_calls = [
            c for c in mock_conn.execute.call_args_list
            if c[0]
            and isinstance(c[0][0], str)
            and c[0][0].strip().upper() == "BEGIN IMMEDIATE"
        ]
        assert len(immediate_calls) == 1, (
            "_check_cancelled should call BEGIN IMMEDIATE exactly once"
        )


class TestRunOnceMultipleJobs:
    def test_multiple_jobs_independent(self, tmp_path: Path, monkeypatch):
        """Stages from different jobs are processed independently."""
        repo, conn = make_repo(tmp_path)
        base = str(tmp_path / "work")

        job1 = repo.create_job(
            CreateJob(directory="C:/a/", config_json=json.dumps({"task_work_dir": base}))
        )
        v1 = repo.add_video(job1.job_id, "C:/a/1.mp4", "fp-1")
        repo.ensure_stage(v1.video_id, "discover", "in-a")

        job2 = repo.create_job(
            CreateJob(directory="C:/b/", config_json=json.dumps({"task_work_dir": base}))
        )
        v2 = repo.add_video(job2.job_id, "C:/b/2.mp4", "fp-2")
        repo.ensure_stage(v2.video_id, "vlm", "in-b")

        # Mock resolver so vlm stage doesn't need real upstream artifacts.
        monkeypatch.setattr(
            "app.task_engine.artifacts.resolve_stage_inputs",
            lambda *a, **kw: {},
        )

        adapters = {
            "discover": MockAdapter(name="discover"),
            "vlm": MockAdapter(name="vlm"),
        }
        worker = TaskWorker(repo, "worker-1", adapters)
        count = worker.drain()
        assert count >= 2
        assert stage_status(conn, repo.ensure_stage(v1.video_id, "discover", "in-a").stage_id) == "succeeded"
        assert stage_status(conn, repo.ensure_stage(v2.video_id, "vlm", "in-b").stage_id) == "succeeded"
