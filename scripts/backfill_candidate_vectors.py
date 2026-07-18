#!/usr/bin/env python3
"""Backfill candidate_vectors for existing candidate_gifs rows."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

_project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_project_root))

from app.config import load_config
from app.services.candidate_vectors import backfill_candidate_vectors
from app.services.embedding import compute_text_embedding
from app.services.preference_schema import apply_preference_schema


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    apply_preference_schema(conn)
    return conn


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill candidate GIF embeddings")
    parser.add_argument(
        "--db",
        default="data/library.db",
        help="SQLite database path, defaults to data/library.db",
    )
    parser.add_argument(
        "--feedback-only",
        action="store_true",
        help="Restrict to only like/dislike feedback targets (default: all candidates)",
    )
    parser.add_argument("--limit", type=int, default=None, help="Maximum vectors to insert")
    parser.add_argument("--dry-run", action="store_true", help="Count missing vectors without embedding")
    args = parser.parse_args()

    load_config()
    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: database not found: {db_path}")
        raise SystemExit(1)

    conn = _connect(db_path)
    try:
        result = backfill_candidate_vectors(
            conn,
            embed_fn=compute_text_embedding,
            only_feedback=args.feedback_only,
            dry_run=args.dry_run,
            limit=args.limit,
        )
    finally:
        conn.close()

    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result.get("failed"):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
