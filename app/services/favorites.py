"""Persistent favorite GIF records.

Phase 3: the ``favorite()`` method also records a ``favorite``
:class:`~app.services.preference_types.FeedbackEvent` so that favorite
actions appear in the append-only preference event stream alongside
likes / dislikes / etc.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone

from app.services.preference_events import PreferenceEventService


class FavoriteService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def favorite(self, candidate_id: str, full_path: str) -> dict[str, str]:
        now = datetime.now(timezone.utc).isoformat()
        cursor = self.conn.execute(
            """INSERT OR IGNORE INTO favorite_gifs
               (favorite_id, candidate_id, full_path, created_at)
               VALUES (?, ?, ?, ?)""",
            (f"fav_{uuid.uuid4().hex}", candidate_id, full_path, now),
        )
        inserted = cursor.rowcount > 0

        # Only record a favorite event when a NEW row was inserted (not on repeat).
        if inserted:
            candidate = self.conn.execute(
                "SELECT source_video_sha256 FROM candidate_gifs WHERE candidate_id=?",
                (candidate_id,),
            ).fetchone()
            if candidate is not None:
                svc = PreferenceEventService(self.conn)
                svc.record_feedback(
                    target_type="candidate_gif",
                    target_id=candidate_id,
                    rating="favorite",
                    source_video_sha256=candidate["source_video_sha256"],
                    scenario_keys=[],
                    update_candidate_status=False,  # favorite does not change status
                )  # record_feedback commits; no extra commit needed here.
            else:
                self.conn.commit()
        else:
            self.conn.commit()

        row = self.conn.execute(
            "SELECT full_path FROM favorite_gifs WHERE candidate_id=?",
            (candidate_id,),
        ).fetchone()
        return {
            "candidate_id": candidate_id,
            "status": "favorited",
            "full_path": row["full_path"] if row else full_path,
            "created": inserted,
        }
