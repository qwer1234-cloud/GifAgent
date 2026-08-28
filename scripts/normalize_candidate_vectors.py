#!/usr/bin/env python3
"""Idempotently L2-normalize stored candidate_vectors blobs.

Fixes mixed-norm contamination from the legacy /api/embeddings path.
Does not call Ollama. Optionally rebuilds (and publishes) a preference profile.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

_project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_project_root))

from app.services.candidate_vectors import renormalize_stored_vectors
from app.services.preference_memory import PreferenceMemoryService
from app.services.preference_schema import apply_preference_schema


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    apply_preference_schema(conn)
    return conn


def main() -> None:
    parser = argparse.ArgumentParser(
        description="L2-normalize stored candidate_vectors (idempotent, local numpy)"
    )
    parser.add_argument(
        "--db",
        default="data/library.db",
        help="SQLite database path, defaults to data/library.db",
    )
    parser.add_argument(
        "--rebuild-profile",
        action="store_true",
        help="Build a new preference profile after renormalizing",
    )
    parser.add_argument(
        "--publish",
        action="store_true",
        help="Publish the rebuilt profile (requires --rebuild-profile)",
    )
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: database not found: {db_path}")
        raise SystemExit(1)

    conn = _connect(db_path)
    try:
        result = renormalize_stored_vectors(conn)
        payload: dict = {"normalize": result}
        if args.rebuild_profile:
            memory = PreferenceMemoryService(conn)
            built = memory.build_profile(dry_run=False)
            payload["profile"] = built
            if args.publish:
                if built.get("status") != "built":
                    payload["publish_error"] = "profile build was blocked"
                else:
                    memory.publish(built["profile_version"])
                    payload["published"] = built["profile_version"]
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        if result.get("updated", 0) == 0 and not args.rebuild_profile:
            return
    finally:
        conn.close()


if __name__ == "__main__":
    main()
