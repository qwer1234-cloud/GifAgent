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
from app.services.embedding import (
    EmbeddingServiceUnavailable,
    check_embedding_service,
    compute_text_embedding,
    compute_text_embeddings_batch,
)
from app.services.preference_schema import apply_preference_schema


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
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
    parser.add_argument(
        "--retry-excluded",
        action="store_true",
        help="Retry candidates previously written to candidate_vector_exclusions",
    )
    parser.add_argument(
        "--missing-only",
        action="store_true",
        help="Insert missing vectors only; do not refresh schema/hash-stale blobs",
    )
    args = parser.parse_args()

    load_config()
    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: database not found: {db_path}")
        raise SystemExit(1)

    if not args.dry_run:
        try:
            check_embedding_service()
        except EmbeddingServiceUnavailable as exc:
            print(
                json.dumps(
                    {"status": "paused", "error": str(exc)},
                    ensure_ascii=False,
                    indent=2,
                )
            )
            raise SystemExit(2)

    conn = _connect(db_path)
    try:
        result = backfill_candidate_vectors(
            conn,
            embed_fn=compute_text_embedding,
            batch_embed_fn=compute_text_embeddings_batch,
            only_feedback=args.feedback_only,
            dry_run=args.dry_run,
            limit=args.limit,
            retry_excluded=args.retry_excluded,
            missing_only=args.missing_only,
        )
    finally:
        conn.close()

    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result.get("aborted") or result.get("failed"):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
