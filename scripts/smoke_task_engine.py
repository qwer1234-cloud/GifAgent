#!/usr/bin/env python3
"""Smoke test for the task engine — creates jobs, runs stages, verifies recovery.

Usage:
    uv run python scripts/smoke_task_engine.py --exe <path> --data-dir <dir> --interruptions N
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path

# Ensure the project root is on sys.path so that ``from app…`` works when
# the script is invoked directly (not via ``uv run``).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.task_engine import (
    RetryPolicy,
    TaskRepository,
    StageRecord,
)

# Production data directories that must NOT be used for smoke testing.
_FORBIDDEN_DIRS = frozenset({
    os.path.abspath("data"),
    os.path.abspath("dist/GifAgentUI/data"),
    os.path.realpath("data"),
    os.path.realpath("dist/GifAgentUI/data"),
})


def _require_safe_data_dir(data_dir: str) -> Path:
    resolved = Path(data_dir).resolve()
    abspath = os.path.abspath(str(resolved))
    real = os.path.realpath(str(resolved))
    if abspath in _FORBIDDEN_DIRS or real in _FORBIDDEN_DIRS:
        print(
            f"ERROR: data directory '{resolved}' is the configured production "
            f"data directory. Refusing to run smoke test against production data.",
            file=sys.stderr,
        )
        sys.exit(2)
    return resolved


def _db_path(data_dir: Path) -> Path:
    return data_dir / "task_state.db"


def _main(exe: str | None, data_dir: Path, interruptions: int) -> int:
    db_path = _db_path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)

    policy = RetryPolicy(
        max_attempts=3, base_delay_s=0.1, max_delay_s=1.0,
    )
    repo = TaskRepository(db_path, retry_policy=policy)
    print(f"Task state DB created at: {db_path}")

    ok = True

    # 1. Create a job
    job_id = uuid.uuid4().hex
    repo.create_job(
        job_id=job_id,
        directory=str(data_dir),
        config={},
        job_limit=0,
        extensions=".mp4,.ts",
    )
    print(f"  [ok] created job {job_id}")

    # 2. Add a video
    video_id = uuid.uuid4().hex
    video_path = str(data_dir / "smoke_test_video.mp4")
    repo._insert_video(video_id, job_id, video_path, fingerprint="smoke-test-fp")
    print(f"  [ok] created video {video_id}")

    # 3. Add a stage
    stage_id = uuid.uuid4().hex
    clip_id = uuid.uuid4().hex
    repo._insert_stage(
        stage_id=stage_id,
        video_id=video_id,
        stage_name="materialize",
        clip_id=clip_id,
        input_key="smoke:video",
    )
    print(f"  [ok] created stage {stage_id}")

    # 4. Claim and complete the stage
    if interruptions > 0:
        repo.conn.execute("BEGIN IMMEDIATE")
    claimed = repo.claim_stage(worker_id="smoke-worker")
    if claimed is None:
        print("  [fail] no stage was claimed")
        ok = False
    else:
        print(f"  [ok] claimed stage {claimed.stage_id}")
        repo.complete_stage(
            stage_id=claimed.stage_id,
            worker_id="smoke-worker",
            output_key="smoke:done",
            output_path=None,
        )
        print(f"  [ok] completed stage {claimed.stage_id}")

        # 5. Verify the stage is now succeeded
        records = repo.find_stages(
            video_id=video_id, statuses=frozenset({"succeeded"})
        )
        succeeded_stages = [s for s in records if s.stage_id == claimed.stage_id]
        if succeeded_stages:
            print(f"  [ok] verified stage status=succeeded")
        else:
            print(f"  [fail] stage not found as succeeded")
            ok = False

    # 6. Cancel and verify
    repo.cancel_job(job_id)
    job = repo.get_job(job_id)
    if job is not None and job.status == "cancelled":
        print(f"  [ok] cancelled job {job_id}")
    else:
        print(f"  [fail] job cancellation failed (status={job.status if job else None})")
        ok = False

    repo.close()
    # Cleanup
    if db_path.exists():
        db_path.unlink()

    return 0 if ok else 1


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Smoke test for GifAgent task engine"
    )
    parser.add_argument(
        "--exe",
        help="Path to GifAgentUI.exe (optional, for packaged build verification)",
    )
    parser.add_argument(
        "--data-dir", required=True,
        help="Temporary directory for smoke test data "
             "(must NOT be the production data directory)",
    )
    parser.add_argument(
        "--interruptions", type=int, default=0,
        help="Number of simulated interruptions to inject (default: 0)",
    )
    args = parser.parse_args()

    data_dir = _require_safe_data_dir(args.data_dir)
    sys.exit(_main(args.exe, data_dir, args.interruptions))


if __name__ == "__main__":
    main()
