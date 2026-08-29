"""W1 regression lock: FTS rebuild must force a full reinsert on the snapshot.

Locks the snapshot rebuild contract (implementation plan "Snapshot rebuild
contract (lock)"): ``LibrarySearchService.rebuild_index()`` resumes
incrementally from ``search_index_state.last_candidate_id``, so a database in
the "FTS empty but state complete" state would be skipped entirely
(``scanned=0``). ``force_rebuild_fts`` must delete both ``candidate_search_fts``
and ``search_index_state`` before rebuilding, and the three counts must agree
with ``candidate_gifs``.

The CLI tests build their own minimal source SQLite files under ``tmp_path``;
the real production database is never touched here.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from app.services.workbench_schema import RebuildReport, apply_search_schema
from scripts.token_sprint.snapshot_library import (
    GuiBusyError,
    RebuildContractError,
    force_rebuild_fts,
    main,
    open_source_readonly,
    vacuum_into,
)

# ── helpers ──────────────────────────────────────────────────────────────────

MINIMAL_CANDIDATE_GIFS_DDL = """
CREATE TABLE IF NOT EXISTS candidate_gifs (
    candidate_id TEXT PRIMARY KEY,
    vlm_summary_json TEXT NOT NULL DEFAULT '{}',
    tags_json TEXT NOT NULL DEFAULT '[]',
    scenario_keys_json TEXT NOT NULL DEFAULT '[]',
    source_video_path TEXT,
    artifact_path TEXT,
    preview_path TEXT,
    final_score REAL,
    status TEXT NOT NULL DEFAULT 'candidate',
    created_at TEXT NOT NULL
);
"""


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(MINIMAL_CANDIDATE_GIFS_DDL)
    apply_search_schema(conn)
    return conn


def _seed(conn: sqlite3.Connection, n: int = 5) -> list[tuple[str, str]]:
    """Insert *n* candidates; return (candidate_id, created_at) in index order."""
    rows: list[tuple[str, str]] = []
    for i in range(n):
        candidate_id = f"cand-{i}"
        created_at = f"2026-07-{i + 1:02d}T00:00:00+00:00"
        conn.execute(
            """INSERT INTO candidate_gifs
               (candidate_id, vlm_summary_json, tags_json, created_at)
               VALUES (?, ?, ?, ?)""",
            (candidate_id, json.dumps({"caption": f"clip {i}"}), json.dumps([f"tag{i}"]), created_at),
        )
        rows.append((candidate_id, created_at))
    conn.commit()
    return rows


def _mark_state_complete(
    conn: sqlite3.Connection, rows: list[tuple[str, str]]
) -> None:
    """Write search_index_state as if everything were already indexed."""
    last_id, last_created_at = rows[-1]
    conn.execute(
        """INSERT OR REPLACE INTO search_index_state
           (id, last_candidate_id, last_candidate_created_at,
            indexed_count, total_count, updated_at)
           VALUES (1, ?, ?, ?, ?, '2026-07-31T00:00:00+00:00')""",
        (last_id, last_created_at, len(rows), len(rows)),
    )
    conn.commit()


def _fts_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM candidate_search_fts").fetchone()[0]


def _build_source_db(path: Path, n: int = 3) -> None:
    """Build a minimal standalone source database (never the production one)."""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(MINIMAL_CANDIDATE_GIFS_DDL)
        for i in range(n):
            conn.execute(
                """INSERT INTO candidate_gifs
                   (candidate_id, vlm_summary_json, tags_json, created_at)
                   VALUES (?, ?, ?, ?)""",
                (
                    f"cand-{i}",
                    json.dumps({"caption": f"clip {i}"}),
                    json.dumps([f"tag{i}"]),
                    f"2026-07-{i + 1:02d}T00:00:00+00:00",
                ),
            )
        apply_search_schema(conn)
        conn.commit()
    finally:
        conn.close()


def _write_sprint_root(sprint_root: Path, source_db: Path) -> None:
    """Create a sprint root whose inputs manifest is pinned to *source_db*."""
    sprint_root.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema": "gifagent.token_sprint.inputs.v1",
        "source_db_abs": source_db.resolve().as_posix(),
    }
    manifest_path = sprint_root / "inputs_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    manifest_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    canonical = {
        "control_plane": {"inputs_manifest.json": {"sha256": manifest_sha}},
    }
    (sprint_root / "sprint_manifest.json").write_text(
        json.dumps(canonical, indent=2), encoding="utf-8"
    )


# ── regression lock: force_rebuild_fts ───────────────────────────────────────


class TestRebuildContract:
    def test_rebuild_without_clear_skips_when_state_complete(self):
        """Contrast path: plain rebuild_index() must NOT repopulate the FTS."""
        conn = _conn()
        rows = _seed(conn)
        _mark_state_complete(conn, rows)
        assert _fts_count(conn) == 0

        from app.services.library_search import LibrarySearchService

        report = LibrarySearchService(conn).rebuild_index()
        assert report.scanned == 0
        assert report.inserted == 0
        assert _fts_count(conn) == 0  # still empty: the regression we lock against

    def test_force_rebuild_repopulates_empty_fts_with_complete_state(self):
        conn = _conn()
        rows = _seed(conn, n=5)
        _mark_state_complete(conn, rows)
        assert _fts_count(conn) == 0

        report = force_rebuild_fts(conn)

        assert report.errors == 0
        assert report.scanned == 5
        assert report.inserted == 5
        assert _fts_count(conn) == 5
        fts_ids = conn.execute(
            "SELECT COUNT(DISTINCT candidate_id) FROM candidate_search_fts"
        ).fetchone()[0]
        candidate_rows = conn.execute("SELECT COUNT(*) FROM candidate_gifs").fetchone()[0]
        assert _fts_count(conn) == fts_ids == candidate_rows == 5

    def test_force_rebuild_raises_on_error_report(self, monkeypatch):
        conn = _conn()
        _seed(conn, n=3)
        monkeypatch.setattr(
            "app.services.library_search.LibrarySearchService.rebuild_index",
            lambda self: RebuildReport(
                scanned=0,
                inserted=0,
                skipped=0,
                errors=1,
                error_details=["boom"],
            ),
        )
        with pytest.raises(RebuildContractError, match="boom"):
            force_rebuild_fts(conn)

    def test_force_rebuild_raises_on_count_mismatch(self, monkeypatch):
        conn = _conn()
        _seed(conn, n=3)
        # A broken "rebuild" that inserts only one FTS row but reports no errors.
        def _partial_rebuild(self):
            self.conn.execute(
                "INSERT OR REPLACE INTO candidate_search_fts "
                "(candidate_id, summary, tags, source_path) VALUES (?, ?, ?, ?)",
                ("cand-0", "partial", "", ""),
            )
            self.conn.commit()
            return RebuildReport(scanned=1, inserted=1, skipped=0, errors=0)

        monkeypatch.setattr(
            "app.services.library_search.LibrarySearchService.rebuild_index",
            _partial_rebuild,
        )
        with pytest.raises(RebuildContractError, match="三计数不一致"):
            force_rebuild_fts(conn)


# ── CLI-level contract on tmp_path source databases ──────────────────────────


class TestSnapshotCli:
    def test_snapshot_creates_snapshot_and_reports_counts(self, tmp_path, capsys):
        source_db = tmp_path / "library.db"
        _build_source_db(source_db, n=3)
        sprint_root = tmp_path / "sprint"
        _write_sprint_root(sprint_root, source_db)

        rc = main(["--sprint-root", str(sprint_root)])

        assert rc == 0
        out = capsys.readouterr().out
        assert "candidate_gifs COUNT(*) = 3" in out
        assert "rebuild errors = 0" in out

        snap = sprint_root / "library_snapshot.db"
        assert snap.is_file()
        snap_conn = sqlite3.connect(str(snap))
        try:
            counts = (
                snap_conn.execute("SELECT COUNT(*) FROM candidate_search_fts").fetchone()[0],
                snap_conn.execute(
                    "SELECT COUNT(DISTINCT candidate_id) FROM candidate_search_fts"
                ).fetchone()[0],
                snap_conn.execute("SELECT COUNT(*) FROM candidate_gifs").fetchone()[0],
            )
        finally:
            snap_conn.close()
        assert counts == (3, 3, 3)

    def test_refuses_overwrite_without_force(self, tmp_path, capsys):
        source_db = tmp_path / "library.db"
        _build_source_db(source_db, n=2)
        sprint_root = tmp_path / "sprint"
        _write_sprint_root(sprint_root, source_db)

        assert main(["--sprint-root", str(sprint_root)]) == 0
        snap = sprint_root / "library_snapshot.db"
        before = hashlib.sha256(snap.read_bytes()).hexdigest()

        capsys.readouterr()
        rc = main(["--sprint-root", str(sprint_root)])
        err = capsys.readouterr().err

        assert rc == 2
        assert "--force" in err
        assert hashlib.sha256(snap.read_bytes()).hexdigest() == before

        capsys.readouterr()
        assert main(["--sprint-root", str(sprint_root), "--force"]) == 0

    def test_exit_2_when_source_locked(self, tmp_path, capsys):
        source_db = tmp_path / "library.db"
        _build_source_db(source_db, n=2)
        sprint_root = tmp_path / "sprint"
        _write_sprint_root(sprint_root, source_db)

        blocker = sqlite3.connect(str(source_db))
        blocker.execute("PRAGMA locking_mode=EXCLUSIVE")
        blocker.execute("BEGIN EXCLUSIVE")
        try:
            rc = main(["--sprint-root", str(sprint_root)])
            err = capsys.readouterr().err
            assert rc == 2
            assert "请先关闭 GifAgent GUI 后重试" in err
        finally:
            blocker.rollback()
            blocker.close()

    def test_vacuum_into_raises_gui_busy_when_source_locked(self, tmp_path):
        source_db = tmp_path / "library.db"
        _build_source_db(source_db, n=1)
        ro_conn = open_source_readonly(source_db)
        blocker = sqlite3.connect(str(source_db))
        blocker.execute("PRAGMA locking_mode=EXCLUSIVE")
        blocker.execute("BEGIN EXCLUSIVE")
        try:
            with pytest.raises(GuiBusyError):
                vacuum_into(ro_conn, tmp_path / "snap.db")
        finally:
            blocker.rollback()
            blocker.close()
            ro_conn.close()

    def test_exit_2_when_manifest_sha_mismatch(self, tmp_path, capsys):
        source_db = tmp_path / "library.db"
        _build_source_db(source_db, n=1)
        sprint_root = tmp_path / "sprint"
        _write_sprint_root(sprint_root, source_db)
        canonical_path = sprint_root / "sprint_manifest.json"
        canonical = json.loads(canonical_path.read_text(encoding="utf-8"))
        canonical["control_plane"]["inputs_manifest.json"]["sha256"] = "0" * 64
        canonical_path.write_text(json.dumps(canonical, indent=2), encoding="utf-8")

        rc = main(["--sprint-root", str(sprint_root)])

        assert rc == 2
        assert "SHA-256" in capsys.readouterr().err

    def test_exit_2_when_source_missing(self, tmp_path, capsys):
        source_db = tmp_path / "does-not-exist.db"
        sprint_root = tmp_path / "sprint"
        _write_sprint_root(sprint_root, source_db)

        rc = main(["--sprint-root", str(sprint_root)])

        assert rc == 2
        assert "source_db_abs" in capsys.readouterr().err
