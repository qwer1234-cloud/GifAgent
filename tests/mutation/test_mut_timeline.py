"""W4 kill tests for timeline mutation survivors.

Targets survivors from the cosmic-ray run recorded in
``<sprint-root>/w4/mutation_scores/cosmic-ray/timeline.json``:

- ``_parse_score_json`` / ``_extract_label_from_vlm`` TypeError nets
  (``except ... TypeError`` replaced by cosmic-ray's sentinel class).
- caption/summary fallback chain (``or`` vs ``and``), caption type check,
  ``not``-guard inversion, truncation threshold (60) and slice width (57).
- thumbnail budget accounting: first span(s) within ``max_thumbnails`` get a
  path, later ones None (``> 0`` guard and ``-= 1`` decrement).
- ``_scene_label`` / ``_candidate_label`` NULL-status defaults and the
  vlm-label short-circuit.

Equivalent mutants (keyword-only marker, 23 annotation unions) are
registered in w4-surviving.md.
"""

from __future__ import annotations

import sqlite3

from app.services.timeline import (
    _candidate_label,
    _extract_label_from_vlm,
    _parse_score_json,
    _scene_label,
    load_timeline_window,
)


# ── _parse_score_json ──────────────────────────────────────────────────────


def test_parse_score_json_handles_junk_and_non_string_inputs():
    assert _parse_score_json("") == {}
    assert _parse_score_json(None) == {}
    assert _parse_score_json("not json") == {}
    # A non-string non-None input makes json.loads raise TypeError; the
    # except-clause must include TypeError.
    assert _parse_score_json(12345) == {}


# ── _extract_label_from_vlm ────────────────────────────────────────────────


def test_extract_label_returns_empty_for_missing_or_junk_input():
    assert _extract_label_from_vlm(None) == ""
    assert _extract_label_from_vlm("") == ""
    assert _extract_label_from_vlm("not json") == ""
    assert _extract_label_from_vlm(12345) == ""


def test_extract_label_prefers_caption_then_summary():
    assert _extract_label_from_vlm('{"caption": "Hello"}') == "Hello"
    assert _extract_label_from_vlm('{"summary": "Sum"}') == "Sum"


def test_extract_label_rejects_non_string_caption():
    assert _extract_label_from_vlm('{"caption": 123}') == ""
    assert _extract_label_from_vlm('{"caption": "   "}') == ""


def test_extract_label_truncates_long_captions_with_ellipsis():
    long_text = "x" * 61
    label = _extract_label_from_vlm('{"caption": "%s"}' % long_text)
    assert label == "x" * 57 + "..."
    assert len(label) == 60


def test_extract_label_keeps_captions_at_or_below_sixty_chars():
    at_limit = "y" * 60
    assert _extract_label_from_vlm('{"caption": "%s"}' % at_limit) == at_limit
    under_limit = "z" * 59
    assert _extract_label_from_vlm('{"caption": "%s"}' % under_limit) == under_limit


# ── label helpers ──────────────────────────────────────────────────────────


def test_scene_label_defaults_null_status_and_uses_score_fields():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE video_clips (clip_id TEXT, status TEXT, score_json TEXT)")
    row = conn.execute("SELECT 'c1' AS clip_id, NULL AS status, NULL AS score_json").fetchone()
    assert _scene_label(row, {}) == "Scene candidate"
    row2 = conn.execute("SELECT 'c1' AS clip_id, 'exported' AS status, NULL AS score_json").fetchone()
    assert _scene_label(row2, {"label": "kitchen chase"}) == "kitchen chase"
    row3 = conn.execute("SELECT 'c1' AS clip_id, 'exported' AS status, NULL AS score_json").fetchone()
    assert _scene_label(row3, {"scene_type": "action"}) == "action"


def test_candidate_label_prefers_vlm_label_and_defaults_null_status():
    assert (
        _candidate_label({"vlm_summary_json": '{"caption": "Neon alley"}', "status": None})
        == "Neon alley"
    )
    assert _candidate_label({"vlm_summary_json": None, "status": None}) == "Candidate candidate"
    assert _candidate_label({"vlm_summary_json": None, "status": "liked"}) == "Candidate liked"


# ── load_timeline_window thumbnail budget ──────────────────────────────────


def _setup_db() -> sqlite3.Connection:
    from app.services.preference_schema import apply_preference_schema

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS media (
            media_id TEXT PRIMARY KEY,
            file_path TEXT NOT NULL,
            media_type TEXT NOT NULL DEFAULT 'video',
            sha256 TEXT UNIQUE,
            duration REAL,
            created_at TEXT NOT NULL,
            indexed_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS video_clips (
            clip_id TEXT PRIMARY KEY,
            video_id TEXT NOT NULL,
            start REAL NOT NULL,
            end REAL NOT NULL,
            duration REAL NOT NULL,
            score_json TEXT,
            status TEXT DEFAULT 'candidate',
            exported_path TEXT,
            created_at TEXT NOT NULL
        );
        """
    )
    apply_preference_schema(conn)
    conn.execute(
        "INSERT INTO media (media_id, file_path, sha256, duration, created_at, indexed_at)"
        " VALUES ('v1', '/videos/a.mp4', 'sha_v1', 600.0, 't', 't')"
    )
    return conn


def _insert_clip(conn, clip_id, *, exported_path):
    conn.execute(
        "INSERT INTO video_clips (clip_id, video_id, start, end, duration, status, exported_path, created_at)"
        " VALUES (?, 'v1', 0.0, 10.0, 10.0, 'candidate', ?, 't')",
        (clip_id, exported_path),
    )


def _insert_candidate(conn, candidate_id, *, preview_path, status="candidate"):
    conn.execute(
        "INSERT INTO candidate_gifs (candidate_id, source_run_id, source_run_candidate_id,"
        " source_video_sha256, source_video_path, start_sec, end_sec, artifact_path,"
        " preview_path, status, created_at, updated_at)"
        " VALUES (?, 'run', ?, 'sha_v1', '/videos/a.mp4', 0.0, 5.0, '/a.gif', ?, ?, 't', 't')",
        (candidate_id, candidate_id, preview_path, status),
    )


def test_scene_thumbnail_budget_counts_down_and_stops():
    conn = _setup_db()
    _insert_clip(conn, "s1", exported_path="/e/s1.gif")
    _insert_clip(conn, "s2", exported_path="/e/s2.gif")
    _insert_clip(conn, "s3", exported_path="/e/s3.gif")
    window = load_timeline_window(conn, video_id="v1", start_sec=0.0, end_sec=100.0, max_thumbnails=2)
    assert [s.thumbnail_path for s in window.scenes] == ["/e/s1.gif", "/e/s2.gif", None]


def test_candidate_thumbnail_budget_counts_down_and_stops():
    conn = _setup_db()
    _insert_candidate(conn, "c1", preview_path="/p/c1.jpg")
    _insert_candidate(conn, "c2", preview_path="/p/c2.jpg")
    _insert_candidate(conn, "c3", preview_path="/p/c3.jpg")
    window = load_timeline_window(conn, video_id="v1", start_sec=0.0, end_sec=100.0, max_thumbnails=2)
    assert [c.thumbnail_path for c in window.candidates] == ["/p/c1.jpg", "/p/c2.jpg", None]
