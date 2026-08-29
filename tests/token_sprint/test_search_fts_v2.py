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
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.token_sprint.coordinator.common import sha256_file
from scripts.token_sprint.coordinator.protocol import (
    write_done,
    write_ready,
    write_receipt,
)

from app.services.workbench_schema import apply_search_schema

ENV_FLAG = "GIFAGENT_SEARCH_FTS_V2"

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BUILDER = PROJECT_ROOT / "scripts" / "token_sprint" / "build_fts_v2.py"
MANIFEST_SCHEMA = "gifagent.token_sprint.approved_enrichment_manifest.v1"


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


# ── builder: synthetic approved-manifest fixture ─────────────────────────────


DEFAULT_SHARD_SPECS = [
    (
        "p1b",
        [
            {"candidate_id": "c1", "tags": ["kitchen", "nudity"]},
            {"candidate_id": "c2", "tags": ["bedroom"]},
        ],
    ),
    (
        "p2",
        [
            {
                "candidate_id": "c1",
                "scene": {"setting": "kitchen", "act": None, "finish": "smiling"},
            },
            {
                "candidate_id": "c2",
                "scene": {"setting": "bedroom", "act": "posing", "finish": None},
            },
            {"candidate_id": "c9", "refused": True},
        ],
    ),
    (
        "p3",
        [
            {"candidate_id": "c1", "keywords": ["intimacy", "kitchen"]},
            {"candidate_id": "c3", "keywords": ["couple"]},
        ],
    ),
]


def _make_sprint_root(
    root: Path,
    shard_specs: list[tuple[str, list[dict]]] | None = None,
) -> dict:
    """Build a synthetic approved-manifest sprint root; return the manifest."""
    root.mkdir(parents=True, exist_ok=True)
    shard_specs = DEFAULT_SHARD_SPECS if shard_specs is None else shard_specs
    shards_dir = root / "shards"
    shards_dir.mkdir(parents=True, exist_ok=True)
    (root / "receipts").mkdir(parents=True, exist_ok=True)

    entries = []
    for i, (pass_id, rows) in enumerate(shard_specs, 1):
        shard_id = f"0000000{i}-0000-4000-8000-{i:012d}"
        path = shards_dir / f"shard-{i:03d}.{pass_id}.v1.{shard_id}.jsonl"
        # Raw string rows are written verbatim (used to inject invalid JSON).
        lines = [row if isinstance(row, str) else json.dumps(row) for row in rows]
        path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
        ready = write_ready(path, producer="w8-test-executor", qc_summary={})
        done = write_done(
            path,
            ready,
            reviewer="w8-test-reviewer",
            qc_evidence={"fixture": True},
        )
        write_receipt(
            sprint_root=root,
            workstream="w8",
            producer="w8-test-executor",
            reviewer="w8-test-reviewer",
            artifact=path,
            ready=ready,
            done=done,
        )
        entries.append(
            {
                "pass_id": pass_id,
                "prompt_version": "v1",
                "shard_id": shard_id,
                "artifact_path": path.resolve().as_posix(),
                "sha256": sha256_file(path),
                "manifest_artifact_id": f"artifact-{i}",
            }
        )

    manifest = {
        "schema": MANIFEST_SCHEMA,
        "generated_by": "w8-test",
        "shards": entries,
    }
    (root / "approved_enrichment_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    # inputs manifest for the production-db guard (builder requires it).
    production_db = root / "_production_library.db"
    production_db.write_bytes(b"")
    (root / "inputs_manifest.json").write_text(
        json.dumps({"source_db_abs": production_db.resolve().as_posix()}) + "\n",
        encoding="utf-8",
    )
    return manifest


def _run_builder(
    args: list[str],
    *,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    full_env = os.environ.copy()
    full_env.pop("GIFAGENT_SPRINT_ROOT", None)
    if env is not None:
        full_env.update(env)
    return subprocess.run(
        [sys.executable, str(BUILDER), *args],
        capture_output=True,
        text=True,
        env=full_env,
        cwd=str(PROJECT_ROOT),
    )


def _build_once(sprint_root: Path, db: Path) -> subprocess.CompletedProcess:
    db.touch()
    return _run_builder(["--sprint-root", str(sprint_root), "--db", str(db)])


EXPECTED_CONTENT = {
    # c1: p1b tags + p3 keywords appended (dedup), p2 scene serialized sorted.
    "c1": ("finish=smiling setting=kitchen", "kitchen nudity intimacy"),
    "c2": ("act=posing setting=bedroom", "bedroom"),
    "c3": ("", "couple"),
}


class TestBuildFtsV2Success:
    def test_build_success_counts_merge_and_provenance(self, tmp_path: Path) -> None:
        sprint_root = tmp_path / "sprint"
        _make_sprint_root(sprint_root)
        db = tmp_path / "shadow.db"

        result = _build_once(sprint_root, db)
        assert result.returncode == 0, result.stderr
        summary = json.loads(result.stdout)
        assert summary["shards_accepted"] == 3
        assert summary["rows_per_pass"] == {"p1b": 2, "p2": 2, "p3": 2}
        assert summary["same_pass_overwrites"] == 0

        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        count, distinct = conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT candidate_id) FROM fts_v2_content"
        ).fetchone()
        assert count == distinct == 3
        fts_count, fts_distinct = conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT candidate_id) "
            "FROM candidate_search_fts_v2"
        ).fetchone()
        assert fts_count == fts_distinct == 3

        rows = {
            row["candidate_id"]: (row["summary"], row["tags"])
            for row in conn.execute(
                "SELECT candidate_id, summary, tags FROM fts_v2_content"
            ).fetchall()
        }
        assert rows == EXPECTED_CONTENT
        # source_path is empty for corpus rows; provenance column binds the
        # approved manifest file itself.
        for row in conn.execute(
            "SELECT source_path, enrichment_manifest_sha FROM fts_v2_content"
        ).fetchall():
            assert row["source_path"] == ""
            assert (
                row["enrichment_manifest_sha"]
                == sha256_file(
                    sprint_root / "approved_enrichment_manifest.json"
                )
            )
        conn.close()

    def test_double_build_deterministic(self, tmp_path: Path) -> None:
        sprint_root = tmp_path / "sprint"
        _make_sprint_root(sprint_root)
        db = tmp_path / "shadow.db"

        first = _build_once(sprint_root, db)
        assert first.returncode == 0, first.stderr
        second = _build_once(sprint_root, db)
        assert second.returncode == 0, second.stderr

        s1 = json.loads(first.stdout)
        s2 = json.loads(second.stdout)
        keys = (
            "content_rows",
            "content_distinct_candidate_id",
            "fts_rows",
            "fts_distinct_candidate_id",
            "fixed_query_digests_sha256",
        )
        for key in keys:
            assert s1[key] == s2[key], key
        assert s1["fixed_query_digests"] == s2["fixed_query_digests"]
        assert s1["content_rows"] == s1["fts_rows"] == 3

    def test_refused_rows_never_reach_content(self, tmp_path: Path) -> None:
        sprint_root = tmp_path / "sprint"
        _make_sprint_root(sprint_root)
        db = tmp_path / "shadow.db"
        result = _build_once(sprint_root, db)
        assert result.returncode == 0, result.stderr
        conn = sqlite3.connect(str(db))
        ids = {
            row[0]
            for row in conn.execute("SELECT candidate_id FROM fts_v2_content")
        }
        conn.close()
        assert "c9" not in ids


class TestBuildFtsV2FailClosed:
    def test_tampered_shard_byte_fails_closed_and_db_untouched(
        self, tmp_path: Path
    ) -> None:
        sprint_root = tmp_path / "sprint"
        _make_sprint_root(sprint_root)
        db = tmp_path / "shadow.db"
        first = _build_once(sprint_root, db)
        assert first.returncode == 0, first.stderr
        sha_before = sha256_file(db)

        # Flip one byte inside an approved shard (still valid JSONL text).
        shard = next((sprint_root / "shards").glob("shard-001.p1b.*.jsonl"))
        data = shard.read_bytes()
        shard.write_bytes(data.replace(b"kitchen", b"Kitchen", 1))

        second = _build_once(sprint_root, db)
        assert second.returncode == 2
        assert "byte drift" in second.stderr
        assert sha256_file(db) == sha_before

    def test_missing_done_fails_closed(self, tmp_path: Path) -> None:
        sprint_root = tmp_path / "sprint"
        _make_sprint_root(sprint_root)
        db = tmp_path / "shadow.db"
        db.touch()
        next((sprint_root / "shards").glob("shard-001.p1b.*.jsonl.done")).unlink()
        result = _build_once(sprint_root, db)
        assert result.returncode == 2
        assert ".done missing" in result.stderr

    def test_missing_receipt_fails_closed(self, tmp_path: Path) -> None:
        sprint_root = tmp_path / "sprint"
        _make_sprint_root(sprint_root)
        db = tmp_path / "shadow.db"
        db.touch()
        for receipt in (sprint_root / "receipts").glob("*.json"):
            receipt.unlink()
        result = _build_once(sprint_root, db)
        assert result.returncode == 2
        assert "no receipt" in result.stderr

    def test_enrichment_db_is_never_consumed(self, tmp_path: Path) -> None:
        sprint_root = tmp_path / "sprint"
        _make_sprint_root(sprint_root)
        db = tmp_path / "shadow.db"
        db.touch()

        # A plausible enrichment.db that would silently "fix" the missing
        # shard if the builder ever consumed it. The build must still fail.
        enrichment = sprint_root / "enrichment.db"
        conn = sqlite3.connect(str(enrichment))
        conn.execute(
            "CREATE TABLE enrichment_rows (candidate_id TEXT, tags TEXT)"
        )
        conn.execute(
            "INSERT INTO enrichment_rows VALUES ('c4', 'should_not_index')"
        )
        conn.commit()
        conn.close()

        next((sprint_root / "shards").glob("shard-003.p3.*.jsonl")).unlink()

        result = _build_once(sprint_root, db)
        assert result.returncode == 2
        assert "missing" in result.stderr

    def test_invalid_json_row_fails_closed(self, tmp_path: Path) -> None:
        sprint_root = tmp_path / "sprint"
        # Raw invalid JSON is written verbatim and bound into the manifest by
        # the fixture, so the builder reaches its parser and fails there.
        _make_sprint_root(
            sprint_root,
            shard_specs=[("p1b", ['{"candidate_id": "c1", "tags":', "{not json}"])],
        )
        db = tmp_path / "shadow.db"
        db.touch()
        result = _build_once(sprint_root, db)
        assert result.returncode == 2
        assert "invalid JSON" in result.stderr

    def test_schema_violating_row_fails_closed(self, tmp_path: Path) -> None:
        sprint_root = tmp_path / "sprint"
        _make_sprint_root(
            sprint_root,
            shard_specs=[("p2", [{"candidate_id": "c1", "scene": "not-an-object"}])],
        )
        db = tmp_path / "shadow.db"
        db.touch()
        result = _build_once(sprint_root, db)
        assert result.returncode == 2
        assert "must be an object" in result.stderr

    def test_reject_dist_db_target(self, tmp_path: Path) -> None:
        sprint_root = tmp_path / "sprint"
        _make_sprint_root(sprint_root)
        dist_db = tmp_path / "dist" / "GifAgentUI" / "data" / "library.db"
        dist_db.parent.mkdir(parents=True)
        dist_db.touch()
        result = _build_once(sprint_root, dist_db)
        assert result.returncode == 2
        assert "dist" in result.stderr

    def test_reject_production_source_db_target(self, tmp_path: Path) -> None:
        sprint_root = tmp_path / "sprint"
        _make_sprint_root(sprint_root)
        production_db = sprint_root / "_production_library.db"
        result = _build_once(sprint_root, production_db)
        assert result.returncode == 2
        assert "production" in result.stderr

    def test_reject_snapshot_original(self, tmp_path: Path) -> None:
        sprint_root = tmp_path / "sprint"
        _make_sprint_root(sprint_root)
        snapshot = sprint_root / "library_snapshot.db"
        snapshot.write_bytes(b"")
        result = _build_once(sprint_root, snapshot)
        assert result.returncode == 2
        assert "snapshot" in result.stderr


class TestBuildFtsV2Cli:
    def test_missing_sprint_root_and_env_exit_2(self, tmp_path: Path) -> None:
        db = tmp_path / "shadow.db"
        db.touch()
        result = _run_builder(["--db", str(db)])
        assert result.returncode == 2
        assert "--sprint-root missing" in result.stderr

    def test_env_fallback_sprint_root(self, tmp_path: Path) -> None:
        sprint_root = tmp_path / "sprint"
        _make_sprint_root(sprint_root)
        db = tmp_path / "shadow.db"
        db.touch()
        result = _run_builder(
            ["--db", str(db)], env={"GIFAGENT_SPRINT_ROOT": str(sprint_root)}
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["content_rows"] == 3

    def test_env_sprint_root_missing_dir_exit_2(self, tmp_path: Path) -> None:
        db = tmp_path / "shadow.db"
        db.touch()
        result = _run_builder(
            ["--db", str(db)],
            env={"GIFAGENT_SPRINT_ROOT": str(tmp_path / "does-not-exist")},
        )
        assert result.returncode == 2


class TestMergeRules:
    def test_same_pass_cross_shard_later_manifest_entry_wins(
        self, tmp_path: Path
    ) -> None:
        sprint_root = tmp_path / "sprint"
        _make_sprint_root(
            sprint_root,
            shard_specs=[
                ("p3", [{"candidate_id": "c1", "keywords": ["first", "old"]}]),
                ("p3", [{"candidate_id": "c1", "keywords": ["second"]}]),
            ],
        )
        db = tmp_path / "shadow.db"
        result = _build_once(sprint_root, db)
        assert result.returncode == 0, result.stderr
        summary = json.loads(result.stdout)
        assert summary["same_pass_overwrites"] == 1
        conn = sqlite3.connect(str(db))
        tags = conn.execute(
            "SELECT tags FROM fts_v2_content WHERE candidate_id='c1'"
        ).fetchone()[0]
        conn.close()
        assert tags == "second"


# ── env-gated v2 reader ──────────────────────────────────────────────────────


def _seed_split_indexes(conn: sqlite3.Connection) -> None:
    """v1 index knows 'sunset beach', v2 index knows 'kitchen morning'."""
    _insert_candidate(conn, "v1-cand", summary="sunset beach walk")
    _insert_candidate(
        conn,
        "v2-cand",
        summary="kitchen morning",
        fts_table="candidate_search_fts_v2",
    )


class TestEnvGate:
    def test_env_on_reads_v2_index(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_FLAG, "1")
        from app.services.library_search import LibrarySearchService
        from app.services.workbench_schema import SearchQuery

        conn = _conn()
        _seed_split_indexes(conn)
        service = LibrarySearchService(conn)

        page = service.search(SearchQuery(text="kitchen"))
        assert page.total == 1
        assert [item.candidate_id for item in page.items] == ["v2-cand"]

        # v1-only content is invisible through the v2 reader.
        page_v1 = service.search(SearchQuery(text="sunset"))
        assert page_v1.total == 0
        assert page_v1.items == []

    def test_env_off_reads_v1_index_only(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(ENV_FLAG, raising=False)
        from app.services.library_search import LibrarySearchService
        from app.services.workbench_schema import SearchQuery

        conn = _conn()
        _seed_split_indexes(conn)
        service = LibrarySearchService(conn)

        page = service.search(SearchQuery(text="sunset"))
        assert page.total == 1
        assert [item.candidate_id for item in page.items] == ["v1-cand"]

        page_v2 = service.search(SearchQuery(text="kitchen"))
        assert page_v2.total == 0
        assert page_v2.items == []

    @pytest.mark.parametrize("value", ["0", "true", "yes", ""])
    def test_any_value_other_than_1_stays_v1(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        monkeypatch.setenv(ENV_FLAG, value)
        from app.services.library_search import LibrarySearchService
        from app.services.workbench_schema import SearchQuery

        conn = _conn()
        _seed_split_indexes(conn)
        service = LibrarySearchService(conn)
        page = service.search(SearchQuery(text="sunset"))
        assert [item.candidate_id for item in page.items] == ["v1-cand"]

    def test_env_on_with_missing_v2_table_reports_diagnosis(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(ENV_FLAG, "1")
        from app.services.library_search import LibrarySearchService
        from app.services.workbench_schema import SearchQuery

        conn = _conn()
        _insert_candidate(conn, "v1-cand", summary="sunset beach walk")
        service = LibrarySearchService(conn)
        conn.execute("DROP TABLE candidate_search_fts_v2")
        conn.commit()

        page = service.search(SearchQuery(text="sunset"))
        assert page.total == 0
        assert page.degraded is True
        assert page.diagnosis is not None
        assert "FTS v2 missing" in page.diagnosis

    def test_env_off_with_missing_v2_table_behaves_as_before(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(ENV_FLAG, raising=False)
        from app.services.library_search import LibrarySearchService
        from app.services.workbench_schema import SearchQuery

        conn = _conn()
        _insert_candidate(conn, "v1-cand", summary="sunset beach walk")
        service = LibrarySearchService(conn)
        conn.execute("DROP TABLE candidate_search_fts_v2")
        conn.commit()

        page = service.search(SearchQuery(text="sunset"))
        assert page.total == 1
        assert [item.candidate_id for item in page.items] == ["v1-cand"]
        assert "FTS v2 missing" not in (page.diagnosis or "")

