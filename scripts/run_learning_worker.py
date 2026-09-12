"""Standalone background worker for curve preference learning jobs."""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from app.db import get_connection
from app.services.learning_jobs import run_learning_once
from app.services.preference_schema import apply_preference_schema


def main() -> int:
    parser = argparse.ArgumentParser(description="Run background learning worker loop or once.")
    parser.add_argument("--once", action="store_true", help="Process one job and exit.")
    parser.add_argument("--interval", type=int, default=60, help="Polling interval in seconds.")
    parser.add_argument("--owner", default="standalone_worker", help="Lease owner name.")
    parser.add_argument(
        "--lease-seconds", type=int, default=1800, help="Lease duration in seconds."
    )
    args = parser.parse_args()

    conn = get_connection()
    try:
        apply_preference_schema(conn)
        while True:
            res = run_learning_once(
                conn,
                now_epoch=time.time(),
                owner=args.owner,
                lease_seconds=args.lease_seconds,
            )
            if res["status"] != "IDLE":
                print(
                    f"[LearningWorker] job={res.get('job_id')} "
                    f"stage={res.get('stage')} status={res['status']} "
                    f"error={res.get('error_code')}"
                )
            if args.once:
                break
            time.sleep(args.interval)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
