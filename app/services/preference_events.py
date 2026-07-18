"""P1-4 / P3-1: Append-only feedback events with correction support."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone

from app.services.preference_types import (
    FeedbackEvent,
    FeedbackRating,
    RATING_TO_STATUS,
)
from app.services.scenario import json_dumps


_VALID_RATINGS = frozenset({"like", "neutral", "dislike", "quality_reject", "skip", "favorite"})


class PreferenceEventService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # ── public API ───────────────────────────────────────────────────────────

    def record_feedback(
        self,
        *,
        target_type: str,
        target_id: str,
        rating: FeedbackRating,
        source_video_sha256: str,
        scenario_keys: list[str],
        note: str | None = None,
        update_candidate_status: bool = True,
    ) -> FeedbackEvent:
        """Persist a new feedback event (not a correction)."""
        if rating not in _VALID_RATINGS:
            raise ValueError(
                f"Invalid rating {rating!r}; allowed: {sorted(_VALID_RATINGS)}"
            )

        event_id = f"prefevt_{uuid.uuid4().hex}"
        now = datetime.now(timezone.utc).isoformat()
        previous_status = self._read_previous_status(target_type, target_id)

        self.conn.execute(
            """INSERT INTO preference_events
               (event_id, target_type, target_id, rating,
                source_video_sha256, scenario_keys_json, note, created_at,
                previous_status, event_kind, supersedes_event_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                event_id,
                target_type,
                target_id,
                rating,
                source_video_sha256,
                json_dumps(scenario_keys),
                note,
                now,
                previous_status,
                "feedback",
                None,
            ),
        )

        if update_candidate_status and rating in RATING_TO_STATUS:
            new_status = RATING_TO_STATUS[rating]
            self.conn.execute(
                "UPDATE candidate_gifs SET status=?, updated_at=? WHERE candidate_id=?",
                (new_status, now, target_id),
            )

        self.conn.commit()

        return FeedbackEvent(
            event_id=event_id,
            target_type=target_type,  # type: ignore[arg-type]
            target_id=target_id,
            rating=rating,
            event_kind="feedback",
            supersedes_event_id=None,
            source_video_sha256=source_video_sha256,
            created_at=now,
        )

    def correct_feedback(
        self,
        event_id: str,
        replacement: FeedbackRating,
        reason: str,
    ) -> FeedbackEvent:
        """Create a correction that supersedes a prior feedback event.

        The original row is *never* mutated — a new ``correction``-kind event
        is inserted whose ``supersedes_event_id`` points to the original.
        """
        if replacement not in _VALID_RATINGS:
            raise ValueError(
                f"Invalid replacement rating {replacement!r}; "
                f"allowed: {sorted(_VALID_RATINGS)}"
            )

        # Use BEGIN IMMEDIATE to prevent race conditions between the check
        # and the insert below.
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            original = self.conn.execute(
                """SELECT target_type, target_id, rating, source_video_sha256
                   FROM preference_events WHERE event_id=?""",
                (event_id,),
            ).fetchone()
            if original is None:
                raise ValueError(
                    f"No preference event found with event_id={event_id!r}"
                )

            # Check if the original event has already been superseded by
            # another correction.  If so, reject the new correction to avoid
            # duplicate/cascading corrections.
            already_superseded = self.conn.execute(
                "SELECT 1 FROM preference_events "
                "WHERE supersedes_event_id=? AND event_kind='correction' LIMIT 1",
                (event_id,),
            ).fetchone()
            if already_superseded is not None:
                raise ValueError("event already corrected")

            new_event_id = f"prefevt_{uuid.uuid4().hex}"
            now = datetime.now(timezone.utc).isoformat()
            previous_status = self._read_previous_status(
                original["target_type"], original["target_id"]
            )

            self.conn.execute(
                """INSERT INTO preference_events
                   (event_id, target_type, target_id, rating,
                    source_video_sha256, scenario_keys_json, note, created_at,
                    previous_status, event_kind, supersedes_event_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    new_event_id,
                    original["target_type"],
                    original["target_id"],
                    replacement,
                    original["source_video_sha256"],
                    "[]",
                    f"correction: {reason}",
                    now,
                    previous_status,
                    "correction",
                    event_id,
                ),
            )

            # Update candidate status for the corrected rating.
            if replacement in RATING_TO_STATUS:
                new_status = RATING_TO_STATUS[replacement]
                self.conn.execute(
                    "UPDATE candidate_gifs SET status=?, updated_at=? WHERE candidate_id=?",
                    (new_status, now, original["target_id"]),
                )

            # Sync favorite_gifs table when correcting to/from "favorite".
            if original["rating"] == "favorite" and replacement != "favorite":
                self.conn.execute(
                    "DELETE FROM favorite_gifs WHERE candidate_id=?",
                    (original["target_id"],),
                )
            elif replacement == "favorite" and original["rating"] != "favorite":
                c = self.conn.execute(
                    "SELECT artifact_path FROM candidate_gifs WHERE candidate_id=?",
                    (original["target_id"],),
                ).fetchone()
                if c is not None:
                    self.conn.execute(
                        """INSERT OR IGNORE INTO favorite_gifs
                           (favorite_id, candidate_id, full_path, created_at)
                           VALUES (?, ?, ?, ?)""",
                        (f"fav_{uuid.uuid4().hex}", original["target_id"],
                         c["artifact_path"] or "", now),
                    )

            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

        return FeedbackEvent(
            event_id=new_event_id,
            target_type=original["target_type"],
            target_id=original["target_id"],
            rating=replacement,
            event_kind="correction",
            supersedes_event_id=event_id,
            source_video_sha256=original["source_video_sha256"],
            created_at=now,
        )

    def effective_feedback(
        self,
        *,
        before: str | None = None,
    ) -> list[FeedbackEvent]:
        """Return all feedback events that are currently *effective*.

        An event is effective when:
        * It has NOT been undone (``undone_at IS NULL``), **and**
        * No other event points at it via ``supersedes_event_id``.

        When *before* is provided only events created **before** that
        timestamp are considered.
        """
        query = """
            SELECT event_id, target_type, target_id, rating,
                   event_kind, supersedes_event_id,
                   source_video_sha256, created_at
            FROM preference_events e
            WHERE e.undone_at IS NULL
              AND e.event_id NOT IN (
                  SELECT supersedes_event_id FROM preference_events
                  WHERE supersedes_event_id IS NOT NULL
              )
        """
        params: list[str] = []
        if before is not None:
            query += " AND e.created_at < ?"
            params.append(before)
        query += " ORDER BY e.created_at ASC"

        rows = self.conn.execute(query, params).fetchall()
        return [self._row_to_event(r) for r in rows]

    # ── backward-compat aliases (still used by UI / profile builder) ─────────

    def latest_effective_ratings(self) -> dict[str, FeedbackEvent]:
        """Return the most recent effective FeedbackEvent per (type:id).

        Phase 3 update: events that have been superseded by a correction
        are excluded.
        """
        rows = self.conn.execute(
            """SELECT event_id, target_type, target_id, rating,
                      event_kind, supersedes_event_id,
                      source_video_sha256, created_at
               FROM preference_events e
               WHERE e.undone_at IS NULL
                 AND e.event_id NOT IN (
                    SELECT supersedes_event_id FROM preference_events
                    WHERE supersedes_event_id IS NOT NULL
                 )
               ORDER BY e.created_at ASC"""
        ).fetchall()

        result: dict[str, FeedbackEvent] = {}
        for row in rows:
            key = f"{row['target_type']}:{row['target_id']}"
            result[key] = self._row_to_event(row)
        return result

    def undo_last_candidate_action(self) -> dict[str, str | None]:
        """Undo the newest active candidate review event without deleting it.

        Phase 3: this method skips ``correction``-kind events (use
        ``correct_feedback`` to supersede them instead).  When undoing a
        ``favorite`` rating the corresponding row in ``favorite_gifs`` is
        also removed.
        """
        row = self.conn.execute(
            """SELECT event_id, target_id, rating, note, previous_status, event_kind
               FROM preference_events
               WHERE target_type='candidate_gif'
                 AND undone_at IS NULL
                 AND event_kind != 'correction'
                 AND event_id NOT IN (
                    SELECT supersedes_event_id FROM preference_events
                    WHERE supersedes_event_id IS NOT NULL
                 )
               ORDER BY created_at DESC, rowid DESC LIMIT 1"""
        ).fetchone()
        if row is None:
            return {"status": "nothing_to_undo", "event_id": None, "candidate_id": None}

        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """UPDATE preference_events
               SET undone_at=?, undone_reason=? WHERE event_id=?""",
            (now, "user_undo", row["event_id"]),
        )
        if row["previous_status"] is not None:
            self.conn.execute(
                "UPDATE candidate_gifs SET status=?, updated_at=? WHERE candidate_id=?",
                (row["previous_status"], now, row["target_id"]),
            )

        # Remove from favorite_gifs when the undone event is a favorite.
        is_favorite = row["rating"] == "favorite" or (
            row["rating"] == "like" and row["note"] == "favorite"
        )
        if is_favorite:
            self.conn.execute(
                "DELETE FROM favorite_gifs WHERE candidate_id=?",
                (row["target_id"],),
            )
        self.conn.commit()
        return {
            "status": "undone",
            "event_id": row["event_id"],
            "candidate_id": row["target_id"],
            "rating": row["rating"],
        }

    # ── helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> FeedbackEvent:
        return FeedbackEvent(
            event_id=row["event_id"],
            target_type=row["target_type"],
            target_id=row["target_id"],
            rating=row["rating"],
            event_kind=row["event_kind"],
            supersedes_event_id=row["supersedes_event_id"],
            source_video_sha256=row["source_video_sha256"],
            created_at=row["created_at"],
        )

    def _read_previous_status(
        self, target_type: str, target_id: str
    ) -> str | None:
        if target_type == "candidate_gif":
            candidate_row = self.conn.execute(
                "SELECT status FROM candidate_gifs WHERE candidate_id=?",
                (target_id,),
            ).fetchone()
            return candidate_row["status"] if candidate_row else None
        return None
