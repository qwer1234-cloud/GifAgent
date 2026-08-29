"""Property tests for the timeline viewport query (W5 Task 7).

Locked against ``app.services.timeline.load_timeline_window`` over an
in-memory SQLite database:

- every returned span overlaps the window: ``start_sec < end_sec`` and
  ``end_sec > start_sec`` (strict, mirroring the SQL predicate);
- spans carrying a non-None ``thumbnail_path`` never exceed
  ``max_thumbnails``;
- a clip inserted entirely outside the window never appears.

The schema helpers are copied verbatim from ``tests/test_timeline.py`` (the
plan requires the copy; the original test file is not modified).
"""

from __future__ import annotations

import json
import sqlite3

from hypothesis import given, seed, settings, strategies as st

from app.services.timeline import load_timeline_window

SEED = 20260828


# ── helpers (copied verbatim from tests/test_timeline.py) ────────────────────


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


# ── property strategies ──────────────────────────────────────────────────────


@st.composite
def timeline_scenarios(draw):
    """A viewport window plus random clip/candidate spans around it."""
    window_start = draw(
        st.floats(0.0, 80.0, allow_nan=False, allow_infinity=False)
    )
    width = draw(st.floats(1.0, 40.0, allow_nan=False, allow_infinity=False))
    window_end = window_start + width

    def span(draw):
        center = draw(
            st.floats(
                window_start - 15.0,
                window_end + 15.0,
                allow_nan=False,
                allow_infinity=False,
            )
        )
        half = draw(
            st.floats(0.5, 15.0, allow_nan=False, allow_infinity=False)
        )
        return center - half, center + half

    clip_count = draw(st.integers(0, 6))
    candidate_count = draw(st.integers(0, 6))
    clips = [span(draw) for _ in range(clip_count)]
    candidates = [span(draw) for _ in range(candidate_count)]
    max_thumbnails = draw(st.integers(0, 12))
    statuses = draw(
        st.lists(
            st.sampled_from(
                [
                    "candidate",
                    "liked",
                    "disliked",
                    "neutral",
                    "promoted",
                    "rejected",
                    "archived",
                ]
            ),
            min_size=candidate_count,
            max_size=candidate_count,
        )
    )
    return {
        "window_start": window_start,
        "window_end": window_end,
        "clips": clips,
        "candidates": list(zip(candidates, statuses)),
        "max_thumbnails": max_thumbnails,
    }


@given(scenario=timeline_scenarios())
@settings(deadline=None)
@seed(SEED)
def test_window_spans_overlap_and_respect_thumbnail_cap(scenario):
    conn = _conn()
    _insert_media(conn, "v1", sha256="abc123")

    for i, (start, end) in enumerate(scenario["clips"]):
        _insert_clip(
            conn,
            f"clip-{i}",
            "v1",
            start=start,
            end=end,
            exported_path=f"/thumbs/clip-{i}.jpg",
        )
    for i, ((start, end), status) in enumerate(scenario["candidates"]):
        _insert_candidate(
            conn,
            f"cand-{i}",
            start_sec=start,
            end_sec=end,
            preview_path=f"/thumbs/cand-{i}.jpg",
            status=status,
        )
    # A clip that lies entirely after the window must never surface.
    _insert_clip(
        conn,
        "clip-outside",
        "v1",
        start=scenario["window_end"] + 2.0,
        end=scenario["window_end"] + 3.0,
        exported_path="/thumbs/clip-outside.jpg",
    )

    window = load_timeline_window(
        conn,
        video_id="v1",
        start_sec=scenario["window_start"],
        end_sec=scenario["window_end"],
        max_thumbnails=scenario["max_thumbnails"],
    )
    all_spans = [
        *window.scenes,
        *window.candidates,
        *window.generated_gifs,
    ]

    for span in all_spans:
        assert span.start_sec < window.end_sec, span
        assert span.end_sec > window.start_sec, span

    thumbnails = [
        span for span in all_spans if span.thumbnail_path is not None
    ]
    assert len(thumbnails) <= scenario["max_thumbnails"]

    returned_ids = {span.span_id for span in all_spans}
    assert "clip-outside" not in returned_ids


def test_unknown_video_returns_empty_window():
    conn = _conn()
    window = load_timeline_window(
        conn, video_id="missing", start_sec=0.0, end_sec=60.0
    )
    assert window.scenes == ()
    assert window.candidates == ()
    assert window.generated_gifs == ()
