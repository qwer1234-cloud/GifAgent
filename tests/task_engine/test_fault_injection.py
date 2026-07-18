from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.task_engine import (
    CreateJob,
    TaskRepository,
    TaskWorker,
    connect_task_db,
)
from app.task_engine.fingerprints import sha256_file
from app.task_engine.stages import StageContext, StageResult

# ---------------------------------------------------------------------------
# Shared helpers
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


def expire_lease(conn: sqlite3.Connection, stage_id: str) -> None:
    conn.execute(
        "UPDATE task_stages SET lease_expires_at=? WHERE stage_id=?",
        ("2020-01-01T00:00:00.000000+00:00", stage_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Mock adapter
# ---------------------------------------------------------------------------


class MockAdapter:
    def __init__(self, name: str = "discover", version: str = "1", raise_exc=None):
        self._name = name
        self._version = version
        self.raise_exc = raise_exc
        self.called_with: list[StageContext] = []

    @property
    def name(self):
        return self._name

    @property
    def version(self):
        return self._version

    def run(self, context):
        self.called_with.append(context)
        if self.raise_exc is not None:
            raise self.raise_exc
        return StageResult(output_key="test_output", artifacts=(), metrics={})


# ---------------------------------------------------------------------------
# Fixture: a minimal job + video + single pending stage
# ---------------------------------------------------------------------------


@pytest.fixture
def stage_env(tmp_path: Path):
    """Create a repo with one job, one video, one pending stage.

    Returns a dict with keys: repo, conn, job, video, stage, tmp_path,
    base_dir.
    """
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
    return {
        "repo": repo,
        "conn": conn,
        "job": job,
        "video": video,
        "stage": stage,
        "tmp_path": tmp_path,
        "base_dir": base_dir,
    }


# ===========================================================================
# Crash injection tests — simulate process death at every lifecycle phase
# ===========================================================================


def _make_crash_once(wrapper_attr, crash_msg="Simulated crash"):
    """Return a replacement method that crashes on first call, then
    delegates to the original on subsequent calls.

    Parameters
    ----------
    wrapper_attr:
        Dotted attribute path on ``TaskWorker`` to wrap, e.g.
        ``"run_once"``, ``"_check_cancelled"``.
    crash_msg:
        Exception message for the crash.
    """
    parts = wrapper_attr.split(".")
    orig = getattr(TaskWorker, parts[-1])
    call_count = [0]

    def repl(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            raise RuntimeError(crash_msg)
        return orig(*args, **kwargs)

    return repl


class TestCrashInjection:
    """Verify that a process crash at any point leaves the stage in a
    recoverable state (leased with an expiring lease), and that recovery
    succeeds after the lease expires."""

    @pytest.mark.parametrize(
        "crash_point,crash_msg,setup_adapter,expected_after_crash,can_recover",
        [
            # ---- Phase 1: crash BEFORE claim raises (worker never
            # gets a stage, no change to DB). ----
            pytest.param(
                "claim_raises", "Simulated DB failure on claim",
                None, "pending", False,
                id="1-before-claim",
            ),
            # ---- Phase 2: crash AFTER claim succeeds — the stage is
            # leased.  We simulate process death by having the cancel
            # check raise. ----
            pytest.param(
                "crash_after_claim", "Simulated crash after claim",
                None, "leased", True,
                id="2-after-claim",
            ),
            # ---- Phase 3: crash in context builder (process dies
            # while resolving the stage).  After the fix this is caught
            # by the run_once try/except and the stage is failed with
            # a transient error. ----
            pytest.param(
                "crash_during_context", "Simulated crash during context",
                None, "retry_wait", True,
                id="3-during-context",
            ),
            # ---- Phase 4: crash when recovery is attempted on a
            # reclaimed stage that has no result file (the recover
            # callback will be triggered because attempt_count > 1).
            # After the fix this is caught by the run_once try/except
            # and the stage is failed with a transient error. ----
            pytest.param(
                "crash_during_recovery", "Simulated crash during recovery",
                None, "retry_wait", True,
                id="4-during-recovery",
            ),
            # ---- Phase 5: adapter raises a transient error (this is
            # NOT a process crash — handled gracefully by the worker). ----
            pytest.param(
                "adapter_transient", "",
                sqlite3.OperationalError("database is locked"),
                "retry_wait", True,
                id="5-adapter-transient",
            ),
            # ---- Phase 6: adapter raises an attention error. ----
            pytest.param(
                "adapter_attention", "",
                FileNotFoundError("No such file: /x.mp4"),
                "needs_attention", False,
                id="6-adapter-attention",
            ),
            # ---- Phase 7: crash after a successful adapter run,
            # before the result file is saved. ----
            pytest.param(
                "crash_before_save_result",
                "Simulated crash before save result",
                None, "retry_wait", True,
                id="7-before-save-result",
            ),
            # ---- Phase 8: crash after result file is saved, before
            # complete_stage commits — the result file exists, so
            # recovery should find it. ----
            pytest.param(
                "crash_after_save_result",
                "Simulated crash after save, before complete",
                None, "retry_wait", True,
                id="8-after-save-result",
            ),
            # ---- Phase 9: crash during complete_stage (DB commit
            # failure). ----
            pytest.param(
                "crash_during_complete",
                "Simulated DB crash during complete",
                None, "retry_wait", True,
                id="9-during-complete",
            ),
        ],
    )
    def test_crash_at_phase(
        self,
        stage_env,
        crash_point: str,
        crash_msg: str,
        setup_adapter,
        expected_after_crash: str,
        can_recover: bool,
        monkeypatch,
    ):
        """Crash at a specific lifecycle phase and verify recovery."""
        env = stage_env
        repo = env["repo"]
        conn = env["conn"]
        stage = env["stage"]
        stage_id = stage.stage_id

        # ---------------------------------------------------------------
        # Inject the crash at the requested point.
        #
        # IMPORTANT: all monkeypatches are applied globally (they affect
        # ALL TaskWorker instances).  We use a "crash-once" wrapper that
        # crashes the first time it is called and delegates to the
        # original on subsequent calls so that the recovery worker can
        # run normally.
        # ---------------------------------------------------------------

        if crash_point == "claim_raises":
            orig_claim = repo.claim_stage
            claim_count = [0]

            def _crashing_claim(*args, **kwargs):
                claim_count[0] += 1
                if claim_count[0] == 1:
                    raise RuntimeError(crash_msg)
                return orig_claim(*args, **kwargs)

            monkeypatch.setattr(repo, "claim_stage", _crashing_claim)

        elif crash_point == "crash_after_claim":
            repl = _make_crash_once(
                "_check_cancelled", crash_msg
            )
            monkeypatch.setattr(
                TaskWorker, "_check_cancelled", repl
            )

        elif crash_point == "crash_during_context":
            repl = _make_crash_once(
                "_build_context", crash_msg
            )
            monkeypatch.setattr(
                TaskWorker, "_build_context", repl
            )

        elif crash_point == "crash_during_recovery":
            # First, claim the stage and expire the lease so that
            # attempt_count > 1, which triggers the recovery path.
            first = repo.claim_stage("worker-a", T0)
            expire_lease(conn, stage_id)

            repl = _make_crash_once(
                "_try_recover", crash_msg
            )
            monkeypatch.setattr(
                TaskWorker, "_try_recover", repl
            )

        elif crash_point == "crash_before_save_result":
            repl = _make_crash_once(
                "_save_result", crash_msg
            )
            monkeypatch.setattr(
                TaskWorker, "_save_result", repl
            )

        elif crash_point == "crash_after_save_result":
            # Let the real _save_result execute, then crash right
            # after.  We need to capture the original first.
            orig_save = TaskWorker._save_result
            save_count = [0]

            def _save_then_crash(self, work_dir, result, stage):
                save_count[0] += 1
                if save_count[0] == 1:
                    orig_save(self, work_dir, result, stage)
                    raise RuntimeError(crash_msg)
                return orig_save(self, work_dir, result, stage)

            monkeypatch.setattr(
                TaskWorker, "_save_result", _save_then_crash
            )

        elif crash_point == "crash_during_complete":
            orig_complete = repo.complete_stage_with_artifacts
            complete_count = [0]

            def _crashing_complete(*args, **kwargs):
                complete_count[0] += 1
                if complete_count[0] == 1:
                    raise RuntimeError(crash_msg)
                return orig_complete(*args, **kwargs)

            monkeypatch.setattr(
                repo, "complete_stage_with_artifacts", _crashing_complete
            )

        # ---------------------------------------------------------------
        # Build adapter and worker
        # ---------------------------------------------------------------

        adapter = MockAdapter(raise_exc=setup_adapter)
        adapters = {"discover": adapter}

        worker = TaskWorker(repo, "worker-1", adapters)

        # ---------------------------------------------------------------
        # Run once — expect a crash (for process-death points) or a
        # normal return (for handled errors).
        # ---------------------------------------------------------------

        # Points that are OUTSIDE the try/except in run_once — these
        # still propagate as uncaught RuntimeError.
        is_crash = crash_point in (
            "claim_raises",
            "crash_after_claim",
        )

        if is_crash:
            with pytest.raises(RuntimeError):
                worker.run_once(now=T0)
        else:
            # Handled errors — worker.run_once returns True (did work)
            result = worker.run_once(now=T0)
            assert result is True

        # ---------------------------------------------------------------
        # Assert post-crash invariants
        # ---------------------------------------------------------------

        status = stage_status(conn, stage_id)
        assert status == expected_after_crash, (
            f"Expected status {expected_after_crash!r} after "
            f"{crash_point}, got {status!r}"
        )

        # ---------------------------------------------------------------
        # Recovery verification for leased and retry_wait stages
        # ---------------------------------------------------------------

        if expected_after_crash == "leased" and can_recover:
            expire_lease(conn, stage_id)

            recovery_adapter = MockAdapter()
            recovery_worker = TaskWorker(
                repo, "worker-recovery", {"discover": recovery_adapter}
            )
            recovery_result = recovery_worker.run_once(
                now=T0 + timedelta(hours=1)
            )
            assert recovery_result is True, (
                f"Recovery should succeed after {crash_point}"
            )
            final_status = stage_status(conn, stage_id)
            assert final_status == "succeeded", (
                f"Expected 'succeeded' after recovery from "
                f"{crash_point}, got {final_status!r}"
            )

        elif expected_after_crash == "retry_wait" and can_recover:
            # Transient error — claim after backoff
            row = conn.execute(
                "SELECT retry_at FROM task_stages WHERE stage_id=?",
                (stage_id,),
            ).fetchone()
            assert row is not None
            retry_at = datetime.fromisoformat(row[0])

            recovery_worker = TaskWorker(
                repo, "worker-recovery", {"discover": MockAdapter()}
            )
            recovery_worker.run_once(
                now=retry_at + timedelta(seconds=1)
            )
            assert stage_status(conn, stage_id) == "succeeded"


# ===========================================================================
# Focused recovery tests with artifact validation
# ===========================================================================


class TestRecoveryWithArtifacts:
    """Verify artifact validation during lease-expiry recovery."""

    @pytest.mark.parametrize(
        "scenario,setup_artifacts,expect_recover",
        [
            pytest.param(
                "valid_artifacts",
                lambda wd, j, stage_id="": _create_valid_result(wd, j, stage_id=stage_id),
                True,
                id="rec-1-valid-artifacts",
            ),
            pytest.param(
                "no_result_file",
                lambda wd, j, stage_id="": None,
                False,
                id="rec-2-no-result-file",
            ),
            pytest.param(
                "missing_artifact_file",
                lambda wd, j, stage_id="": _create_missing_artifact_result(wd, j, stage_id=stage_id),
                False,
                id="rec-3-missing-artifact",
            ),
            pytest.param(
                "checksum_mismatch",
                lambda wd, j, stage_id="": _create_checksum_mismatch_result(wd, j, stage_id=stage_id),
                False,
                id="rec-4-checksum-mismatch",
            ),
            pytest.param(
                "corrupt_result_json",
                lambda wd, j, stage_id="": _create_corrupt_result(wd),
                False,
                id="rec-5-corrupt-json",
            ),
        ],
    )
    def test_recovery_scenario(
        self, stage_env, scenario: str, setup_artifacts, expect_recover: bool
    ):
        env = stage_env
        repo = env["repo"]
        conn = env["conn"]
        job = env["job"]
        stage = env["stage"]
        base_dir = env["base_dir"]

        # Claim the stage (first worker)
        first = repo.claim_stage("worker-a", T0)
        stage_id = first.stage_id
        work_dir = base_dir / "discover" / stage_id
        work_dir.mkdir(parents=True, exist_ok=True)

        # Setup artifacts according to scenario
        setup_artifacts(work_dir, job, stage_id=stage_id)

        # Expire the lease
        expire_lease(conn, stage_id)

        # Recovery worker
        adapter = MockAdapter()
        worker = TaskWorker(repo, "worker-recovery", {"discover": adapter})
        result = worker.run_once(now=T0 + timedelta(hours=1))

        assert result is True

        final_status = stage_status(conn, stage_id)
        if expect_recover:
            assert final_status == "succeeded", (
                f"Should have recovered in {scenario}, got {final_status}"
            )
            assert len(adapter.called_with) == 0, (
                "Recovery should NOT have called the adapter"
            )
        else:
            assert final_status == "succeeded", (
                f"Should have re-run in {scenario}, got {final_status}"
            )
            assert len(adapter.called_with) == 1, (
                "Non-recovery should re-run the adapter"
            )


# ---------------------------------------------------------------------------
# Helper: artifact setup functions
# ---------------------------------------------------------------------------


def _create_valid_result(work_dir: Path, job, video=None, stage_id=""):
    artifact = work_dir / "output.mp4"
    artifact.write_text("valid-content")
    sha = sha256_file(artifact)
    _write_result_file(
        work_dir,
        "recovered_output",
        [
            {
                "artifact_id": "a1",
                "job_id": job.job_id,
                "video_id": "",
                "stage_name": "discover",
                "clip_id": None,
                "path": str(artifact),
                "sha256": sha,
                "size_bytes": artifact.stat().st_size,
                "provenance_json": "{}",
                "stage_id": stage_id,
                "artifact_kind": "discover_manifest",
            }
        ],
        stage_id=stage_id,
    )


def _create_missing_artifact_result(work_dir: Path, job, video=None, stage_id=""):
    missing = work_dir / "missing.mp4"
    _write_result_file(
        work_dir,
        "bad_output",
        [
            {
                "artifact_id": "a1",
                "job_id": job.job_id,
                "video_id": "",
                "stage_name": "discover",
                "clip_id": None,
                "path": str(missing),
                "sha256": "0" * 64,
                "size_bytes": 100,
                "provenance_json": "{}",
                "stage_id": stage_id,
                "artifact_kind": "discover_manifest",
            }
        ],
        stage_id=stage_id,
    )


def _create_checksum_mismatch_result(work_dir: Path, job, video=None, stage_id=""):
    artifact = work_dir / "output.mp4"
    artifact.write_text("real-content")
    _write_result_file(
        work_dir,
        "bad_output",
        [
            {
                "artifact_id": "a1",
                "job_id": job.job_id,
                "video_id": "",
                "stage_name": "discover",
                "clip_id": None,
                "path": str(artifact),
                "sha256": "0" * 64,
                "size_bytes": artifact.stat().st_size,
                "provenance_json": "{}",
                "stage_id": stage_id,
                "artifact_kind": "discover_manifest",
            }
        ],
        stage_id=stage_id,
    )


def _create_corrupt_result(work_dir: Path):
    (work_dir / ".stage_result.json").write_text("not valid json{{{")


def _write_result_file(
    work_dir: Path, output_key: str, artifacts: list[dict], stage_id: str = ""
) -> None:
    data = {
        "schema_version": 1,
        "stage_id": stage_id,
        "stage_name": "discover",
        "output_key": output_key,
        "artifacts": artifacts,
        "metrics": {},
    }
    (work_dir / ".stage_result.json").write_text(json.dumps(data))
