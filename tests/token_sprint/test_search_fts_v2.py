"""W8 shadow FTS v2 (token sprint) tests.

Covers
------
- Additive v2 schema (``fts_v2_content`` + ``candidate_search_fts_v2``).
- Default-off behaviour: without ``GIFAGENT_SEARCH_FTS_V2=1`` the search
  service keeps reading the v1 ``candidate_search_fts`` table.
- Env-gated v2 reader and fail-closed builder behaviour
  (added in the later tasks of the W8 plan).
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from app.services.workbench_schema import apply_search_schema

ENV_FLAG = "GIFAGENT_SEARCH_FTS_V2"


# ── helpers ──────────────────────────────────────────────────────────────────


def _conn() -> sqlite3.Connection:
    """In-memory connection with the workbench (v1 + v2) search schema and a
    minimal ``candidate_gifs`` table."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE candidate_gifs (
               candidate_id TEXT PRIMARY KEY,
               source_video_path TEXT,
               artifact_path TEXT,
               preview_path TEXT,
               start_sec REAL,
               end_sec REAL,
               vlm_summary_json TEXT,
               tags_json TEXT,
               status TEXT,
               final_score REAL,
               created_at TEXT
           )"""
    )
    apply_search_schema(conn)
    return conn


def _insert_candidate(
    conn: sqlite3.Connection,
    candidate_id: str,
    *,
    summary: str = "",
    tags: list[str] | None = None,
    fts_table: str = "candidate_search_fts",
) -> None:
    """Insert a candidate row plus its FTS index entry."""
    conn.execute(
        """INSERT INTO candidate_gifs
           (candidate_id, source_video_path, artifact_path, preview_path,
            start_sec, end_sec, vlm_summary_json, tags_json, status,
            final_score, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (
            candidate_id,
            f"/videos/{candidate_id}.mp4",
            f"data/exports/{candidate_id}.gif",
            f"data/thumbs/{candidate_id}.jpg",
            0.0,
            5.0,
            json.dumps({"caption": summary}),
            json.dumps(tags or []),
            "candidate",
            0.5,
            "2026-07-18T00:00:00+00:00",
        ),
    )
    conn.execute(
        f"INSERT INTO {fts_table}(candidate_id, summary, tags, source_path) "
        "VALUES (?,?,?,?)",
        (candidate_id, summary, " ".join(tags or []), f"/videos/{candidate_id}.mp4"),
    )
    conn.commit()


# ── schema ───────────────────────────────────────────────────────────────────


class TestFtsV2Schema:
    def test_apply_search_schema_creates_v2_tables(self) -> None:
        conn = _conn()
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE name IN ('fts_v2_content', 'candidate_search_fts_v2')"
            ).fetchall()
        }
        assert tables == {"fts_v2_content", "candidate_search_fts_v2"}

    def test_content_table_has_primary_key_and_not_null_columns(self) -> None:
        conn = _conn()
        cols = {
            row["name"]: row
            for row in conn.execute("PRAGMA table_info(fts_v2_content)").fetchall()
        }
        assert set(cols) == {
            "candidate_id",
            "summary",
            "tags",
            "source_path",
            "enrichment_manifest_sha",
        }
        assert cols["candidate_id"]["pk"] == 1
        for name in ("summary", "tags", "source_path", "enrichment_manifest_sha"):
            assert cols[name]["notnull"] == 1

    def test_v2_virtual_table_matches_locked_ddl(self) -> None:
        conn = _conn()
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='candidate_search_fts_v2'"
        ).fetchone()[0]
        assert "candidate_id UNINDEXED" in sql
        assert "tokenize='porter unicode61'" in sql

    def test_v2_rows_survive_only_via_content_table_pk(self) -> None:
        """The v2 FTS virtual table has no unique constraint on candidate_id;
        the uniqueness guarantee lives in fts_v2_content."""
        conn = _conn()
        # Demonstrate the documented FTS5 pitfall: duplicate candidate_id rows
        # are accepted by the virtual table, hence the full-delete reinsert
        # policy in the builder.
        conn.execute(
            "INSERT INTO candidate_search_fts_v2"
            "(candidate_id, summary, tags, source_path) VALUES ('c1','a','','')"
        )
        conn.execute(
            "INSERT INTO candidate_search_fts_v2"
            "(candidate_id, summary, tags, source_path) VALUES ('c1','b','','')"
        )
        dupes = conn.execute(
            "SELECT COUNT(*) FROM candidate_search_fts_v2 WHERE candidate_id='c1'"
        ).fetchone()[0]
        assert dupes == 2


# ── default-off behaviour ────────────────────────────────────────────────────


class TestDefaultOff:
    def test_flag_is_absent_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(ENV_FLAG, raising=False)
        import os

        assert os.environ.get(ENV_FLAG) != "1"

    def test_default_search_uses_v1_index(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(ENV_FLAG, raising=False)
        from app.services.library_search import LibrarySearchService
        from app.services.workbench_schema import SearchQuery

        conn = _conn()
        # v1 has a match; v2 stays empty.
        _insert_candidate(conn, "v1-cand", summary="sunset beach walk")

        service = LibrarySearchService(conn)
        page = service.search(SearchQuery(text="sunset"))
        assert [item.candidate_id for item in page.items] == ["v1-cand"]
        assert page.total == 1

    def test_v2_content_alone_is_not_searchable_by_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(ENV_FLAG, raising=False)
        from app.services.library_search import LibrarySearchService
        from app.services.workbench_schema import SearchQuery

        conn = _conn()
        _insert_candidate(conn, "v1-cand", summary="sunset beach walk")
        conn.execute(
            "INSERT INTO fts_v2_content"
            "(candidate_id, summary, tags, source_path, enrichment_manifest_sha) "
            "VALUES ('v2-only', 'kitchen morning', '', '', 'deadbeef')"
        )
        conn.execute(
            "INSERT INTO candidate_search_fts_v2"
            "(candidate_id, summary, tags, source_path) "
            "VALUES ('v2-only', 'kitchen morning', '', '')"
        )
        conn.commit()

        service = LibrarySearchService(conn)
        page = service.search(SearchQuery(text="kitchen"))
        assert page.total == 0
        assert page.items == []
