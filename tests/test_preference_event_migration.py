"""Phase 3 Task 1: Migration from legacy preference_events to append-only.

Verifies that the transactional table rebuild preserves data integrity,
adds the new ``event_kind`` / ``supersedes_event_id`` columns, maps
existing active rows to feedback events, and handles the new correction
semantics without mutating original rows.
"""

from __future__ import annotations

import sqlite3


# ── helpers ──────────────────────────────────────────────────────────────────


def _legacy_db() -> sqlite3.Connection:
    """Create an in-memory database with the *old* Phase 2 schema."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE candidate_gifs (
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
            status TEXT NOT NULL DEFAULT 'candidate'
                CHECK(status IN ('candidate','liked','disliked','neutral',
                                 'promoted','rejected','archived')),
            promoted_media_id INTEGER,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(source_run_id, source_run_candidate_id)
        );

        CREATE TABLE favorite_gifs (
            favorite_id TEXT PRIMARY KEY,
            candidate_id TEXT NOT NULL UNIQUE,
            full_path TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(candidate_id) REFERENCES candidate_gifs(candidate_id)
        );

        CREATE TABLE preference_events (
            event_id TEXT PRIMARY KEY,
            target_type TEXT NOT NULL
                CHECK(target_type IN ('media','candidate_gif')),
            target_id TEXT NOT NULL,
            rating TEXT NOT NULL
                CHECK(rating IN ('like','neutral','dislike','quality_reject','skip')),
            source_video_sha256 TEXT NOT NULL,
            scenario_keys_json TEXT NOT NULL DEFAULT '[]',
            corrected_tags_json TEXT,
            note TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            previous_status TEXT,
            undone_at TEXT,
            undone_reason TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_preference_events_target
            ON preference_events(target_type, target_id, created_at);

        INSERT INTO candidate_gifs
            (candidate_id, source_run_id, source_run_candidate_id,
             source_video_sha256, source_video_path, start_sec, end_sec, status)
        VALUES
            ('cand-1', 'run-1', 'rc-1', 'sha256-a', '/v/a.mp4', 0.0, 2.0, 'liked'),
            ('cand-2', 'run-1', 'rc-2', 'sha256-a', '/v/a.mp4', 2.0, 4.0, 'disliked'),
            ('cand-3', 'run-1', 'rc-3', 'sha256-a', '/v/a.mp4', 4.0, 6.0, 'candidate'),
            ('cand-4', 'run-2', 'rc-4', 'sha256-b', '/v/b.mp4', 0.0, 3.0, 'liked');

        INSERT INTO favorite_gifs
            (favorite_id, candidate_id, full_path, created_at)
        VALUES
            ('fav-1', 'cand-1', '/v/fav/cand-1.gif', CURRENT_TIMESTAMP);

        INSERT INTO preference_events
            (event_id, target_type, target_id, rating,
             source_video_sha256, scenario_keys_json, note,
             created_at, previous_status)
        VALUES
            ('evt-1', 'candidate_gif', 'cand-1', 'like',
             'sha256-a', '[]', NULL,
             '2026-01-01T00:00:00', 'candidate'),
            ('evt-2', 'candidate_gif', 'cand-2', 'dislike',
             'sha256-a', '[]', NULL,
             '2026-01-01T00:01:00', 'candidate'),
            ('evt-3', 'candidate_gif', 'cand-3', 'skip',
             'sha256-a', '[]', NULL,
             '2026-01-01T00:02:00', 'candidate'),
            ('evt-4', 'candidate_gif', 'cand-4', 'like',
             'sha256-b', '[]', 'favorite',
             '2026-01-01T00:03:00', 'candidate'),
            ('evt-5', 'candidate_gif', 'cand-1', 'dislike',
             'sha256-a', '[]', 'corrected',
             '2026-01-02T00:00:00', 'liked');
        """
    )
    conn.commit()
    return conn


def _count(conn: sqlite3.Connection, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# ── tests ────────────────────────────────────────────────────────────────────


def test_migration_preserves_row_counts_and_ids():
    """Transactional rebuild must keep every row with its original event_id."""
    conn = _legacy_db()
    old_count = _count(conn, "preference_events")
    old_ids = {
        r["event_id"]
        for r in conn.execute("SELECT event_id FROM preference_events").fetchall()
    }

    from app.services.preference_schema import apply_preference_schema

    apply_preference_schema(conn)

    new_count = _count(conn, "preference_events")
    new_ids = {
        r["event_id"]
        for r in conn.execute("SELECT event_id FROM preference_events").fetchall()
    }

    assert new_count == old_count, f"Row count changed: {old_count} -> {new_count}"
    assert new_ids == old_ids, "event_id set changed after migration"


def test_migration_adds_new_columns():
    """event_kind and supersedes_event_id must exist after migration."""
    conn = _legacy_db()

    from app.services.preference_schema import apply_preference_schema

    apply_preference_schema(conn)

    cols = {
        r[1]
        for r in conn.execute("PRAGMA table_info(preference_events)").fetchall()
    }
    assert "event_kind" in cols
    assert "supersedes_event_id" in cols


def test_migration_sets_event_kind_to_feedback():
    """Pre-existing rows must have event_kind='feedback'."""
    conn = _legacy_db()

    from app.services.preference_schema import apply_preference_schema

    apply_preference_schema(conn)

    kinds = {
        r["event_kind"]
        for r in conn.execute(
            "SELECT DISTINCT event_kind FROM preference_events"
        ).fetchall()
    }
    assert kinds == {"feedback"}, f"Unexpected event_kind values: {kinds}"


def test_migration_sets_supersedes_event_id_null():
    """Pre-existing rows must have supersedes_event_id IS NULL."""
    conn = _legacy_db()

    from app.services.preference_schema import apply_preference_schema

    apply_preference_schema(conn)

    null_count = conn.execute(
        "SELECT COUNT(*) FROM preference_events WHERE supersedes_event_id IS NULL"
    ).fetchone()[0]
    total = _count(conn, "preference_events")
    assert null_count == total


def test_migration_preserves_favorite_gifs():
    """The separate favorite_gifs table must be untouched by migration."""
    conn = _legacy_db()

    from app.services.preference_schema import apply_preference_schema

    apply_preference_schema(conn)

    assert _count(conn, "favorite_gifs") == 1
    row = conn.execute(
        "SELECT candidate_id FROM favorite_gifs WHERE favorite_id=?",
        ("fav-1",),
    ).fetchone()
    assert row is not None
    assert row["candidate_id"] == "cand-1"


def test_migration_idempotent():
    """Running apply_preference_schema twice must not double rows or error."""
    conn = _legacy_db()
    old_count = _count(conn, "preference_events")

    from app.services.preference_schema import apply_preference_schema

    apply_preference_schema(conn)
    apply_preference_schema(conn)

    assert _count(conn, "preference_events") == old_count


def test_correction_does_not_mutate_original():
    """correct_feedback must leave the original row unchanged."""
    conn = _legacy_db()

    from app.services.preference_schema import apply_preference_schema

    apply_preference_schema(conn)

    from app.services.preference_events import PreferenceEventService

    svc = PreferenceEventService(conn)

    original = conn.execute(
        "SELECT rating FROM preference_events WHERE event_id=?", ("evt-1",)
    ).fetchone()
    assert original["rating"] == "like"

    correction = svc.correct_feedback(
        event_id="evt-1", replacement="dislike", reason="fat-finger"
    )

    original_after = conn.execute(
        "SELECT rating FROM preference_events WHERE event_id=?", ("evt-1",)
    ).fetchone()
    assert original_after["rating"] == "like", "Original row was mutated!"

    assert correction.event_kind == "correction"
    assert correction.supersedes_event_id == "evt-1"
    assert correction.rating == "dislike"


def test_effective_feedback_resolves_correction():
    """effective_feedback must return the correction value, not the original."""
    conn = _legacy_db()

    from app.services.preference_schema import apply_preference_schema

    apply_preference_schema(conn)

    from app.services.preference_events import PreferenceEventService

    svc = PreferenceEventService(conn)

    evt = svc.correct_feedback(
        event_id="evt-1", replacement="dislike", reason="fat-finger"
    )

    effective = svc.effective_feedback()

    # evt-1 (like) should NOT appear; evt-1 is superseded.
    # The correction event should appear instead.
    correction_results = [e for e in effective if e.supersedes_event_id == "evt-1"]
    assert len(correction_results) == 1
    assert correction_results[0].rating == "dislike"

    # The original "evt-1" should NOT be in effective results.
    original_in_effective = [e for e in effective if e.event_id == "evt-1"]
    assert len(original_in_effective) == 0


def test_effective_feedback_before_filter():
    """effective_feedback(before=...) must only return events before a cut-off."""
    conn = _legacy_db()

    from app.services.preference_schema import apply_preference_schema

    apply_preference_schema(conn)

    from app.services.preference_events import PreferenceEventService

    svc = PreferenceEventService(conn)

    svc.correct_feedback(
        event_id="evt-1", replacement="dislike", reason="fat-finger"
    )

    before_all = svc.effective_feedback()
    before_cutoff = svc.effective_feedback(before="2026-01-02T00:00:00")

    assert len(before_cutoff) < len(before_all)


def test_latest_effective_ratings_excludes_superseded():
    """latest_effective_ratings must not return superseded events."""
    conn = _legacy_db()

    from app.services.preference_schema import apply_preference_schema

    apply_preference_schema(conn)

    from app.services.preference_events import PreferenceEventService

    svc = PreferenceEventService(conn)

    # evt-5 (cand-1, dislike) is the most recent non-superseded event for
    # cand-1.  After we supersede evt-1 (like, cand-1), evt-5 should still
    # be the effective rating for cand-1 because it came *after* evt-1.
    svc.correct_feedback(
        event_id="evt-1", replacement="dislike", reason="fat-finger"
    )

    latest = svc.latest_effective_ratings()

    # cand-1's effective rating is from evt-5 (dislike), not the correction
    # (also dislike) — both are "dislike" so this test is about which event_id.
    cand1 = latest["candidate_gif:cand-1"]
    assert cand1.rating == "dislike"
    # evt-1 should not appear anywhere in latest.
    for key, evt in latest.items():
        assert evt.event_id != "evt-1"


def test_skip_status_unchanged():
    """Skip feedback must NOT change candidate_gifs.status after migration."""
    conn = _legacy_db()

    from app.services.preference_schema import apply_preference_schema

    apply_preference_schema(conn)

    status = conn.execute(
        "SELECT status FROM candidate_gifs WHERE candidate_id=?", ("cand-3",)
    ).fetchone()["status"]
    assert status == "candidate"
