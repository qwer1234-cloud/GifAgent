"""Regression tests for schemas required immediately after application startup."""
from __future__ import annotations

import sqlite3


def test_init_db_applies_workbench_schemas(monkeypatch, tmp_path):
    """Direct startup routes must not run before search/collection tables exist."""
    from app import db

    db_path = tmp_path / "library.db"
    monkeypatch.setattr(db, "DB_PATH", str(db_path))

    db.init_db(apply_preference=True)

    with sqlite3.connect(db_path) as conn:
        table_names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

    assert {
        "collections",
        "collection_versions",
        "collection_items",
        "candidate_search_fts",
        "search_index_state",
    } <= table_names
