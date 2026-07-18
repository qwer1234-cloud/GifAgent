#!/usr/bin/env python3
"""Submit, refresh, or cancel a benchmark experiment run.

Usage:
    uv run python scripts/run_quality_experiment.py submit <run_id> [--db PATH]
    uv run python scripts/run_quality_experiment.py refresh <run_id> [--db PATH]
    uv run python scripts/run_quality_experiment.py cancel <run_id> [--db PATH]
    uv run python scripts/run_quality_experiment.py create --manifest MANIFEST_ID --config CONFIG_ID --split {tune,holdout} [--db PATH]
    uv run python scripts/run_quality_experiment.py --help
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.quality_lab.schema import connect_quality_db
from app.quality_lab.runner import ExperimentRunner, HttpTaskClient


def _build_runner(db_path: str | None, api_base_url: str = "http://localhost:8000") -> ExperimentRunner:
    """Build an ``ExperimentRunner`` with a quality-lab DB connection and HTTP task client."""
    conn = connect_quality_db(db_path)
    task_client = HttpTaskClient(base_url=api_base_url)
    return ExperimentRunner(conn, task_client)


def cmd_create(args: argparse.Namespace) -> None:
    runner = _build_runner(args.db, args.api_base_url)
    run = runner.create_run(
        manifest_id=args.manifest,
        config_id=args.config,
        split=args.split,
    )
    print(f"Created run: {run.run_id}  (status={run.status})")


def cmd_submit(args: argparse.Namespace) -> None:
    runner = _build_runner(args.db, args.api_base_url)
    job_ids = runner.submit(args.run_id)
    print(f"Submitted {len(job_ids)} job(s) for run {args.run_id}:")
    for jid in job_ids:
        print(f"  {jid}")


def cmd_refresh(args: argparse.Namespace) -> None:
    runner = _build_runner(args.db, args.api_base_url)
    run = runner.refresh(args.run_id)
    print(f"Run {run.run_id} status: {run.status}")


def cmd_cancel(args: argparse.Namespace) -> None:
    runner = _build_runner(args.db, args.api_base_url)
    runner.cancel(args.run_id)
    print(f"Cancelled run {args.run_id}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Manage benchmark experiment runs.",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="Path to quality-lab database (default: data/quality_lab.db).",
    )
    parser.add_argument(
        "--api-base-url",
        default="http://localhost:8000",
        help="Base URL of the task HTTP API (default: http://localhost:8000).",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    # create
    p_create = sub.add_parser("create", help="Create a new experiment run.")
    p_create.add_argument("--manifest", required=True, help="Manifest ID.")
    p_create.add_argument("--config", required=True, help="Experiment config ID.")
    p_create.add_argument(
        "--split", required=True, choices=("tune", "holdout"),
        help="Benchmark split to run.",
    )
    p_create.set_defaults(func=cmd_create)

    # submit
    p_submit = sub.add_parser("submit", help="Submit all items as task jobs.")
    p_submit.add_argument("run_id", help="Experiment run ID.")
    p_submit.set_defaults(func=cmd_submit)

    # refresh
    p_refresh = sub.add_parser("refresh", help="Query task engine for latest state.")
    p_refresh.add_argument("run_id", help="Experiment run ID.")
    p_refresh.set_defaults(func=cmd_refresh)

    # cancel
    p_cancel = sub.add_parser("cancel", help="Cancel all running jobs.")
    p_cancel.add_argument("run_id", help="Experiment run ID.")
    p_cancel.set_defaults(func=cmd_cancel)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
