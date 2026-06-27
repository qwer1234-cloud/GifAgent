"""P1-4: PreferenceEventService — append-only feedback events."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone

from app.services.preference_types import (
    FeedbackEvent,
    Rating,
    RATING_TO_STATUS,
)
from app.services.scenario import json_dumps


_VALID_RATINGS = frozenset({"like", "neutral", "dislike", "quality_reject", "skip"})


class PreferenceEventService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def record_feedback(
        self,
        *,
        target_type: str,
        target_id: str,
        rating: Rating,
        source_video_sha256: str,
        scenario_keys: list[str],
        note: str | None = None,
    ) -> FeedbackEvent:
        if rating not in _VALID_RATINGS:
            raise ValueError(
                f"Invalid rating {rating!r}; allowed: {sorted(_VALID_RATINGS)}"
            )

        event_id = f"prefevt_{uuid.uuid4().hex}"
        now = datetime.now(timezone.utc).isoformat()

        self.conn.execute(
            """INSERT INTO preference_events
               (event_id, target_type, target_id, rating,
                source_video_sha256, scenario_keys_json, note, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                event_id,
                target_type,
                target_id,
                rating,
                source_video_sha256,
                json_dumps(scenario_keys),
                note,
                now,
            ),
        )

        # Update candidate_gifs.status unless rating is 'skip'
        if rating != "skip":
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
            source_video_sha256=source_video_sha256,
            created_at=now,
        )

    def latest_effective_ratings(self) -> dict[str, FeedbackEvent]:
        """Return the most recent FeedbackEvent per (target_type, target_id) keyed by 'type:id'."""
        rows = self.conn.execute(
            """SELECT event_id, target_type, target_id, rating,
                      source_video_sha256, created_at
               FROM preference_events
               ORDER BY created_at ASC"""
        ).fetchall()

        result: dict[str, FeedbackEvent] = {}
        for row in rows:
            key = f"{row['target_type']}:{row['target_id']}"
            result[key] = FeedbackEvent(
                event_id=row["event_id"],
                target_type=row["target_type"],
                target_id=row["target_id"],
                rating=row["rating"],
                source_video_sha256=row["source_video_sha256"],
                created_at=row["created_at"],
            )
        return result
