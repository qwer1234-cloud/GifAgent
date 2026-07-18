"""Fifth-review §3 (P0-1) + §6 (P1-2) RED tests: outcome recovery contract.

A stage that completed its work but returned ``outcome="needs_attention"``
(e.g. materialize with unrecoverable publish conflicts) must STAY
``needs_attention`` across a worker crash:

* the ``.stage_result.json`` written by ``_save_result`` must record the
  outcome so a later ``_try_recover`` reproduces it;
* recovery must call ``complete_stage_with_artifacts`` with the same
  ``needs_attention`` flag as the normal commit path (unified helper);
* an unknown outcome value in the result file (or returned by an adapter)
  must NEVER silently map to ``succeeded``.

These tests target the recovery + outcome contract and are expected to
FAIL before the P0-1 / P1-2 fixes land.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.task_engine import (
    CreateJob,
    TaskRepository,
    TaskWorker,
    connect_task_db,
)
from app.task_engine.stages import StageResult

T0 = datetime(2026, 7, 17, tzinfo=timezone.utc)


def _stage_status(conn, stage_id):
    row = conn.execute(
        "SELECT status FROM task_stages WHERE stage_id=?", (stage_id,)
    ).fetchone()
    return row[0] if row else ""


def _last_error(conn, stage_id):
    row = conn.execute(
        "SELECT last_error_json FROM task_stages WHERE stage_id=?", (stage_id,)
    ).fetchone()
    return row[0] if row else None


def _make_artifact_on_disk(work_dir: Path, stage_id: str):
    from app.task_engine.artifacts import make_artifact_id
    from app.task_engine.fingerprints import sha256_file
    from app.task_engine.models import ArtifactRef

    work_dir.mkdir(parents=True, exist_ok=True)
    p = work_dir / "discover_manifest.json"
    p.write_text(json.dumps({
        "schema_version": 1, "stage": "discover", "duration_s": 2.0,
    }))
    sha = sha256_file(p)
    aid = make_artifact_id(
        stage_id=stage_id, artifact_kind="discover_manifest",
        clip_id=None, normalized_path=str(p),
    )
    ref = ArtifactRef(
        artifact_id=aid, job_id="", video_id="",
        stage_name="discover", clip_id=None,
        path=str(p), sha256=sha, size_bytes=p.stat().st_size,
        provenance_json="{}", stage_id=stage_id,
        artifact_kind="discover_manifest",
    )
    return p, ref


class TestOutcomeRecovery:
    """§3.2: crash recovery must preserve a needs_attention outcome."""

    def test_recovery_preserves_needs_attention_outcome(self, tmp_path):
        """materialize returned needs_attention + valid artifacts, worker
        crashed before the DB commit; a new worker reclaims and the stage,
        video, and job all end up needs_attention with artifacts written once.
        """
        repo, conn = make_repo(tmp_path)
        base_dir = tmp_path / "task_work"
        job = repo.create_job(CreateJob(
            directory="C:/video",
            config_json=json.dumps({"task_work_dir": str(base_dir)}),
        ))
        video = repo.add_video(job.job_id, "C:/video/a.mp4", "fp-a")
        stage = repo.ensure_stage(video.video_id, "discover", "input-a")

        first_claim = repo.claim_stage("worker-a", T0)
        ctx_work_dir = base_dir / first_claim.stage_name / first_claim.stage_id
        _, ref = _make_artifact_on_disk(ctx_work_dir, first_claim.stage_id)
        # Patch job/video ids onto the ref (helper left them blank).
        ref = ref.__class__(
            artifact_id=ref.artifact_id, job_id=job.job_id,
            video_id=video.video_id, stage_name=ref.stage_name,
            clip_id=ref.clip_id, path=ref.path, sha256=ref.sha256,
            size_bytes=ref.size_bytes, provenance_json=ref.provenance_json,
            stage_id=ref.stage_id, artifact_kind=ref.artifact_kind,
        )

        # Simulate the worker having saved a needs_attention result file but
        # crashing before the atomic DB commit.
        from app.task_engine.worker import TaskWorker as _W
        worker_a = _W(repo, "worker-a", {}, lease_seconds=90, db_path=str(tmp_path / "task.db"))
        fake_result = StageResult(
            output_key="discover", artifacts=(ref,), metrics={"failed_count": 1},
            outcome="needs_attention",
        )
        worker_a._save_result(ctx_work_dir, fake_result, first_claim)

        # The result file MUST carry the outcome (P0-1 root cause).
        saved = json.loads((ctx_work_dir / ".stage_result.json").read_text())
        assert saved.get("outcome") == "needs_attention", (
            f"_save_result must persist outcome, got {saved.get('outcome')!r}"
        )

        # Expire the lease so a second worker reclaims it.
        conn.execute(
            "UPDATE task_stages SET lease_expires_at=? WHERE stage_id=?",
            ("2020-01-01T00:00:00.000000+00:00", stage.stage_id),
        )
        conn.commit()

        # Second worker: a NO-OP adapter (recovery path must not re-run).
        class _NoopAdapter:
            name = "discover"
            version = "1"
            def run(self, ctx):
                raise AssertionError("recovery must not re-run the adapter")
        worker_b = TaskWorker(
            repo, "worker-b", {"discover": _NoopAdapter()},
            lease_seconds=90, db_path=str(tmp_path / "task.db"),
        )
        assert worker_b.run_once(now=T0 + timedelta(hours=1)) is True

        # Stage / video / job all needs_attention (no false success).
        assert _stage_status(conn, stage.stage_id) == "needs_attention"
        assert "publish failure" in (_last_error(conn, stage.stage_id) or "")
        vid_status = conn.execute(
            "SELECT status FROM task_videos WHERE video_id=?", (video.video_id,)
        ).fetchone()[0]
        assert vid_status == "needs_attention"
        job_status = conn.execute(
            "SELECT status FROM task_jobs WHERE job_id=?", (job.job_id,)
        ).fetchone()[0]
        assert job_status == "needs_attention"

        # Artifact written exactly once (recovery did not duplicate it).
        n = conn.execute(
            "SELECT COUNT(*) FROM task_artifacts WHERE stage_id=?",
            (stage.stage_id,),
        ).fetchone()[0]
        assert n == 1

    def test_recovery_rejects_unknown_outcome(self, tmp_path):
        """A result file carrying an unknown outcome (typo / future value)
        must NOT silently recover as succeeded - the worker must re-run the
        stage instead of trusting garbage."""
        repo, conn = make_repo(tmp_path)
        base_dir = tmp_path / "task_work"
        job = repo.create_job(CreateJob(
            directory="C:/video",
            config_json=json.dumps({"task_work_dir": str(base_dir)}),
        ))
        video = repo.add_video(job.job_id, "C:/video/a.mp4", "fp-a")
        stage = repo.ensure_stage(video.video_id, "discover", "input-a")
        first_claim = repo.claim_stage("worker-a", T0)
        ctx_work_dir = base_dir / first_claim.stage_name / first_claim.stage_id
        p, _ = _make_artifact_on_disk(ctx_work_dir, first_claim.stage_id)

        # Craft a result file with an UNKNOWN outcome.
        bad_result = {
            "schema_version": 1,
            "stage_id": first_claim.stage_id,
            "stage_name": "discover",
            "output_key": "discover",
            "outcome": "needs-atention-typo",
            "artifacts": [{
                "artifact_id": "a1", "job_id": job.job_id,
                "video_id": video.video_id, "stage_name": "discover",
                "clip_id": None, "path": str(p),
                "sha256": _sha(p), "size_bytes": p.stat().st_size,
                "provenance_json": "{}", "stage_id": first_claim.stage_id,
                "artifact_kind": "discover_manifest",
            }],
            "metrics": {},
        }
        (ctx_work_dir / ".stage_result.json").write_text(json.dumps(bad_result))

        conn.execute(
            "UPDATE task_stages SET lease_expires_at=? WHERE stage_id=?",
            ("2020-01-01T00:00:00.000000+00:00", stage.stage_id),
        )
        conn.commit()

        ran = {"yes": False}

        class _RunAdapter:
            name = "discover"
            version = "1"
            def run(self, ctx):
                ran["yes"] = True
                from app.task_engine.stages import StageResult as _SR
                return _SR(output_key="discover", artifacts=(), metrics={})

        worker_b = TaskWorker(
            repo, "worker-b", {"discover": _RunAdapter()},
            lease_seconds=90, db_path=str(tmp_path / "task.db"),
        )
        worker_b.run_once(now=T0 + timedelta(hours=1))

        # The unknown outcome must force a re-run, not silently succeed.
        assert ran["yes"] is True, (
            "unknown outcome must trigger re-run, not silent recovery"
        )

    def test_old_result_file_without_outcome_recovers_as_succeeded(self, tmp_path):
        """Backward-compat: a pre-outcome result file (no ``outcome`` key)
        is treated as ``succeeded`` and recovers normally."""
        repo, conn = make_repo(tmp_path)
        base_dir = tmp_path / "task_work"
        job = repo.create_job(CreateJob(
            directory="C:/video",
            config_json=json.dumps({"task_work_dir": str(base_dir)}),
        ))
        video = repo.add_video(job.job_id, "C:/video/a.mp4", "fp-a")
        stage = repo.ensure_stage(video.video_id, "discover", "input-a")
        first_claim = repo.claim_stage("worker-a", T0)
        ctx_work_dir = base_dir / first_claim.stage_name / first_claim.stage_id
        p, _ = _make_artifact_on_disk(ctx_work_dir, first_claim.stage_id)

        old_result = {
            "schema_version": 1,
            "stage_id": first_claim.stage_id,
            "stage_name": "discover",
            "output_key": "discover",
            # NO outcome key (legacy file).
            "artifacts": [{
                "artifact_id": "a1", "job_id": job.job_id,
                "video_id": video.video_id, "stage_name": "discover",
                "clip_id": None, "path": str(p),
                "sha256": _sha(p), "size_bytes": p.stat().st_size,
                "provenance_json": "{}", "stage_id": first_claim.stage_id,
                "artifact_kind": "discover_manifest",
            }],
            "metrics": {},
        }
        (ctx_work_dir / ".stage_result.json").write_text(json.dumps(old_result))

        conn.execute(
            "UPDATE task_stages SET lease_expires_at=? WHERE stage_id=?",
            ("2020-01-01T00:00:00.000000+00:00", stage.stage_id),
        )
        conn.commit()

        class _NoopAdapter:
            name = "discover"
            version = "1"
            def run(self, ctx):
                raise AssertionError("legacy file should recover, not re-run")

        worker_b = TaskWorker(
            repo, "worker-b", {"discover": _NoopAdapter()},
            lease_seconds=90, db_path=str(tmp_path / "task.db"),
        )
        worker_b.run_once(now=T0 + timedelta(hours=1))
        assert _stage_status(conn, stage.stage_id) == "succeeded"


def _sha(p: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    h.update(p.read_bytes())
    return h.hexdigest()


def make_repo(tmp_path: Path):
    conn = connect_task_db(tmp_path / "task.db")
    return TaskRepository(conn), conn
