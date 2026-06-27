"""Evaluate a preference profile against a holdout judgment set.

Usage:
    python scripts/evaluate_preference.py --profile-version <version> --holdout <jsonl>
    python scripts/evaluate_preference.py --profile-version <version> --holdout-count <N>

Prints the evaluation report as JSON to stdout.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# When running as a script the project root may not be on sys.path.
_script_dir = Path(__file__).resolve().parent
_project_root = _script_dir.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from app.db import get_connection
from app.services.preference_schema import apply_preference_schema
from app.services.preference_evaluation import PreferenceEvaluationService


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Evaluate a preference profile against a holdout set."
    )
    parser.add_argument(
        "--profile-version",
        required=True,
        help="Profile version to evaluate (e.g. profile_abc123def45678)",
    )
    parser.add_argument(
        "--holdout",
        type=Path,
        default=None,
        help="Path to JSONL file with holdout judgments",
    )
    parser.add_argument(
        "--holdout-count",
        type=int,
        default=0,
        help="Generate N synthetic holdout judgments (for testing)",
    )
    args = parser.parse_args()

    if args.holdout is None and args.holdout_count <= 0:
        parser.error("Either --holdout <jsonl> or --holdout-count <N> is required.")

    conn = get_connection()
    apply_preference_schema(conn)

    service = PreferenceEvaluationService(conn)
    report = service.evaluate(
        args.profile_version,
        holdout_path=args.holdout,
        holdout_count=args.holdout_count,
    )

    print(json.dumps(report, indent=2, ensure_ascii=False))

    if not report["can_publish"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
