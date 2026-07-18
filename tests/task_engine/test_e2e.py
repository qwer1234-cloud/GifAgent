"""Phase H: Real end-to-end tests for the task engine.

Tests exercise the full worker chain: discover → materialize,
with real file artifacts in temp directories.  All external
dependencies (ffprobe, ffmpeg, VLM, LLM) are replaced with fake
adapters that produce real artifact files.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.task_engine.artifacts import (
    ArtifactCollisionError,
    insert_artifact_dedup,
    make_artifact_id,
    resolve_stage_inputs,
    STAGE_INPUT_KINDS,
)
from app.task_engine.fingerprints import sha256_file
from app.task_engine.models import ArtifactRef, CreateJob, StageName
from app.task_engine.orchestrator import (
    advance_job,
    initialize_job,
    _STAGE_ORDER,
)
from app.task_engine.repository import TaskRepository
from app.task_engine.schema import connect_task_db
from app.task_engine.stages import StageContext, StageResult
from app.task_engine.worker import TaskWorker

T0 = datetime(2026, 7, 17, tzinfo=timezone.utc)
STAGE_NAMES: tuple[StageName, ...] = _STAGE_ORDER


# ==========================================================================
# Helper: create a fake adapter that writes a manifest + returns ArtifactRefs
# ==========================================================================


def _fake_stage_adapter(
    stage_name_str: StageName,
    manifest_content: dict,
    file_name: str,
    kind: str,
    clip_id_val: str | None = None,
    extra_files: list[tuple[str, str, str]] | None = None,
):
    """Create a StageAdapter class that writes a manifest and returns a result.

    Parameters
    ----------
    stage_name_str: stage name for the adapter
    manifest_content: dict to write as JSON
    file_name: name of the manifest file in work_dir
    kind: artifact_kind for the main artifact
    clip_id_val: if provided, used for artifact metadata
    extra_files: additional (filename, content, artifact_kind) triplets
    """

    class _Adapter:
        _name_val = stage_name_str
        _clip_val = clip_id_val
        _manifest_content = manifest_content
        _file_name = file_name
        _kind = kind
        _extra_files = extra_files
        version = "1"

        @property
        def name(self): return self._name_val

        def run(self, ctx: StageContext) -> StageResult:
            ctx.work_dir.mkdir(parents=True, exist_ok=True)
            manifest_path = ctx.work_dir / self._file_name
            self._manifest_content["video_id"] = ctx.video_id
            self._manifest_content.setdefault("stage", self._name_val)
            self._manifest_content.setdefault("schema_version", 1)
            manifest_path.write_text(json.dumps(self._manifest_content))
            sha = sha256_file(manifest_path)

            cid_val = self._clip_val or ctx.clip_id
            art_id = make_artifact_id(
                stage_id=ctx.stage_id,
                artifact_kind=self._kind,
                clip_id=cid_val,
                normalized_path=str(manifest_path),
            )

            artifacts = [
                ArtifactRef(
                    artifact_id=art_id,
                    job_id=ctx.job_id,
                    video_id=ctx.video_id,
                    stage_name=self._name_val,
                    clip_id=cid_val,
                    path=str(manifest_path),
                    sha256=sha,
                    size_bytes=manifest_path.stat().st_size,
                    provenance_json="{}",
                    stage_id=ctx.stage_id,
                    artifact_kind=self._kind,
                )
            ]

            if self._extra_files:
                for extra_name, content, extra_kind in self._extra_files:
                    extra_path = ctx.work_dir / extra_name
                    extra_path.write_text(content)
                    extra_sha = sha256_file(extra_path)
                    extra_art_id = make_artifact_id(
                        stage_id=ctx.stage_id,
                        artifact_kind=extra_kind,
                        clip_id=cid_val,
                        normalized_path=str(extra_path),
                    )
                    artifacts.append(ArtifactRef(
                        artifact_id=extra_art_id,
                        job_id=ctx.job_id,
                        video_id=ctx.video_id,
                        stage_name=self._name_val,
                        clip_id=cid_val,
                        path=str(extra_path),
                        sha256=extra_sha,
                        size_bytes=extra_path.stat().st_size,
                        provenance_json="{}",
                        stage_id=ctx.stage_id,
                        artifact_kind=extra_kind,
                    ))

            return StageResult(
                output_key=f"{self._name_val}-done",
                artifacts=tuple(artifacts),
                metrics={},
            )

    return _Adapter()


def _make_all_adapters(clip_count: int = 2) -> dict:
    """Build adapters for the full pipeline with a specified clip count."""
    return {
        "discover": _fake_stage_adapter(
            "discover", {"duration_s": 120.0, "width": 1920},
            "discover_manifest.json", "discover_manifest",
        ),
        "sample": _fake_stage_adapter(
            "sample", {"sample_points": [10, 20, 30, 40, 50],
                       "frame_count": 2,
                       "timestamps": [10, 20],
                       "frame_paths": ["frame_10.jpg", "frame_20.jpg"]},
            "sample_manifest.json", "sample_manifest",
            extra_files=[("frame_10.jpg", "frame-data-10", "sample_frames"),
                         ("frame_20.jpg", "frame-data-20", "sample_frames")],
        ),
        "vlm": _fake_stage_adapter(
            "vlm", {"scores": [0.3, 0.5, 0.7, 0.6, 0.4]},
            "vlm_manifest.json", "vlm_manifest",
        ),
        "refine": _fake_stage_adapter(
            "refine", {"refined_regions": [{"start": 20, "end": 40}]},
            "refine_manifest.json", "refine_manifest",
        ),
        "synthesize": _fake_stage_adapter(
            "synthesize", {
                "clips": [
                    {"clip_id": f"clip-{chr(65 + i)}", "start_ts": i * 20 + 10,
                     "end_ts": i * 20 + 15, "gif_worthiness": 0.8 - i * 0.1}
                    for i in range(clip_count)
                ],
            },
            "synthesize_manifest.json", "synthesize_manifest",
        ),
        "rank_dedup": _fake_stage_adapter(
            "rank_dedup", {
                "clip_count": clip_count,
                "clips": [
                    {"clip_id": f"clip-{chr(65 + i)}", "start_ts": i * 20 + 10,
                     "end_ts": i * 20 + 15, "gif_worthiness": 0.8 - i * 0.1}
                    for i in range(clip_count)
                ],
            },
            "rank_dedup_manifest.json", "rank_dedup_manifest",
        ),
        "gif_clip": _fake_stage_adapter(
            "gif_clip", {"clip_id": "$CLIP_ID", "gif_path": "$GIF_PATH"},
            "gif_clip_manifest.json", "gif_clip_manifest",
            clip_id_val="$CLIP_ID",  # placeholder, overridden in run()
            extra_files=[("output.gif", "GIF-BYTES-PLACEHOLDER", "gif_file")],
        ),
        "materialize": _fake_stage_adapter(
            "materialize", {"succeeded_clips": [], "failed_clips": []},
            "result.json", "result",
        ),
    }


# But gif_clip needs to know its actual clip_id dynamically.  We need a
# custom adapter that reads ctx.clip_id.  Let's replace it.
def _make_gif_clip_adapter():
    class _GifClipAdapter:
        name = "gif_clip"
        version = "1"

        def run(self, ctx: StageContext) -> StageResult:
            ctx.work_dir.mkdir(parents=True, exist_ok=True)
            cid = ctx.clip_id
            if cid is None:
                raise ValueError("gif_clip stage requires clip_id")

            # Write GIF file
            gif_path = ctx.work_dir / f"output_{ctx.clip_id}.gif"
            gif_path.write_text(f"GIF-{cid}")
            gif_sha = sha256_file(gif_path)
            gif_art_id = make_artifact_id(
                stage_id=ctx.stage_id, artifact_kind="gif_file",
                clip_id=cid, normalized_path=str(gif_path),
            )

            # Write manifest
            manifest_path = ctx.work_dir / f"gif_manifest_{cid}.json"
            manifest = {
                "schema_version": 1, "stage": "gif_clip",
                "clip_id": cid, "gif_path": str(gif_path),
                "width": 720, "height": 480, "fps": 24, "duration_s": 5.0,
            }
            manifest_path.write_text(json.dumps(manifest))
            manifest_sha = sha256_file(manifest_path)
            manifest_art_id = make_artifact_id(
                stage_id=ctx.stage_id, artifact_kind="gif_clip_manifest",
                clip_id=cid, normalized_path=str(manifest_path),
            )

            return StageResult(
                output_key=f"gif-{cid}-done",
                artifacts=(
                    ArtifactRef(
                        artifact_id=gif_art_id,
                        job_id=ctx.job_id, video_id=ctx.video_id,
                        stage_name="gif_clip", clip_id=cid,
                        path=str(gif_path), sha256=gif_sha,
                        size_bytes=gif_path.stat().st_size,
                        provenance_json="{}",
                        stage_id=ctx.stage_id, artifact_kind="gif_file",
                    ),
                    ArtifactRef(
                        artifact_id=manifest_art_id,
                        job_id=ctx.job_id, video_id=ctx.video_id,
                        stage_name="gif_clip", clip_id=cid,
                        path=str(manifest_path), sha256=manifest_sha,
                        size_bytes=manifest_path.stat().st_size,
                        provenance_json="{}",
                        stage_id=ctx.stage_id, artifact_kind="gif_clip_manifest",
                    ),
                ),
                metrics={"gif_size_bytes": gif_path.stat().st_size},
            )

    return _GifClipAdapter()


def _make_gif_clip_failable_adapter(fail_on_clip: str):
    """GifClip adapter that fails on the first attempt for a specific clip_id."""
    fail_count = [0]

    class _Failable:
        name = "gif_clip"; version = "1"
        def run(self, ctx):
            if ctx.clip_id == fail_on_clip:
                fail_count[0] += 1
                if fail_count[0] == 1:
                    # Use a non-transient error so the stage goes to
                    # needs_attention, not retry_wait.
                    raise OSError("No such file: missing_clip_output")
            return _make_gif_clip_adapter().run(ctx)

    return _Failable()


# ==========================================================================
# Fixtures
# ==========================================================================


@pytest.fixture
def task_db(tmp_path: Path) -> sqlite3.Connection:
    conn = connect_task_db(tmp_path / "task.db")
    yield conn
    conn.close()


@pytest.fixture
def repo(task_db: sqlite3.Connection) -> TaskRepository:
    return TaskRepository(task_db)


@pytest.fixture
def video_dir(tmp_path: Path) -> Path:
    d = tmp_path / "videos"
    d.mkdir()
    (d / "test.mp4").write_text("fake-video-data")
    return d


# ==========================================================================
# E2E: Full chain driver helper
# ==========================================================================


def _drive_full_chain(repo: TaskRepository, video_dir: Path, tmp_path: Path,
                      clip_count: int = 2, worker_id: str = "worker-1",
                      fail_on_clip: str | None = None):
    """Drive a full pipeline end-to-end and return job_id, video_id."""
    base = tmp_path / "task_work"
    job = repo.create_job(CreateJob(
        directory=str(video_dir),
        config_json=json.dumps({"task_work_dir": str(base)}),
    ))
    initialize_job(repo, job.job_id)

    adapters = _make_all_adapters(clip_count=clip_count)
    adapters["gif_clip"] = (
        _make_gif_clip_failable_adapter(fail_on_clip)
        if fail_on_clip
        else _make_gif_clip_adapter()
    )

    worker = TaskWorker(repo, worker_id, adapters)
    worker.drain()

    # Advance one more time to ensure job aggregation runs
    advance_job(repo, job.job_id)

    return job, adapters


# ==========================================================================
# Test: Full chain with 2 clips succeeds
# ==========================================================================


class TestFullChainE2E:
    def test_stages_created_in_order(
        self, repo: TaskRepository, video_dir: Path, tmp_path: Path
    ):
        """All 8 stages appear for a 2-clip pipeline."""
        job, _ = _drive_full_chain(repo, video_dir, tmp_path, clip_count=2)

        stages = repo.conn.execute(
            """SELECT s.stage_name, s.status FROM task_stages s
               JOIN task_videos v ON s.video_id = v.video_id
               WHERE v.job_id = ? ORDER BY s.created_at""",
            (job.job_id,),
        ).fetchall()

        names_seen = {r["stage_name"] for r in stages}
        for expected in STAGE_NAMES:
            assert expected in names_seen, f"Stage {expected} missing"

    def test_artifact_counts(
        self, repo: TaskRepository, video_dir: Path, tmp_path: Path
    ):
        """Each stage produces the correct number of artifacts."""
        job, _ = _drive_full_chain(repo, video_dir, tmp_path, clip_count=2)

        arts_by_kind = repo.conn.execute(
            """SELECT a.artifact_kind, COUNT(*) as cnt
               FROM task_artifacts a
               JOIN task_videos v ON a.video_id = v.video_id
               WHERE v.job_id = ?
               GROUP BY a.artifact_kind""",
            (job.job_id,),
        ).fetchall()

        counts = {r["artifact_kind"]: r["cnt"] for r in arts_by_kind}
        assert counts.get("rank_dedup_manifest", 0) == 1
        assert counts.get("gif_file", 0) == 2
        assert counts.get("gif_clip_manifest", 0) == 2

    def test_artifacts_sha256_verifiable(
        self, repo: TaskRepository, video_dir: Path, tmp_path: Path
    ):
        """Every artifact's SHA-256 matches the actual file on disk."""
        job, _ = _drive_full_chain(repo, video_dir, tmp_path, clip_count=2)

        arts = repo.conn.execute(
            """SELECT a.* FROM task_artifacts a
               JOIN task_videos v ON a.video_id = v.video_id
               WHERE v.job_id = ?""",
            (job.job_id,),
        ).fetchall()

        for art in arts:
            p = Path(art["path"])
            assert p.exists(), f"Artifact file missing: {art['path']}"
            assert sha256_file(p) == art["sha256"], (
                f"SHA-256 mismatch for {art['artifact_id']}"
            )
            assert p.stat().st_size == art["size_bytes"], (
                f"Size mismatch for {art['artifact_id']}"
            )

    def test_gif_clip_fan_out_count(
        self, repo: TaskRepository, video_dir: Path, tmp_path: Path
    ):
        """3 clips → exactly 3 gif_clip stages with correct clip_ids."""
        job, _ = _drive_full_chain(repo, video_dir, tmp_path, clip_count=3)

        gif_clips = repo.conn.execute(
            """SELECT s.clip_id FROM task_stages s
               JOIN task_videos v ON s.video_id = v.video_id
               WHERE v.job_id = ? AND s.stage_name = 'gif_clip'
               ORDER BY s.clip_id""",
            (job.job_id,),
        ).fetchall()

        assert len(gif_clips) == 3
        clip_ids = {r["clip_id"] for r in gif_clips}
        assert clip_ids == {"clip-A", "clip-B", "clip-C"}


# ==========================================================================
# Test: Retry preserves successful clips
# ==========================================================================


class TestRetryPreservesClips:
    def test_failed_clip_retry_leaves_successful_unchanged(
        self, repo: TaskRepository, video_dir: Path, tmp_path: Path
    ):
        """Clip-A fails (needs_attention); retry succeeds; clip-B untouched."""
        job, adapters = _drive_full_chain(
            repo, video_dir, tmp_path, clip_count=2, fail_on_clip="clip-A"
        )

        # clip-A should be needs_attention (non-transient failure)
        clip_a_status = repo.conn.execute(
            """SELECT s.status FROM task_stages s
               JOIN task_videos v ON s.video_id = v.video_id
               WHERE v.job_id = ? AND s.stage_name = 'gif_clip'
                 AND s.clip_id = 'clip-A'""",
            (job.job_id,),
        ).fetchone()
        assert clip_a_status is not None
        assert clip_a_status["status"] == "needs_attention", (
            f"Expected needs_attention, got {clip_a_status['status']}"
        )

        # clip-B should be succeeded with artifacts
        clip_b_row = repo.conn.execute(
            """SELECT s.status, a.sha256, a.path FROM task_stages s
               JOIN task_videos v ON s.video_id = v.video_id
               JOIN task_artifacts a ON a.video_id = s.video_id
                 AND a.stage_name = 'gif_clip' AND a.clip_id = s.clip_id
                 AND a.artifact_kind = 'gif_file'
               WHERE v.job_id = ? AND s.stage_name = 'gif_clip'
                 AND s.clip_id = 'clip-B'""",
            (job.job_id,),
        ).fetchone()
        assert clip_b_row is not None, "clip-B should have gif_file artifact"
        assert clip_b_row["status"] == "succeeded"
        orig_sha = clip_b_row["sha256"]
        orig_path = Path(clip_b_row["path"])
        orig_mtime = orig_path.stat().st_mtime

        # §3.6 point 4: capture the successful clip's stage_id + attempt_count
        # so we can prove retry did NOT re-run it.
        clip_b_stage = repo.conn.execute(
            """SELECT s.stage_id, s.attempt_count FROM task_stages s
               JOIN task_videos v ON s.video_id = v.video_id
               WHERE v.job_id = ? AND s.stage_name = 'gif_clip'
                 AND s.clip_id = 'clip-B'""",
            (job.job_id,),
        ).fetchone()
        assert clip_b_stage is not None
        orig_b_stage_id = clip_b_stage["stage_id"]
        orig_b_attempt = clip_b_stage["attempt_count"]
        # Capture clip-A's stage_id for the re-attempt event count check.
        clip_a_stage = repo.conn.execute(
            """SELECT s.stage_id, s.attempt_count FROM task_stages s
               JOIN task_videos v ON s.video_id = v.video_id
               WHERE v.job_id = ? AND s.stage_name = 'gif_clip'
                 AND s.clip_id = 'clip-A'""",
            (job.job_id,),
        ).fetchone()
        assert clip_a_stage is not None
        clip_a_stage_id = clip_a_stage["stage_id"]
        clip_a_attempt_before = clip_a_stage["attempt_count"]

        # Issue retry command
        repo.append_command(job.job_id, "retry", {})
        advance_job(repo, job.job_id)

        # Verify clip-A was reset to pending
        after_retry = repo.conn.execute(
            """SELECT s.status, s.attempt_count FROM task_stages s
               JOIN task_videos v ON s.video_id = v.video_id
               WHERE v.job_id = ? AND s.stage_name = 'gif_clip'
                 AND s.clip_id = 'clip-A'""",
            (job.job_id,),
        ).fetchone()
        assert after_retry is not None
        assert after_retry["status"] == "pending", (
            f"Expected pending after retry, got {after_retry['status']}"
        )

        # Run worker again with non-failing adapter
        adapters["gif_clip"] = _make_gif_clip_adapter()
        worker2 = TaskWorker(repo, "worker-2", adapters)
        worker2.drain()
        advance_job(repo, job.job_id)

        # clip-A should now be succeeded
        clip_a_done = repo.conn.execute(
            """SELECT s.status FROM task_stages s
               JOIN task_videos v ON s.video_id = v.video_id
               WHERE v.job_id = ? AND s.stage_name = 'gif_clip'
                 AND s.clip_id = 'clip-A'""",
            (job.job_id,),
        ).fetchone()
        assert clip_a_done is not None
        assert clip_a_done["status"] == "succeeded", (
            f"Expected succeeded after retry, got {clip_a_done['status']}"
        )

        # clip-B should be untouched
        assert orig_path.exists()
        assert orig_path.stat().st_mtime == orig_mtime
        assert sha256_file(orig_path) == orig_sha

        # §3.6 point 4: the successful clip is NOT re-run - stage_id and
        # attempt_count are unchanged (retry only resets failed/needs_attention).
        clip_b_after = repo.conn.execute(
            """SELECT s.stage_id, s.attempt_count FROM task_stages s
               JOIN task_videos v ON s.video_id = v.video_id
               WHERE v.job_id = ? AND s.stage_name = 'gif_clip'
                 AND s.clip_id = 'clip-B'""",
            (job.job_id,),
        ).fetchone()
        assert clip_b_after["stage_id"] == orig_b_stage_id, (
            "clip-B stage_id must be unchanged by retry"
        )
        assert clip_b_after["attempt_count"] == orig_b_attempt, (
            "clip-B attempt_count must be unchanged (not re-run)"
        )

        # §3.6 point 5: the failed clip was re-attempted and eventually
        # succeeded.  It has more stage.claimed events than clip-B.
        a_claims = repo.conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE kind='stage.claimed' "
            "AND payload_json LIKE ?",
            (f'%{clip_a_stage_id}%',),
        ).fetchone()[0]
        b_claims = repo.conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE kind='stage.claimed' "
            "AND payload_json LIKE ?",
            (f'%{orig_b_stage_id}%',),
        ).fetchone()[0]
        assert a_claims >= 2, (
            f"clip-A should be claimed >=2 times (fail + retry), got {a_claims}"
        )
        assert b_claims == 1, (
            f"clip-B should be claimed exactly once (not re-run), got {b_claims}"
        )


# ==========================================================================
# Test: Zero-clip path
# ==========================================================================


class TestZeroClip:
    def test_zero_clip_no_gif_clip_stages(
        self, repo: TaskRepository, video_dir: Path, tmp_path: Path
    ):
        """0 clips → no gif_clip stages, materialize exists and succeeds."""
        job, _ = _drive_full_chain(repo, video_dir, tmp_path, clip_count=0)

        gif_count = repo.conn.execute(
            """SELECT COUNT(*) FROM task_stages s
               JOIN task_videos v ON s.video_id = v.video_id
               WHERE v.job_id = ? AND s.stage_name = 'gif_clip'""",
            (job.job_id,),
        ).fetchone()[0]
        assert gif_count == 0, "zero-clip should not create gif_clip stages"

        mat = repo.conn.execute(
            """SELECT s.status FROM task_stages s
               JOIN task_videos v ON s.video_id = v.video_id
               WHERE v.job_id = ? AND s.stage_name = 'materialize'""",
            (job.job_id,),
        ).fetchone()
        assert mat is not None
        assert mat["status"] == "succeeded"


# ==========================================================================
# Test: Concurrent workers dedup
# ==========================================================================


class TestConcurrentDedup:
    def test_two_workers_no_duplicate_stages(
        self, repo: TaskRepository, video_dir: Path, tmp_path: Path
    ):
        """Two workers with separate connections process the same job
        without creating duplicate stages."""
        base = tmp_path / "task_work"
        db_path = tmp_path / "task.db"
        job = repo.create_job(CreateJob(
            directory=str(video_dir),
            config_json=json.dumps({"task_work_dir": str(base)}),
        ))
        initialize_job(repo, job.job_id)

        adapters = _make_all_adapters(clip_count=1)
        adapters["gif_clip"] = _make_gif_clip_adapter()

        barrier = threading.Barrier(2)

        def worker_fn(wid: str):
            conn = connect_task_db(db_path)
            repo2 = TaskRepository(conn)
            w = TaskWorker(repo2, wid, adapters)
            barrier.wait(timeout=15)
            w.drain()
            conn.close()

        t1 = threading.Thread(target=worker_fn, args=("w-a",))
        t2 = threading.Thread(target=worker_fn, args=("w-b",))
        t1.start(); t2.start()
        t1.join(timeout=60); t2.join(timeout=60)

        # Each logical stage should appear exactly once (except gif_clip
        # which has exactly 1 instance for 1 clip).
        stage_counts = repo.conn.execute(
            """SELECT stage_name, COUNT(*) as cnt
               FROM task_stages s
               JOIN task_videos v ON s.video_id = v.video_id
               WHERE v.job_id = ?
               GROUP BY stage_name""",
            (job.job_id,),
        ).fetchall()

        for r in stage_counts:
            # Single-connection stages should have exactly 1 instance.
            # gif_clip count = clip_count = 1.
            expected = 1
            assert r["cnt"] == expected, (
                f"Stage {r['stage_name']}: expected {expected}, got {r['cnt']}"
            )


# ==========================================================================
# Phase A: Artifact identity and dedup
# ==========================================================================


class TestArtifactIdentityDedup:
    def test_idempotent_insert(
        self, repo: TaskRepository, video_dir: Path, tmp_path: Path
    ):
        """Inserting the same artifact twice is idempotent."""
        job, _ = _drive_full_chain(repo, video_dir, tmp_path, clip_count=1)

        existing = repo.conn.execute(
            """SELECT * FROM task_artifacts WHERE artifact_kind='gif_file' LIMIT 1"""
        ).fetchone()
        assert existing is not None

        ref = ArtifactRef(
            artifact_id=existing["artifact_id"],
            job_id=existing["job_id"],
            video_id=existing["video_id"],
            stage_name=existing["stage_name"],
            clip_id=existing["clip_id"],
            path=existing["path"],
            sha256=existing["sha256"],
            size_bytes=existing["size_bytes"],
            provenance_json=existing["provenance_json"],
            stage_id=existing["stage_id"] or "",
            artifact_kind=existing["artifact_kind"],
        )
        repo.conn.execute("BEGIN IMMEDIATE")
        assert insert_artifact_dedup(repo.conn, ref) is False  # idempotent
        repo.conn.commit()

    def test_collision_different_sha(
        self, repo: TaskRepository, video_dir: Path, tmp_path: Path
    ):
        """Same artifact_id, different SHA → collision error."""
        job, _ = _drive_full_chain(repo, video_dir, tmp_path, clip_count=1)

        existing = repo.conn.execute(
            """SELECT * FROM task_artifacts WHERE artifact_kind='gif_file' LIMIT 1"""
        ).fetchone()
        assert existing is not None

        ref = ArtifactRef(
            artifact_id=existing["artifact_id"],
            job_id=existing["job_id"],
            video_id=existing["video_id"],
            stage_name=existing["stage_name"],
            clip_id=existing["clip_id"],
            path=existing["path"],
            sha256="0" * 64,  # WRONG
            size_bytes=existing["size_bytes"],
            provenance_json=existing["provenance_json"],
            stage_id=existing["stage_id"] or "",
            artifact_kind=existing["artifact_kind"],
        )
        repo.conn.execute("BEGIN IMMEDIATE")
        with pytest.raises(ArtifactCollisionError, match="sha256"):
            insert_artifact_dedup(repo.conn, ref)
        repo.conn.rollback()

    def test_resolver_finds_upstream_artifacts(
        self, repo: TaskRepository, video_dir: Path, tmp_path: Path
    ):
        """resolve_stage_inputs finds artifacts from a previous stage."""
        job, _ = _drive_full_chain(repo, video_dir, tmp_path, clip_count=2)

        # Get the video_id from the job
        vid_row = repo.conn.execute(
            "SELECT video_id FROM task_videos WHERE job_id=? LIMIT 1",
            (job.job_id,),
        ).fetchone()
        assert vid_row is not None

        # Resolve sample's inputs (should find discover_manifest)
        inputs = resolve_stage_inputs(
            repo.conn, vid_row["video_id"], "sample",
        )
        assert "discover_manifest" in inputs
        assert len(inputs["discover_manifest"]) >= 1

        # Resolve gif_clip inputs for a specific clip
        gif_row = repo.conn.execute(
            """SELECT clip_id FROM task_stages s
               JOIN task_videos v ON s.video_id = v.video_id
               WHERE v.job_id = ? AND s.stage_name = 'gif_clip' LIMIT 1""",
            (job.job_id,),
        ).fetchone()

        if gif_row and gif_row["clip_id"]:
            gif_inputs = resolve_stage_inputs(
                repo.conn, vid_row["video_id"], "gif_clip",
                clip_id=gif_row["clip_id"],
            )
            assert "rank_dedup_manifest" in gif_inputs

    def test_video_status_aggregation(
        self, repo: TaskRepository, video_dir: Path, tmp_path: Path
    ):
        """After full chain, video status is succeeded."""
        job, _ = _drive_full_chain(repo, video_dir, tmp_path, clip_count=2)

        vid_status = repo.conn.execute(
            """SELECT v.status FROM task_videos v
               WHERE v.job_id = ? LIMIT 1""",
            (job.job_id,),
        ).fetchone()
        assert vid_status is not None
        assert vid_status["status"] == "succeeded"

        job_status = repo.conn.execute(
            "SELECT status FROM task_jobs WHERE job_id=?", (job.job_id,)
        ).fetchone()
        assert job_status["status"] == "succeeded"


# ==========================================================================
# Test: Partial GIF failure → video needs_attention
# ==========================================================================


class TestPartialFailureStatus:
    def test_one_clip_failed_video_needs_attention(
        self, repo: TaskRepository, video_dir: Path, tmp_path: Path
    ):
        """When one gif_clip fails permanently, video is needs_attention."""
        job, _ = _drive_full_chain(
            repo, video_dir, tmp_path, clip_count=2, fail_on_clip="clip-A"
        )

        # clip-A should have failed (needs_attention from non-transient error)
        # clip-B should be succeeded
        vid_status = repo.conn.execute(
            """SELECT v.status FROM task_videos v
               WHERE v.job_id = ? LIMIT 1""",
            (job.job_id,),
        ).fetchone()
        # After drive, job may not have advanced fully. Let's check.
        # The _drive_full_chain calls advance_job which aggregates.
        # With one gif_clip failed + one succeeded → video is needs_attention.
        assert vid_status is not None
        # Both clips exist; one failed → aggregate should be needs_attention
        assert vid_status["status"] == "needs_attention", (
            f"Expected needs_attention, got {vid_status['status']}"
        )
