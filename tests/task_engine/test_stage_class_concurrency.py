"""Task 12: stage-class worker concurrency safety."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.task_engine import (
    CPU_STAGES,
    CreateJob,
    GPU_STAGES,
    TaskRepository,
    TaskWorker,
    connect_task_db,
)
from app.task_engine.orchestrator import advance_job


T0 = datetime(2026, 8, 23, tzinfo=timezone.utc)


def _repo(tmp_path: Path, name: str = "task.db") -> tuple[TaskRepository, Path]:
    db_path = tmp_path / name
    conn = connect_task_db(db_path)
    return TaskRepository(conn), db_path


def test_two_workers_never_claim_the_same_stage(tmp_path):
    repo, db_path = _repo(tmp_path)
    job = repo.create_job(CreateJob(directory="C:/video", config_json="{}"))
    video = repo.add_video(job.job_id, "C:/video/a.mp4", "fp-a")
    stage = repo.ensure_stage(video.video_id, "sample", "input-a")
    repo.conn.close()

    barrier = threading.Barrier(8)
    claimed: list[str | None] = [None] * 8
    errors: list[BaseException] = []

    def worker(index: int) -> None:
        conn = connect_task_db(db_path)
        try:
            local = TaskRepository(conn)
            barrier.wait(timeout=15)
            record = local.claim_stage(f"w-{index}", T0)
            claimed[index] = record.stage_id if record else None
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
        assert not thread.is_alive()

    assert not errors
    winners = [item for item in claimed if item is not None]
    assert winners == [stage.stage_id]


def test_claim_stage_names_filter_skips_other_classes(tmp_path):
    repo, _ = _repo(tmp_path)
    job = repo.create_job(CreateJob(directory="C:/video", config_json="{}"))
    video = repo.add_video(job.job_id, "C:/video/a.mp4", "fp-a")
    cpu = repo.ensure_stage(video.video_id, "sample", "input-sample")
    gpu = repo.ensure_stage(video.video_id, "vlm", "input-vlm")

    claimed_gpu = repo.claim_stage("gpu-1", T0, stage_names=GPU_STAGES)
    claimed_cpu = repo.claim_stage("cpu-1", T0, stage_names=CPU_STAGES)
    assert claimed_gpu is not None and claimed_gpu.stage_id == gpu.stage_id
    assert claimed_cpu is not None and claimed_cpu.stage_id == cpu.stage_id
    repo.conn.close()


def test_concurrent_advance_job_does_not_duplicate_gif_clip_stages(tmp_path):
    repo, db_path = _repo(tmp_path)
    job = repo.create_job(CreateJob(directory=str(tmp_path), config_json="{}"))
    video = repo.add_video(job.job_id, str(tmp_path / "a.mp4"), "fp-a")
    repo.ensure_stage(video.video_id, "rank_dedup", "from:synthesize")
    repo.conn.close()

    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def worker() -> None:
        conn = connect_task_db(db_path)
        try:
            local = TaskRepository(conn)
            barrier.wait(timeout=15)
            # Two threads race the same idempotency key family.
            for clip_id in ("clip-a", "clip-b"):
                local.ensure_stage(
                    video.video_id,
                    "gif_clip",
                    f"from:rank_dedup:clip:{clip_id}",
                    clip_id=clip_id,
                )
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
        assert not thread.is_alive()
    assert not errors

    check, _ = _repo(tmp_path, db_path.name)
    rows = check.conn.execute(
        "SELECT clip_id, COUNT(*) AS n FROM task_stages "
        "WHERE video_id=? AND stage_name='gif_clip' GROUP BY clip_id",
        (video.video_id,),
    ).fetchall()
    counts = {row["clip_id"]: row["n"] for row in rows}
    assert counts.get("clip-a") == 1
    assert counts.get("clip-b") == 1
    check.conn.close()


def test_materialize_is_created_exactly_once_under_concurrency(tmp_path):
    repo, db_path = _repo(tmp_path)
    job = repo.create_job(CreateJob(directory=str(tmp_path), config_json="{}"))
    video = repo.add_video(job.job_id, str(tmp_path / "a.mp4"), "fp-a")
    gif = repo.ensure_stage(
        video.video_id, "gif_clip", "from:rank_dedup:clip:c1", clip_id="c1",
    )
    claimed = repo.claim_stage("setup", T0)
    assert claimed is not None and claimed.stage_id == gif.stage_id
    repo.complete_stage(claimed.stage_id, "setup", "gif-done")
    repo.conn.close()

    barrier = threading.Barrier(4)
    errors: list[BaseException] = []

    def worker() -> None:
        conn = connect_task_db(db_path)
        try:
            local = TaskRepository(conn)
            barrier.wait(timeout=15)
            advance_job(local, job.job_id)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
        assert not thread.is_alive()
    assert not errors

    check, _ = _repo(tmp_path, db_path.name)
    rows = check.conn.execute(
        "SELECT COUNT(*) AS n FROM task_stages "
        "WHERE video_id=? AND stage_name='materialize'",
        (video.video_id,),
    ).fetchone()
    assert rows["n"] == 1
    check.conn.close()


def test_heartbeat_connections_do_not_interfere(tmp_path):
    repo, db_path = _repo(tmp_path)
    job = repo.create_job(CreateJob(directory="C:/video", config_json="{}"))
    video = repo.add_video(job.job_id, "C:/video/a.mp4", "fp-a")
    stage = repo.ensure_stage(video.video_id, "sample", "input-a")
    claimed = repo.claim_stage("hb-owner", T0, lease_seconds=90)
    assert claimed is not None
    repo.conn.close()

    errors: list[BaseException] = []

    def heartbeat_loop(worker_id: str) -> None:
        conn = connect_task_db(db_path)
        try:
            local = TaskRepository(conn)
            worker = TaskWorker(local, worker_id, {}, db_path=str(db_path))
            for offset in range(5):
                worker.heartbeat(stage.stage_id, T0 + timedelta(seconds=offset))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            conn.close()

    threads = [
        threading.Thread(target=heartbeat_loop, args=(f"hb-{i}",))
        for i in range(3)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive()
    assert not errors


def test_busy_timeout_absorbs_multi_worker_contention(tmp_path):
    repo, db_path = _repo(tmp_path)
    job = repo.create_job(CreateJob(directory="C:/video", config_json="{}"))
    video = repo.add_video(job.job_id, "C:/video/a.mp4", "fp-a")
    for index in range(6):
        repo.ensure_stage(video.video_id, "sample", f"input-{index}")
    repo.conn.close()

    errors: list[BaseException] = []
    claimed = 0
    lock = threading.Lock()

    def worker(index: int) -> None:
        nonlocal claimed
        conn = connect_task_db(db_path)
        try:
            local = TaskRepository(conn)
            record = local.claim_stage(f"busy-{index}", T0)
            if record is not None:
                with lock:
                    claimed += 1
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
        assert not thread.is_alive()
    assert not errors
    assert claimed == 6
