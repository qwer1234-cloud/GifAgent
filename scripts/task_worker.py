#!/usr/bin/env python3
"""CLI entry point for a single-writer task worker process.

Usage
-----
    python scripts/task_worker.py [--worker-id ID] [--db PATH]
        [--once] [--poll SECONDS]
        [--max-attempts N] [--base-delay SECONDS] [--max-delay SECONDS]

When ``--once`` is given the worker processes a single stage and exits
with status 0 (work done) or 1 (idle).  Without ``--once`` the worker
drains all available stages and exits.
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from pathlib import Path

# Ensure the project root is on sys.path so that ``from app…`` works when
# the script is invoked directly (not via ``uv run``).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.task_engine import (
    AdaptivePipelineAdapter,
    RetryPolicy,
    TaskRepository,
    TaskWorker,
    connect_task_db,
)
from app.task_engine.models import StageName


def _build_adapters() -> dict:
    """Return an adapter for every known ``StageName``.

    Each adapter wraps a subprocess call to the adaptive pipeline script.
    """
    stage_names = (
        "discover",
        "sample",
        "vlm",
        "refine",
        "synthesize",
        "rank_dedup",
        "gif_clip",
        "materialize",
    )
    return {name: AdaptivePipelineAdapter(name) for name in stage_names}


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the GIF task worker")
    parser.add_argument(
        "--worker-id",
        default=None,
        help="Unique worker identifier (auto-generated if omitted)",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="Path to the task database (default: $GIFAGENT_TASK_DB or data/task_state.db)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Process a single stage and exit",
    )
    parser.add_argument(
        "--poll",
        type=float,
        default=1.0,
        help="Poll interval in seconds (unused in drain mode, reserved for future use)",
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=3,
        help="Maximum retry attempts for transient errors",
    )
    parser.add_argument(
        "--base-delay",
        type=int,
        default=5,
        help="Base back-off delay in seconds",
    )
    parser.add_argument(
        "--max-delay",
        type=int,
        default=300,
        help="Maximum back-off delay in seconds",
    )
    parser.add_argument(
        "--lease-seconds",
        type=int,
        default=90,
        help="Duration (seconds) a claimed stage remains leased (default: 90)",
    )
    parser.add_argument(
        "--heartbeat-seconds",
        type=int,
        default=None,
        help="Interval (seconds) between lease-renewal heartbeats "
             "(default: max(1, lease_seconds // 3))",
    )
    args = parser.parse_args()

    worker_id = args.worker_id or f"worker-{uuid.uuid4().hex[:8]}"
    db_path = args.db or os.environ.get("GIFAGENT_TASK_DB", "data/task_state.db")

    # P1-4: Validate heartbeat < lease relationship.
    lease_sec = max(1, args.lease_seconds)
    hb_sec = args.heartbeat_seconds or max(1, lease_sec // 3)
    if hb_sec >= lease_sec:
        print(
            f"ERROR: heartbeat_seconds ({hb_sec}) must be less than "
            f"lease_seconds ({lease_sec})",
            file=sys.stderr,
        )
        sys.exit(1)

    conn = connect_task_db(db_path)
    repo = TaskRepository(conn)
    retry = RetryPolicy(
        max_attempts=args.max_attempts,
        base_delay_seconds=args.base_delay,
        max_delay_seconds=args.max_delay,
    )

    adapters = _build_adapters()
    worker = TaskWorker(
        repo, worker_id, adapters, retry,
        lease_seconds=lease_sec, heartbeat_seconds=hb_sec,
        db_path=db_path,
    )

    if args.once:
        did_work = worker.run_once()
        sys.exit(0 if did_work else 1)
    else:
        count = worker.drain()
        print(f"Processed {count} stages.")
        sys.exit(0)


if __name__ == "__main__":
    main()
