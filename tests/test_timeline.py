"""Tests for Phase 4 Task 4: Moment Timeline and PotPlayer jump targets.

Covers
------
- TimelineSpan / TimelineWindow dataclass contracts (frozen, field types).
- load_timeline_window returns only overlapping spans within the window.
- At most ``max_thumbnails`` (default 60) thumbnails are populated.
- Spans carry base_score and preference_score when available.
- Missing source paths still produce a valid TimelineWindow (no crash).
- potplayer_target quotes spaces without invoking a shell.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from app.services.timeline import (
    TimelineSpan,
    TimelineWindow,
    load_timeline_window,
    potplayer_target,
)


# ── helpers ──────────────────────────────────────────────────────────────────


def _conn() -> sqlite3.Connection:
    """In-memory library DB with media + video_clips + candidate_gifs."""
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
    return conn


def _insert_media(
    conn: sqlite3.Connection,
    media_id: str,
    *,
    file_path: str = "/videos/sample.mp4",
    sha256: str = "abc123",
    duration: float = 120.0,
) -> None:
    conn.execute(
        """INSERT INTO media (media_id, file_path, media_type, sha256, duration, created_at, indexed_at)
           VALUES (?, ?, 'video', ?, ?, '2026-07-18T00:00:00+00:00', '2026-07-18T00:00:00+00:00')""",
        (media_id, file_path, sha256, duration),
    )
    conn.commit()


def _insert_clip(
    conn: sqlite3.Connection,
    clip_id: str,
    video_id: str,
    *,
    start: float = 0.0,
    end: float = 10.0,
    score_json: str | None = None,
    status: str = "candidate",
    exported_path: str | None = None,
) -> None:
    conn.execute(
        """INSERT INTO video_clips (clip_id, video_id, start, end, duration, score_json, status, exported_path, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, '2026-07-18T00:00:00+00:00')""",
        (clip_id, video_id, start, end, end - start, score_json, status, exported_path),
    )
    conn.commit()


def _insert_candidate(
    conn: sqlite3.Connection,
    candidate_id: str,
    *,
    source_video_sha256: str = "abc123",
    source_video_path: str = "/videos/sample.mp4",
    start_sec: float = 0.0,
    end_sec: float = 5.0,
    preview_path: str | None = "data/thumbs/preview.jpg",
    base_rag_similarity: float | None = None,
    profile_score: float | None = None,
    final_score: float | None = None,
    status: str = "candidate",
) -> None:
    conn.execute(
        """INSERT INTO candidate_gifs
           (candidate_id, source_run_id, source_run_candidate_id,
            source_video_sha256, source_video_path, start_sec, end_sec,
            artifact_path, preview_path, vlm_summary_json, tags_json,
            scenario_keys_json, base_rag_similarity, profile_score, final_score, status,
            created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            candidate_id,
            "run-1",
            f"rc-{candidate_id}",
            source_video_sha256,
            source_video_path,
            start_sec,
            end_sec,
            "data/exports/full.gif",
            preview_path,
            json.dumps({"caption": "no caption"}),
            json.dumps([]),
            json.dumps([]),
            base_rag_similarity,
            profile_score,
            final_score,
            status,
            "2026-07-18T00:00:00+00:00",
            "2026-07-18T00:00:00+00:00",
        ),
    )
    conn.commit()


# ===================================================================
# Dataclass contracts
# ===================================================================


class TestTimelineSpanDataclass:
    """TimelineSpan must be a frozen dataclass with the expected fields."""

    def test_fields(self):
        span = TimelineSpan(
            span_id="s1",
            start_sec=0.0,
            end_sec=10.0,
            label="Scene 1",
            base_score=0.85,
            preference_score=0.92,
            thumbnail_path="/thumbs/s1.jpg",
        )
        assert span.span_id == "s1"
        assert span.start_sec == 0.0
        assert span.end_sec == 10.0
        assert span.label == "Scene 1"
        assert span.base_score == 0.85
        assert span.preference_score == 0.92
        assert span.thumbnail_path == "/thumbs/s1.jpg"

    def test_scores_can_be_none(self):
        span = TimelineSpan(
            span_id="s2",
            start_sec=5.0,
            end_sec=15.0,
            label="Scene 2",
            base_score=None,
            preference_score=None,
            thumbnail_path=None,
        )
        assert span.base_score is None
        assert span.preference_score is None
        assert span.thumbnail_path is None

    def test_frozen(self):
        span = TimelineSpan(
            span_id="s1",
            start_sec=0.0,
            end_sec=10.0,
            label="test",
            base_score=None,
            preference_score=None,
            thumbnail_path=None,
        )
        with pytest.raises(AttributeError):
            span.span_id = "s2"  # type: ignore[misc]

    def test_frozen_window(self):
        window = TimelineWindow(
            video_id="v1",
            start_sec=0.0,
            end_sec=60.0,
            scenes=(),
            candidates=(),
            generated_gifs=(),
        )
        with pytest.raises(AttributeError):
            window.video_id = "v2"  # type: ignore[misc]


class TestTimelineWindowDataclass:
    """TimelineWindow must bundle scenes, candidates, and generated_gifs."""

    def test_empty_window(self):
        window = TimelineWindow(
            video_id="v1",
            start_sec=0.0,
            end_sec=60.0,
            scenes=(),
            candidates=(),
            generated_gifs=(),
        )
        assert window.video_id == "v1"
        assert window.start_sec == 0.0
        assert window.end_sec == 60.0
        assert window.scenes == ()
        assert window.candidates == ()
        assert window.generated_gifs == ()

    def test_window_with_spans(self):
        scene = TimelineSpan("s1", 0.0, 10.0, "Scene", 0.5, 0.6, None)
        cand = TimelineSpan("c1", 5.0, 8.0, "Candidate", 0.7, 0.8, "/t.jpg")
        gen = TimelineSpan("g1", 2.0, 6.0, "Generated", 0.9, 0.95, "/g.jpg")
        window = TimelineWindow(
            video_id="v1",
            start_sec=0.0,
            end_sec=60.0,
            scenes=(scene,),
            candidates=(cand,),
            generated_gifs=(gen,),
        )
        assert len(window.scenes) == 1
        assert len(window.candidates) == 1
        assert len(window.generated_gifs) == 1


# ===================================================================
# load_timeline_window tests
# ===================================================================


class TestLoadTimelineWindow:
    """load_timeline_window must return only overlapping spans."""

    def test_returns_window_for_known_video(self):
        conn = _conn()
        _insert_media(conn, "v1")
        _insert_clip(conn, "clip-1", "v1", start=0.0, end=10.0)

        window = load_timeline_window(conn, video_id="v1", start_sec=0.0, end_sec=60.0)
        assert window.video_id == "v1"
        assert len(window.scenes) == 1

    def test_only_overlapping_spans(self):
        """Only spans that overlap the window should be included."""
        conn = _conn()
        _insert_media(conn, "v1", sha256="abc123")
        # Clip within window
        _insert_clip(conn, "clip-in", "v1", start=5.0, end=15.0)
        # Clip entirely before window
        _insert_clip(conn, "clip-before", "v1", start=0.0, end=3.0)
        # Clip entirely after window
        _insert_clip(conn, "clip-after", "v1", start=50.0, end=60.0)
        # Clip overlapping start
        _insert_clip(conn, "clip-overlap-start", "v1", start=0.0, end=12.0)
        # Clip overlapping end
        _insert_clip(conn, "clip-overlap-end", "v1", start=18.0, end=25.0)

        window = load_timeline_window(conn, video_id="v1", start_sec=4.0, end_sec=20.0)
        clip_ids = {s.span_id for s in window.scenes}
        assert "clip-in" in clip_ids
        assert "clip-before" not in clip_ids
        assert "clip-after" not in clip_ids
        assert "clip-overlap-start" in clip_ids
        assert "clip-overlap-end" in clip_ids

    def test_overlapping_candidates(self):
        """Candidates should also be filtered by overlap."""
        conn = _conn()
        _insert_media(conn, "v1", sha256="abc123")
        # Candidate within window
        _insert_candidate(conn, "cand-in", start_sec=5.0, end_sec=10.0)
        # Candidate before window
        _insert_candidate(conn, "cand-before", start_sec=0.0, end_sec=2.0)
        # Candidate after window
        _insert_candidate(conn, "cand-after", start_sec=30.0, end_sec=35.0)

        window = load_timeline_window(conn, video_id="v1", start_sec=4.0, end_sec=20.0)
        cand_ids = {s.span_id for s in window.candidates}
        assert "cand-in" in cand_ids
        assert "cand-before" not in cand_ids
        assert "cand-after" not in cand_ids

    def test_max_thumbnails_default(self):
        """At most 60 thumbnails should be returned (default max_thumbnails=60)."""
        conn = _conn()
        _insert_media(conn, "v1", sha256="abc123")

        # Insert 40 clips with thumbnails
        for i in range(40):
            _insert_clip(
                conn, f"clip-{i}", "v1",
                start=i * 2.0, end=i * 2.0 + 1.0,
                exported_path=f"/thumbs/clip-{i}.jpg",
            )

        # Insert 30 candidates with thumbnails
        for i in range(30):
            _insert_candidate(
                conn, f"cand-{i}",
                start_sec=5.0 + i * 0.5,
                end_sec=5.0 + i * 0.5 + 1.0,
                preview_path=f"/thumbs/cand-{i}.jpg",
            )

        window = load_timeline_window(
            conn, video_id="v1", start_sec=0.0, end_sec=100.0, max_thumbnails=60,
        )

        # Count spans with non-None thumbnail_path
        thumb_count = sum(
            1 for s in list(window.scenes) + list(window.candidates) + list(window.generated_gifs)
            if s.thumbnail_path is not None
        )
        assert thumb_count <= 60, f"Expected <= 60 thumbnails, got {thumb_count}"

    def test_max_thumbnails_custom(self):
        """Custom max_thumbnails limit is respected."""
        conn = _conn()
        _insert_media(conn, "v1", sha256="abc123")

        for i in range(20):
            _insert_clip(
                conn, f"clip-{i}", "v1",
                start=i, end=i + 1,
                exported_path=f"/thumbs/clip-{i}.jpg",
            )

        window = load_timeline_window(
            conn, video_id="v1", start_sec=0.0, end_sec=100.0, max_thumbnails=5,
        )
        thumb_count = sum(
            1 for s in window.scenes if s.thumbnail_path is not None
        )
        assert thumb_count <= 5, f"Expected <= 5 thumbnails, got {thumb_count}"

    def test_spans_include_scores(self):
        """Spans must carry base_score and preference_score from source data."""
        conn = _conn()
        _insert_media(conn, "v1", sha256="abc123")

        # Clip with score_json
        score_data = json.dumps({"base_rag_similarity": 0.75, "final_score": 0.88})
        _insert_clip(conn, "clip-scored", "v1", start=0.0, end=10.0, score_json=score_data)

        # Candidate with scores
        _insert_candidate(
            conn, "cand-scored",
            start_sec=2.0, end_sec=7.0,
            base_rag_similarity=0.65,
            profile_score=0.72,
            final_score=0.80,
        )

        window = load_timeline_window(conn, video_id="v1", start_sec=0.0, end_sec=60.0)

        # Check clip scores parsed from score_json
        for s in window.scenes:
            if s.span_id == "clip-scored":
                assert s.base_score == 0.75
                assert s.preference_score == 0.88

        # Check candidate scores from columns
        for s in window.candidates:
            if s.span_id == "cand-scored":
                assert s.base_score == 0.65
                assert s.preference_score == 0.80  # final_score takes priority

    def test_exported_candidates_in_generated_gifs(self):
        """Candidates with exported/promoted/liked status go to generated_gifs."""
        conn = _conn()
        _insert_media(conn, "v1", sha256="abc123")

        _insert_candidate(conn, "cand-candidate", status="candidate", start_sec=0.0, end_sec=3.0)
        _insert_candidate(conn, "cand-promoted", status="promoted", start_sec=4.0, end_sec=7.0)
        _insert_candidate(conn, "cand-promoted2", status="promoted", start_sec=8.0, end_sec=11.0)
        _insert_candidate(conn, "cand-liked", status="liked", start_sec=12.0, end_sec=15.0)

        window = load_timeline_window(conn, video_id="v1", start_sec=0.0, end_sec=60.0)

        cand_ids = {s.span_id for s in window.candidates}
        gen_ids = {s.span_id for s in window.generated_gifs}

        assert "cand-candidate" in cand_ids
        assert "cand-promoted" in gen_ids
        assert "cand-promoted2" in gen_ids
        assert "cand-liked" in gen_ids
        assert "cand-candidate" not in gen_ids
        assert "cand-promoted" not in cand_ids
        assert "cand-promoted2" not in cand_ids
        assert "cand-liked" not in cand_ids

    def test_missing_video_returns_empty(self):
        """A non-existent video_id returns an empty TimelineWindow."""
        conn = _conn()
        window = load_timeline_window(conn, video_id="nonexistent", start_sec=0.0, end_sec=60.0)
        assert window.video_id == "nonexistent"
        assert window.scenes == ()
        assert window.candidates == ()
        assert window.generated_gifs == ()

    def test_missing_source_path_still_works(self):
        """Missing source paths should not crash the loader."""
        conn = _conn()
        _insert_media(conn, "v1", file_path="", sha256="missing-sha", duration=30.0)
        _insert_clip(conn, "clip-no-path", "v1", start=0.0, end=10.0, exported_path=None)
        _insert_candidate(
            conn, "cand-no-path",
            source_video_sha256="missing-sha",
            start_sec=2.0, end_sec=6.0,
            preview_path=None,
        )

        window = load_timeline_window(conn, video_id="v1", start_sec=0.0, end_sec=30.0)
        assert len(window.scenes) >= 1
        assert len(window.candidates) >= 1
        # All thumbnail paths should be None (no crash)
        for s in window.scenes:
            assert s.span_id == "clip-no-path"
        for s in window.candidates:
            assert s.span_id == "cand-no-path"

    def test_window_duration_from_media(self):
        """The window end_sec should use the provided value regardless of media duration."""
        conn = _conn()
        _insert_media(conn, "v1", duration=60.0)

        window = load_timeline_window(conn, video_id="v1", start_sec=10.0, end_sec=30.0)
        assert window.start_sec == 10.0
        assert window.end_sec == 30.0


# ===================================================================
# potplayer_target tests
# ===================================================================


class TestPotplayerTarget:
    """potplayer_target must produce valid jump targets."""

    def test_basic_path(self):
        target = potplayer_target(r"C:\videos\clip.mp4", 30.5)
        assert target.startswith("potplayer://")
        assert "seek=30.5" in target
        assert "clip.mp4" in target

    def test_path_with_spaces(self):
        """Spaces in the path must be percent-encoded."""
        target = potplayer_target(r"C:\my videos\great clip.mp4", 15.0)
        assert "%20" in target, f"Spaces not encoded in {target!r}"
        assert "great%20clip" in target

    def test_zero_seconds(self):
        target = potplayer_target("/videos/clip.mp4", 0.0)
        assert "seek=0.0" in target or "seek=0" in target

    def test_negative_seconds(self):
        """Negative seek values should be passed through (PotPlayer handles them)."""
        target = potplayer_target("/videos/clip.mp4", -5.0)
        assert "seek=-5.0" in target

    def test_no_shell_invocation(self):
        """The target string must be usable with subprocess.Popen([...], shell=False).

        This means the string should not contain shell metacharacters that would
        require shell interpretation. Spaces are already encoded.
        """
        import subprocess

        target = potplayer_target(r"C:\path with spaces\video.mp4", 42.5)
        # Should be a single argument suitable for Popen([...], shell=False)
        cmd = ["potplayer.exe", target]
        # No shell metacharacters in the argument
        assert "&&" not in target
        assert "|" not in target
        assert ";" not in target
        assert "`" not in target
        assert "$" not in target
        # The subprocess.list2cmdline roundtrip should be clean
        reconstructed = subprocess.list2cmdline(cmd)
        assert "potplayer.exe" in reconstructed
        assert "%20" in reconstructed  # spaces encoded, not quoted with shell chars

    def test_unix_path(self):
        target = potplayer_target("/videos/my movie.mp4", 100.0)
        assert target.startswith("potplayer://")
        assert "%20" in target
        assert "seek=100.0" in target

    def test_float_precision(self):
        """The seek value should preserve precision."""
        target = potplayer_target("/v.mp4", 123.456789)
        assert "seek=123.456789" in target
