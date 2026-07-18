"""Tests for Phase 4 Task 2: Attention Inbox read model.

Covers
------
- Empty inbox (no items from any source)
- All five ``AttentionKind`` values present
- Deterministic ordering by severity (error > warning > info) then time
- Stable, deterministic attention IDs
- No routine success entries included
- No cross-database transaction (each DB opened independently)
- Degraded-source warnings when one database is locked/unavailable
- ``limit`` parameter respected
- FastAPI endpoint response structure
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest


# ===================================================================
# In-memory schema helpers
# ===================================================================


def _create_task_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS task_jobs (
            job_id TEXT PRIMARY KEY,
            directory TEXT NOT NULL,
            directory_key TEXT NOT NULL,
            config_json TEXT NOT NULL DEFAULT '{}',
            job_limit INTEGER NOT NULL DEFAULT 0,
            extensions TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS task_videos (
            video_id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL REFERENCES task_jobs(job_id),
            path TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(job_id, path)
        );
        CREATE TABLE IF NOT EXISTS task_stages (
            stage_id TEXT PRIMARY KEY,
            video_id TEXT NOT NULL REFERENCES task_videos(video_id),
            stage_name TEXT NOT NULL,
            clip_id TEXT,
            input_key TEXT NOT NULL,
            output_key TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            attempt_count INTEGER NOT NULL DEFAULT 0,
            lease_owner TEXT,
            lease_expires_at TEXT,
            retry_at TEXT,
            last_error_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    conn.commit()


def _create_library_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS media (
            media_id TEXT PRIMARY KEY,
            file_path TEXT NOT NULL,
            media_type TEXT NOT NULL DEFAULT 'gif',
            film TEXT,
            sha256 TEXT,
            phash TEXT,
            width INTEGER,
            height INTEGER,
            duration REAL,
            frame_count INTEGER,
            cluster_id TEXT,
            is_representative INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            indexed_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS candidate_gifs (
            candidate_id TEXT PRIMARY KEY,
            source_run_id TEXT NOT NULL,
            source_run_candidate_id TEXT NOT NULL,
            source_video_sha256 TEXT NOT NULL,
            source_video_path TEXT NOT NULL,
            start_sec REAL NOT NULL,
            end_sec REAL NOT NULL,
            artifact_path TEXT,
            preview_path TEXT,
            vlm_summary_json TEXT NOT NULL DEFAULT '{}',
            tags_json TEXT NOT NULL DEFAULT '[]',
            scenario_keys_json TEXT NOT NULL DEFAULT '[]',
            base_rag_similarity REAL,
            profile_score REAL,
            final_score REAL,
            score_profile_version TEXT,
            status TEXT NOT NULL DEFAULT 'candidate',
            promoted_media_id INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(source_run_id, source_run_candidate_id)
        );
        CREATE TABLE IF NOT EXISTS preference_profile_publications (
            publication_id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_version TEXT NOT NULL,
            previous_profile_version TEXT,
            published_at TEXT NOT NULL,
            config_json TEXT NOT NULL DEFAULT '{}'
        );
        """
    )
    conn.commit()


def _create_quality_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS champion_history (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            config_id TEXT NOT NULL,
            action TEXT NOT NULL CHECK(action IN ('promote','rollback')),
            previous_config_id TEXT,
            scorecard_json TEXT,
            created_at TEXT NOT NULL
        );
        """
    )
    conn.commit()


# ===================================================================
# Fixtures — in-memory databases seeded with test data
# ===================================================================


@pytest.fixture
def task_db():
    """In-memory task database with schema."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _create_task_schema(conn)
    return conn


@pytest.fixture
def library_db():
    """In-memory library database with schema (media + preference tables)."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _create_library_schema(conn)
    return conn


@pytest.fixture
def quality_db():
    """In-memory quality-lab database with schema."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _create_quality_schema(conn)
    return conn


class MockTaskRepo:
    """Minimal TaskRepository stand-in wrapping an in-memory connection."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn


# ===================================================================
# Service-level tests (unit tests against list_attention_items)
# ===================================================================


class TestEmptyInbox:
    """No items in any source yields an empty list."""

    def test_no_items_returns_empty(self, task_db, library_db, quality_db):
        from app.services.attention import list_attention_items

        repo = MockTaskRepo(task_db)
        items = list_attention_items(
            task_repo=repo,
            library_conn=library_db,
            quality_conn=quality_db,
            limit=100,
        )
        assert items == []

    def test_none_connections_returns_empty(self):
        from app.services.attention import list_attention_items

        items = list_attention_items(
            task_repo=None,
            library_conn=None,
            quality_conn=None,
            limit=100,
        )
        assert items == []


class TestAllFiveKinds:
    """One item from each AttentionKind appears in the result."""

    _TS = "2026-07-18T12:00:00+00:00"

    def seed_all(self, task_db, library_db, quality_db):
        # Task failure (stage-level)
        task_db.execute(
            "INSERT INTO task_jobs (job_id, directory, directory_key, status, created_at, updated_at) "
            "VALUES ('job-1', '/media/test', 'test', 'needs_attention', ?, ?)",
            (self._TS, self._TS),
        )
        task_db.execute(
            "INSERT INTO task_videos (video_id, job_id, path, fingerprint, status, created_at, updated_at) "
            "VALUES ('vid-1', 'job-1', '/media/test/a.mp4', 'fp1', 'running', ?, ?)",
            (self._TS, self._TS),
        )
        task_db.execute(
            "INSERT INTO task_stages (stage_id, video_id, stage_name, input_key, status, "
            "last_error_json, created_at, updated_at) "
            "VALUES ('stage-1', 'vid-1', 'extract', 'inp', 'needs_attention', "
            "'{\"message\":\"timeout\"}', ?, ?)",
            (self._TS, self._TS),
        )
        task_db.commit()

        # Migration conflict
        library_db.execute(
            "INSERT INTO media (media_id, file_path, media_type, sha256, created_at, indexed_at) "
            "VALUES ('m1', '/a.gif', 'gif', 'abc123', ?, ?)",
            (self._TS, self._TS),
        )
        library_db.execute(
            "INSERT INTO media (media_id, file_path, media_type, sha256, created_at, indexed_at) "
            "VALUES ('m2', '/b.gif', 'gif', 'abc123', ?, ?)",
            (self._TS, self._TS),
        )
        library_db.commit()

        # Profile publish
        library_db.execute(
            "INSERT INTO preference_profile_publications "
            "(publication_id, profile_version, previous_profile_version, published_at, config_json) "
            "VALUES (1, 'v2', 'v1', ?, '{}')",
            (self._TS,),
        )
        library_db.commit()

        # High-value review
        library_db.execute(
            "INSERT INTO candidate_gifs "
            "(candidate_id, source_run_id, source_run_candidate_id, source_video_sha256, "
            "source_video_path, start_sec, end_sec, final_score, status, created_at, updated_at) "
            "VALUES ('cand-high1', 'run-1', 'clip-1', 'sha1', '/path/to/video.mp4', "
            "0.0, 5.0, 0.95, 'candidate', ?, ?)",
            (self._TS, self._TS),
        )
        library_db.commit()

        # Champion promotion
        quality_db.execute(
            "INSERT INTO champion_history (config_id, action, created_at) "
            "VALUES ('cfg-best', 'promote', ?)",
            (self._TS,),
        )
        quality_db.commit()

    def test_all_five_present(self, task_db, library_db, quality_db):
        from app.services.attention import list_attention_items

        self.seed_all(task_db, library_db, quality_db)
        repo = MockTaskRepo(task_db)
        items = list_attention_items(
            task_repo=repo,
            library_conn=library_db,
            quality_conn=quality_db,
            limit=100,
        )

        kinds_found = {item.kind for item in items}
        assert kinds_found == {
            "task_failure",
            "migration_conflict",
            "profile_publish",
            "high_value_review",
            "champion_promotion",
        }, f"Missing kinds: {kinds_found}"


class TestOrdering:
    """Items sorted by severity (error first), then newest first."""

    def _ts(self, offset_hours: int) -> str:
        dt = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc) + timedelta(
            hours=offset_hours
        )
        return dt.isoformat()

    def test_errors_before_warnings_before_info(self, task_db, library_db, quality_db):
        """Severity ordering takes priority over time ordering."""
        from app.services.attention import list_attention_items

        old = self._ts(-10)  # 10 hours ago
        recent = self._ts(0)  # now

        # Recent info item (high-value review)
        library_db.execute(
            "INSERT INTO candidate_gifs "
            "(candidate_id, source_run_id, source_run_candidate_id, source_video_sha256, "
            "source_video_path, start_sec, end_sec, final_score, status, created_at, updated_at) "
            "VALUES ('cand-info', 'run-1', 'clip-1', 'sha1', '/p.mp4', "
            "0.0, 1.0, 0.95, 'candidate', ?, ?)",
            (recent, recent),
        )
        library_db.commit()

        # Old error item (task job failure)
        task_db.execute(
            "INSERT INTO task_jobs (job_id, directory, directory_key, status, created_at, updated_at) "
            "VALUES ('job-error', '/old', 'old', 'needs_attention', ?, ?)",
            (old, old),
        )
        task_db.commit()

        repo = MockTaskRepo(task_db)
        items = list_attention_items(
            task_repo=repo,
            library_conn=library_db,
            quality_conn=quality_db,
            limit=100,
        )

        assert len(items) >= 2
        error_idx = next(i for i, x in enumerate(items) if x.kind == "task_failure")
        info_idx = next(
            i for i, x in enumerate(items) if x.kind == "high_value_review"
        )
        assert error_idx < info_idx, (
            f"Error (idx={error_idx}) should sort before info (idx={info_idx})"
        )

    def test_within_same_severity_newest_first(self, task_db, library_db, quality_db):
        """Within same severity level, newer items appear first."""
        from app.services.attention import list_attention_items

        old = self._ts(-5)
        recent = self._ts(0)

        # Two champion promotions (both info severity)
        quality_db.execute(
            "INSERT INTO champion_history (config_id, action, created_at) "
            "VALUES ('cfg-old', 'promote', ?)",
            (old,),
        )
        quality_db.execute(
            "INSERT INTO champion_history (config_id, action, created_at) "
            "VALUES ('cfg-recent', 'promote', ?)",
            (recent,),
        )
        quality_db.commit()

        repo = MockTaskRepo(task_db)
        items = list_attention_items(
            task_repo=repo,
            library_conn=library_db,
            quality_conn=quality_db,
            limit=100,
        )

        champ_items = [x for x in items if x.kind == "champion_promotion"]
        assert len(champ_items) == 2
        assert champ_items[0].created_at == recent
        assert champ_items[1].created_at == old


class TestStableIDs:
    """Attention IDs are deterministic and stable."""

    def test_stable_id_across_calls(self, task_db, library_db, quality_db):
        from app.services.attention import list_attention_items

        task_db.execute(
            "INSERT INTO task_jobs (job_id, directory, directory_key, status, created_at, updated_at) "
            "VALUES ('job-1', '/d', 'd', 'needs_attention', '2026-07-18T12:00:00+00:00', "
            "'2026-07-18T12:00:00+00:00')",
        )
        task_db.commit()

        repo = MockTaskRepo(task_db)
        items1 = list_attention_items(
            task_repo=repo,
            library_conn=library_db,
            quality_conn=quality_db,
            limit=100,
        )
        items2 = list_attention_items(
            task_repo=repo,
            library_conn=library_db,
            quality_conn=quality_db,
            limit=100,
        )

        assert len(items1) == 1
        assert items1[0].attention_id == items2[0].attention_id

    def test_stable_id_format(self):
        """IDs match expected pattern."""
        from app.services.attention import _stable_id

        result = _stable_id("task_failure", "job:abc-123")
        assert result.startswith("att_")
        assert len(result) == 4 + 16  # "att_" + 16 hex chars = 20
        hex_part = result[4:]
        assert all(c in "0123456789abcdef" for c in hex_part)


class TestNoRoutineSuccess:
    """Routine completed/succeeded items do not generate attention entries."""

    def test_succeeded_jobs_ignored(self, task_db, library_db, quality_db):
        from app.services.attention import list_attention_items

        task_db.execute(
            "INSERT INTO task_jobs (job_id, directory, directory_key, status, created_at, updated_at) "
            "VALUES ('job-ok', '/ok', 'ok', 'succeeded', '2026-07-18T12:00:00+00:00', "
            "'2026-07-18T12:00:00+00:00')",
        )
        task_db.commit()

        repo = MockTaskRepo(task_db)
        items = list_attention_items(
            task_repo=repo,
            library_conn=library_db,
            quality_conn=quality_db,
            limit=100,
        )
        task_items = [x for x in items if x.kind == "task_failure"]
        assert len(task_items) == 0

    def test_completed_stages_ignored(self, task_db, library_db, quality_db):
        from app.services.attention import list_attention_items

        ts = "2026-07-18T12:00:00+00:00"
        task_db.execute(
            "INSERT INTO task_jobs (job_id, directory, directory_key, status, created_at, updated_at) "
            "VALUES ('job-1', '/d', 'd', 'running', ?, ?)", (ts, ts))
        task_db.execute(
            "INSERT INTO task_videos (video_id, job_id, path, fingerprint, status, created_at, updated_at) "
            "VALUES ('vid-1', 'job-1', '/d/a.mp4', 'fp', 'running', ?, ?)", (ts, ts))
        task_db.execute(
            "INSERT INTO task_stages (stage_id, video_id, stage_name, input_key, status, "
            "created_at, updated_at) "
            "VALUES ('s1', 'vid-1', 'extract', 'inp', 'succeeded', ?, ?)", (ts, ts))
        task_db.commit()

        repo = MockTaskRepo(task_db)
        items = list_attention_items(
            task_repo=repo,
            library_conn=library_db,
            quality_conn=quality_db,
            limit=100,
        )
        task_items = [x for x in items if x.kind == "task_failure"]
        assert len(task_items) == 0

    def test_low_score_candidates_ignored(self, library_db):
        from app.services.attention import list_attention_items

        ts = "2026-07-18T12:00:00+00:00"
        library_db.execute(
            "INSERT INTO candidate_gifs "
            "(candidate_id, source_run_id, source_run_candidate_id, source_video_sha256, "
            "source_video_path, start_sec, end_sec, final_score, status, created_at, updated_at) "
            "VALUES ('cand-low', 'run-1', 'clip-1', 'sha1', '/p.mp4', "
            "0.0, 1.0, 0.3, 'candidate', ?, ?)", (ts, ts))
        library_db.commit()

        items = list_attention_items(
            task_repo=None,
            library_conn=library_db,
            quality_conn=None,
            limit=100,
        )
        review_items = [x for x in items if x.kind == "high_value_review"]
        assert len(review_items) == 0


class TestDegradedSources:
    """When one database is unavailable, partial results are returned."""

    def test_task_db_unavailable_still_returns_other_sources(
        self, library_db, quality_db
    ):
        from app.services.attention import list_attention_items

        ts = "2026-07-18T12:00:00+00:00"
        library_db.execute(
            "INSERT INTO media (media_id, file_path, media_type, sha256, created_at, indexed_at) "
            "VALUES ('m1', '/a.gif', 'gif', 'dup123', ?, ?)", (ts, ts))
        library_db.execute(
            "INSERT INTO media (media_id, file_path, media_type, sha256, created_at, indexed_at) "
            "VALUES ('m2', '/b.gif', 'gif', 'dup123', ?, ?)", (ts, ts))
        library_db.commit()

        items = list_attention_items(
            task_repo=None,
            library_conn=library_db,
            quality_conn=quality_db,
            limit=100,
        )
        kinds = {x.kind for x in items}
        assert "migration_conflict" in kinds
        assert "task_failure" not in kinds

    def test_library_db_unavailable_still_returns_other_sources(
        self, task_db, quality_db
    ):
        from app.services.attention import list_attention_items

        ts = "2026-07-18T12:00:00+00:00"
        task_db.execute(
            "INSERT INTO task_jobs (job_id, directory, directory_key, status, created_at, updated_at) "
            "VALUES ('job-1', '/d', 'd', 'needs_attention', ?, ?)", (ts, ts))
        task_db.commit()

        items = list_attention_items(
            task_repo=MockTaskRepo(task_db),
            library_conn=None,
            quality_conn=quality_db,
            limit=100,
        )
        kinds = {x.kind for x in items}
        assert "task_failure" in kinds
        assert "migration_conflict" not in kinds
        assert "profile_publish" not in kinds
        assert "high_value_review" not in kinds

    def test_quality_db_unavailable_still_returns_other_sources(
        self, task_db, library_db
    ):
        from app.services.attention import list_attention_items

        ts = "2026-07-18T12:00:00+00:00"
        task_db.execute(
            "INSERT INTO task_jobs (job_id, directory, directory_key, status, created_at, updated_at) "
            "VALUES ('job-1', '/d', 'd', 'needs_attention', ?, ?)", (ts, ts))
        task_db.commit()

        items = list_attention_items(
            task_repo=MockTaskRepo(task_db),
            library_conn=library_db,
            quality_conn=None,
            limit=100,
        )
        kinds = {x.kind for x in items}
        assert "task_failure" in kinds
        assert "champion_promotion" not in kinds


class TestLimit:
    """The limit parameter caps the number of returned items."""

    def test_limit_respected(self, task_db, library_db, quality_db):
        from app.services.attention import list_attention_items

        ts = "2026-07-18T12:00:00+00:00"
        for i in range(5):
            task_db.execute(
                "INSERT INTO task_jobs (job_id, directory, directory_key, status, created_at, updated_at) "
                "VALUES (?, '/d', 'd', 'needs_attention', ?, ?)",
                (f"job-{i}", ts, ts),
            )
        task_db.commit()

        repo = MockTaskRepo(task_db)
        items = list_attention_items(
            task_repo=repo,
            library_conn=library_db,
            quality_conn=quality_db,
            limit=2,
        )
        assert len(items) <= 2

    def test_limit_default_is_100(self):
        from app.services.attention import list_attention_items
        import inspect

        sig = inspect.signature(list_attention_items)
        default = sig.parameters["limit"].default
        assert default == 100


class TestNoCrossDBTransaction:
    """Each database is queried independently — no cross-DB transaction."""

    def test_separate_connections(self):
        """The function signature separates the three DB parameters."""
        from app.services.attention import list_attention_items

        import inspect

        sig = inspect.signature(list_attention_items)
        params = sig.parameters
        assert "task_repo" in params
        assert "library_conn" in params
        assert "quality_conn" in params


# ===================================================================
# API-level integration tests
# Tests the router's get_attention function directly with patched
# database connections (since TestClient is broken in this env due
# to starlette/httpx version incompatibility).
# ===================================================================


class TestAttentionRouter:
    """Tests the ``get_attention`` endpoint function directly."""

    _TS = "2026-07-18T12:00:00+00:00"

    @pytest.fixture(autouse=True)
    def _setup(self):
        """Create in-memory databases and patch connection functions."""
        self._task_conn = sqlite3.connect(":memory:")
        self._task_conn.row_factory = sqlite3.Row
        _create_task_schema(self._task_conn)

        self._library_conn = sqlite3.connect(":memory:")
        self._library_conn.row_factory = sqlite3.Row
        _create_library_schema(self._library_conn)

        self._quality_conn = sqlite3.connect(":memory:")
        self._quality_conn.row_factory = sqlite3.Row
        _create_quality_schema(self._quality_conn)

        # Seed each source
        self._task_conn.execute(
            "INSERT INTO task_jobs (job_id, directory, directory_key, status, created_at, updated_at) "
            "VALUES ('job-attn', '/test', 'test', 'needs_attention', ?, ?)",
            (self._TS, self._TS),
        )
        self._task_conn.commit()

        self._library_conn.execute(
            "INSERT INTO media (media_id, file_path, media_type, sha256, created_at, indexed_at) "
            "VALUES ('m1', '/a.gif', 'gif', 'dup456', ?, ?)",
            (self._TS, self._TS),
        )
        self._library_conn.execute(
            "INSERT INTO media (media_id, file_path, media_type, sha256, created_at, indexed_at) "
            "VALUES ('m2', '/b.gif', 'gif', 'dup456', ?, ?)",
            (self._TS, self._TS),
        )
        self._library_conn.commit()

        self._quality_conn.execute(
            "INSERT INTO champion_history (config_id, action, created_at) "
            "VALUES ('cfg-best', 'promote', ?)",
            (self._TS,),
        )
        self._quality_conn.commit()

        self._patches = [
            patch("app.routers.workbench.connect_task_db", return_value=self._task_conn),
            patch("app.routers.workbench.get_library_conn", return_value=self._library_conn),
            patch("app.routers.workbench.connect_quality_db", return_value=self._quality_conn),
        ]
        for p in self._patches:
            p.start()
        yield
        for p in self._patches:
            p.stop()

    def test_get_attention_returns_response(self):
        """Router returns AttentionResponse with items and source_warnings."""
        from app.routers.workbench import get_attention

        result = get_attention(limit=100)
        assert hasattr(result, "items")
        assert hasattr(result, "source_warnings")
        assert isinstance(result.items, list)
        assert isinstance(result.source_warnings, list)
        assert len(result.items) >= 1

    def test_get_attention_respects_limit(self):
        """Limit parameter works through the router."""
        from app.routers.workbench import get_attention

        result = get_attention(limit=1)
        assert len(result.items) <= 1

    def test_get_attention_items_have_required_fields(self):
        """Each item returned via the router has all required attributes."""
        from app.routers.workbench import get_attention

        result = get_attention(limit=100)
        for item in result.items:
            assert item.attention_id
            assert item.kind
            assert item.severity
            assert item.title
            assert item.detail
            assert item.action_label
            assert item.action_target
            assert item.created_at

    def test_degraded_task_db_returns_warning(self):
        """When task_db is locked, router returns warning and other items."""
        from app.routers.workbench import get_attention

        with patch(
            "app.routers.workbench.connect_task_db",
            side_effect=sqlite3.OperationalError("database is locked"),
        ):
            result = get_attention(limit=100)
            warnings = result.source_warnings
            assert any("task_db" in w for w in warnings)
            assert len(result.items) >= 1

    def test_all_sources_degraded_returns_empty(self):
        """When all three DBs are locked, router returns empty items + 3 warnings."""
        from app.routers.workbench import get_attention

        with patch(
            "app.routers.workbench.connect_task_db",
            side_effect=sqlite3.OperationalError("locked"),
        ), patch(
            "app.routers.workbench.get_library_conn",
            side_effect=sqlite3.OperationalError("locked"),
        ), patch(
            "app.routers.workbench.connect_quality_db",
            side_effect=sqlite3.OperationalError("locked"),
        ):
            result = get_attention(limit=100)
            assert result.items == []
            assert len(result.source_warnings) == 3
