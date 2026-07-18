"""Timeline service -- moment timeline and PotPlayer jump targets.

Provides the ``TimelineSpan`` / ``TimelineWindow`` data models and the
``load_timeline_window`` query that fetches scenes, candidates, and exported
GIFs overlapping a given viewport window from library.db.

Also exports ``potplayer_target`` which returns a ``potplayer://`` protocol
URL for jumping a video to a specific seek position.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Optional


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TimelineSpan:
    """A single span on the timeline (scene, candidate, or generated GIF).

    Attributes
    ----------
    span_id:
        Unique identifier (``clip_id`` for scenes, ``candidate_id`` otherwise).
    start_sec, end_sec:
        Time range in seconds relative to the source video.
    label:
        Human-readable label (e.g. "Scene 3" or VLM summary snippet).
    base_score:
        Base RAG similarity score (``base_rag_similarity``), if available.
    preference_score:
        Preference-adjusted score (``final_score`` or ``profile_score``).
    thumbnail_path:
        Filesystem path to a representative thumbnail image, or *None*.
        May be *None* when the thumbnail limit is exceeded or no thumbnail
        exists.
    """

    span_id: str
    start_sec: float
    end_sec: float
    label: str
    base_score: Optional[float]
    preference_score: Optional[float]
    thumbnail_path: Optional[str]


@dataclass(frozen=True)
class TimelineWindow:
    """Bundle of timeline spans visible within one viewport window.

    Attributes
    ----------
    video_id:
        The source video identifier.
    start_sec, end_sec:
        The viewport time window in seconds.
    scenes:
        Video clips (scenes) overlapping the window.
    candidates:
        Candidate GIF spans overlapping the window, excluding exported ones.
    generated_gifs:
        Exported/promoted/liked candidate GIFs overlapping the window.
    """

    video_id: str
    start_sec: float
    end_sec: float
    scenes: tuple[TimelineSpan, ...]
    candidates: tuple[TimelineSpan, ...]
    generated_gifs: tuple[TimelineSpan, ...]


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------


def _parse_score_json(score_json: str | None) -> dict:
    """Safely parse the ``score_json`` column from ``video_clips``."""
    if not score_json:
        return {}
    try:
        return json.loads(score_json)
    except (json.JSONDecodeError, TypeError):
        return {}


def _extract_label_from_vlm(vlm_summary_json: str | None) -> str:
    """Extract a short label from candidate ``vlm_summary_json``."""
    if not vlm_summary_json:
        return ""
    try:
        data = json.loads(vlm_summary_json)
    except (json.JSONDecodeError, TypeError):
        return ""
    caption = data.get("caption") or data.get("summary") or ""
    if isinstance(caption, str) and caption.strip():
        # Truncate to a reasonable length for timeline labels
        text = caption.strip()
        if len(text) > 60:
            text = text[:57] + "..."
        return text
    return ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_timeline_window(
    conn: sqlite3.Connection,
    *,
    video_id: str,
    start_sec: float,
    end_sec: float,
    max_thumbnails: int = 60,
) -> TimelineWindow:
    """Build a ``TimelineWindow`` for a video viewport.

    Parameters
    ----------
    conn:
        Open connection to **library.db** with ``row_factory = sqlite3.Row``.
    video_id:
        The ``media.media_id`` of the source video.
    start_sec, end_sec:
        Viewport time window. Only spans that **overlap** this window are
        returned (a span ``[s, e)`` overlaps when ``s < end_sec`` and
        ``e > start_sec``).
    max_thumbnails:
        Maximum number of spans that will carry a non-*None* ``thumbnail_path``.
        Once this limit is reached, additional spans have *None* thumbnails.
        Defaults to 60.

    Returns
    -------
    TimelineWindow
        An empty window (all tuples empty) is returned when the video is not
        found in the ``media`` table.

    Notes
    -----
    - **Scenes** are fetched from ``video_clips``.
    - **Candidates** are fetched from ``candidate_gifs`` and filtered to those
      whose ``source_video_sha256`` matches the video's ``sha256``.
    - **Generated GIFs** are a subset of candidates with status ``exported``,
      ``promoted``, or ``liked``.
    - Thumbnails are assigned first-come-first-served: scenes first, then
      candidates, then generated GIFs.
    """
    # --- resolve video metadata ---
    video_row = conn.execute(
        "SELECT media_id, file_path, sha256, duration FROM media WHERE media_id = ?",
        (video_id,),
    ).fetchone()

    if video_row is None:
        return TimelineWindow(
            video_id=video_id,
            start_sec=start_sec,
            end_sec=end_sec,
            scenes=(),
            candidates=(),
            generated_gifs=(),
        )

    sha256 = video_row["sha256"]
    thumbnail_budget = max_thumbnails

    # --- scenes from video_clips ---
    scenes: list[TimelineSpan] = []
    clip_rows = conn.execute(
        """SELECT clip_id, start, end, status, score_json, exported_path
           FROM video_clips
           WHERE video_id = ? AND start < ? AND end > ?
           ORDER BY start ASC""",
        (video_id, end_sec, start_sec),
    ).fetchall()

    for row in clip_rows:
        score = _parse_score_json(row["score_json"])
        label = _scene_label(row, score)
        thumbnail = row["exported_path"] if thumbnail_budget > 0 else None
        if row["exported_path"] is not None:
            thumbnail_budget -= 1

        scenes.append(
            TimelineSpan(
                span_id=row["clip_id"],
                start_sec=row["start"],
                end_sec=row["end"],
                label=label,
                base_score=score.get("base_rag_similarity"),
                preference_score=score.get("final_score"),
                thumbnail_path=thumbnail,
            )
        )

    # --- candidates & generated GIFs from candidate_gifs ---
    candidates: list[TimelineSpan] = []
    generated: list[TimelineSpan] = []

    cand_rows = conn.execute(
        """SELECT candidate_id, start_sec, end_sec, preview_path,
                  base_rag_similarity, profile_score, final_score, status,
                  vlm_summary_json
           FROM candidate_gifs
           WHERE source_video_sha256 = ?
             AND start_sec < ? AND end_sec > ?
           ORDER BY start_sec ASC""",
        (sha256, end_sec, start_sec),
    ).fetchall()

    for row in cand_rows:
        label = _candidate_label(row)
        preference_score = (
            row["final_score"]
            if row["final_score"] is not None
            else row["profile_score"]
        )
        thumbnail = row["preview_path"] if thumbnail_budget > 0 else None
        if row["preview_path"] is not None:
            thumbnail_budget -= 1

        span = TimelineSpan(
            span_id=row["candidate_id"],
            start_sec=row["start_sec"],
            end_sec=row["end_sec"],
            label=label,
            base_score=row["base_rag_similarity"],
            preference_score=preference_score,
            thumbnail_path=thumbnail,
        )

        if row["status"] in ("promoted", "liked"):
            generated.append(span)
        else:
            candidates.append(span)

    return TimelineWindow(
        video_id=video_id,
        start_sec=start_sec,
        end_sec=end_sec,
        scenes=tuple(scenes),
        candidates=tuple(candidates),
        generated_gifs=tuple(generated),
    )


def potplayer_target(video_path: str, start_sec: float) -> str:
    """Return a ``potplayer://`` protocol URL for jumping to *start_sec*.

    Spaces in *video_path* are percent-encoded so the result is safe to use
    as a single argument in ``subprocess.Popen([...], shell=False)`` without
    requiring shell quoting.

    Parameters
    ----------
    video_path:
        Absolute or relative filesystem path to the video file.
    start_sec:
        Seek position in seconds.

    Returns
    -------
    str
        A ``potplayer://`` URL of the form::

            potplayer://<encoded-path>?seek=<seconds>

    Example
    -------
    >>> potplayer_target("C:/my videos/clip.mp4", 30.5)
    'potplayer://C:/my%20videos/clip.mp4?seek=30.5'
    """
    # Percent-encode spaces to avoid shell quoting issues
    encoded_path = video_path.replace(" ", "%20")
    return f"potplayer://{encoded_path}?seek={start_sec}"


# ---------------------------------------------------------------------------
# Internal label helpers
# ---------------------------------------------------------------------------


def _scene_label(row: sqlite3.Row, score: dict) -> str:
    """Build a label for a scene span."""
    status = row["status"] or "candidate"
    label = f"Scene {status}"
    # If there's a score summary, append it
    if "label" in score:
        label = str(score["label"])
    elif "scene_type" in score:
        label = str(score["scene_type"])
    return label


def _candidate_label(row: sqlite3.Row) -> str:
    """Build a label for a candidate span."""
    vlm_label = _extract_label_from_vlm(row["vlm_summary_json"])
    if vlm_label:
        return vlm_label
    status = row["status"] or "candidate"
    return f"Candidate {status}"
