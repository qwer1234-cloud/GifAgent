"""Persistent favorite GIF records."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone


class FavoriteService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def favorite(self, candidate_id: str, full_path: str) -> dict[str, str]:
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """INSERT OR IGNORE INTO favorite_gifs
               (favorite_id, candidate_id, full_path, created_at)
               VALUES (?, ?, ?, ?)""",
            (f"fav_{uuid.uuid4().hex}", candidate_id, full_path, now),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT full_path FROM favorite_gifs WHERE candidate_id=?",
            (candidate_id,),
        ).fetchone()
        return {
            "candidate_id": candidate_id,
            "status": "favorited",
            "full_path": row["full_path"] if row else full_path,
        }
