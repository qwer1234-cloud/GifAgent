#!/usr/bin/env python3
"""Promote a DRAFT curve preference model to PUBLISHED after qualification.

This is the only command that can flip a curve model from DRAFT to
PUBLISHED, and it requires a verified QUALIFIED qualification bundle:

    python scripts/promote_curve_preference_model.py \
        --db dist/GifAgentUI/data/library.db \
        --model-version curve-pref-v1-20260905120400 \
        --qualification-dir data/curve_preference_qualification/<qualification_id>

Add ``--dry-run`` to validate only (no backup, no transaction). Promotion
does not enable production reranking and never touches ranking configs.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.curve_preference import (  # noqa: E402
    promotion_backup_database,
    promote_model,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db", default="dist/GifAgentUI/data/library.db",
        help="production DB (the only database promotion writes to)",
    )
    parser.add_argument("--model-version", required=True)
    parser.add_argument(
        "--qualification-dir", required=True,
        help="run directory of the QUALIFIED bundle to replay",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="validate everything; no backup and no transaction",
    )
    args = parser.parse_args(argv)

    if args.dry_run:
        result = promote_model(
            sqlite3.connect(str(args.db), timeout=60),
            model_version=args.model_version,
            run_dir=Path(args.qualification_dir),
            dry_run=True,
        )
    else:
        backup_path = promotion_backup_database(Path(args.db))
        result = promote_model(
            sqlite3.connect(str(args.db), timeout=60),
            model_version=args.model_version,
            run_dir=Path(args.qualification_dir),
            dry_run=False,
        )
        result["backup_path"] = str(backup_path)

    print(
        json.dumps(
            result,
            sort_keys=True,
            allow_nan=False,
            indent=1,
            default=str,
        )
    )
    return 0 if result.get("status") in ("PROMOTED", "DRY_RUN_OK") else 1


if __name__ == "__main__":
    sys.exit(main())
