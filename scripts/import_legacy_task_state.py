#!/usr/bin/env python3
"""Import legacy batch queue/checkpoint state into the task engine DB.

Usage:
  uv run python scripts/import_legacy_task_state.py \
      --queue data/batch_queue.json --state data/batch_queue_state.json \
      --checkpoint data/batch_checkpoint.json --db data/task_state.db
  uv run python scripts/import_legacy_task_state.py ... --dry-run   # counts only, writes nothing
"""
import sys, argparse
from pathlib import Path

# Windows console defaults to GBK — reconfigure to handle Unicode output.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.task_engine.legacy_import import import_legacy_state, plan_legacy_import


def print_counts(prefix, migration_id, jobs_label, jobs_count,
                 videos_reused, videos_pending):
    print(f"{prefix}")
    print(f"migration_id={migration_id}")
    print(f"{jobs_label}={jobs_count}")
    print(f"videos_reused={videos_reused}")
    print(f"videos_pending={videos_pending}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue", required=True, help="legacy batch_queue.json path")
    parser.add_argument("--state", required=True, help="legacy batch_queue_state.json path")
    parser.add_argument("--checkpoint", required=True, help="legacy batch_checkpoint.json path")
    parser.add_argument("--db", required=True, help="task engine SQLite DB path")
    parser.add_argument("--backup-dir", default=None,
                        help="backup directory (default: <db dir>/backups)")
    parser.add_argument("--directory", default=None,
                        help="fallback video directory when the queue has no "
                             "jobs and the checkpoint has no last_run.dir")
    parser.add_argument("--dry-run", action="store_true",
                        help="print planned counts; write nothing (no backups, no DB)")
    args = parser.parse_args()

    queue_path = Path(args.queue)
    state_path = Path(args.state)
    checkpoint_path = Path(args.checkpoint)

    if args.dry_run:
        plan = plan_legacy_import(queue_path, state_path, checkpoint_path,
                                  directory=args.directory)
        print_counts(
            "dry-run: no writes",
            plan.migration_id,
            "jobs_planned",
            len(plan.jobs),
            plan.videos_reused,
            plan.videos_pending,
        )
        return 0

    from app.task_engine import TaskRepository, connect_task_db

    db_path = Path(args.db)
    backup_dir = Path(args.backup_dir) if args.backup_dir else db_path.parent / "backups"
    conn = connect_task_db(db_path)
    try:
        report = import_legacy_state(
            TaskRepository(conn),
            queue_path=queue_path,
            state_path=state_path,
            checkpoint_path=checkpoint_path,
            backup_dir=backup_dir,
            directory=args.directory,
        )
    finally:
        conn.close()
    print_counts(
        "import complete",
        report.migration_id,
        "jobs_created",
        report.jobs_created,
        report.videos_reused,
        report.videos_pending,
    )
    for backup in report.backups:
        print(f"backup={backup}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
