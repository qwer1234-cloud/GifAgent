"""Phase 2 Task 5: Blind A/B review sessions.

Compares two experiment runs in a blind fashion — the reviewer sees
opaque side tokens instead of run or config IDs. Side assignment is
seeded and balanced. Clips are paired by source video and temporal
neighborhood proximity.
"""

from __future__ import annotations

import random
import secrets
import sqlite3
import uuid
from datetime import datetime, timezone

from app.quality_lab.models import ABResult, ABSession, BlindPair, Choice


class BlindReviewService:
    """Blind A/B review session lifecycle.

    Create a session from two experiment runs, walk through pairs of clips
    (blind), record judgments, and finally reveal which run was which.
    """

    def __init__(self, db: sqlite3.Connection) -> None:
        self._db = db

    # ------------------------------------------------------------------
    # create_session
    # ------------------------------------------------------------------

    def create_session(self, run_a: str, run_b: str, seed: int) -> ABSession:
        """Create a blind A/B session between two runs.

        Validates that both runs belong to the same manifest, pairs items
        by source-video fingerprint, and assigns opaque tokens for the
        reviewer.
        """
        rows = self._db.execute(
            "SELECT * FROM experiment_runs WHERE run_id IN (?, ?)",
            (run_a, run_b),
        ).fetchall()
        if len(rows) != 2:
            found = {r["run_id"] for r in rows}
            missing = [r for r in (run_a, run_b) if r not in found]
            raise ValueError(f"Run(s) not found: {missing}")

        run_map: dict[str, dict] = {r["run_id"]: dict(r) for r in rows}

        if run_map[run_a]["manifest_id"] != run_map[run_b]["manifest_id"]:
            raise ValueError("Runs must belong to the same manifest")

        manifest_id = run_map[run_a]["manifest_id"]
        run_split = run_map[run_a]["split"]

        session_id = uuid.uuid4().hex
        now = _utcnow()
        self._db.execute(
            """INSERT INTO ab_sessions
               (session_id, run_a, run_b, seed, status, created_at)
               VALUES (?, ?, ?, ?, 'active', ?)""",
            (session_id, run_a, run_b, seed, now),
        )

        # Build pair assignments
        items = self._db.execute(
            "SELECT * FROM benchmark_items WHERE manifest_id=? AND split=?",
            (manifest_id, run_split),
        ).fetchall()

        self._assign_pairs(session_id, items, seed)
        self._db.commit()

        return ABSession(
            session_id=session_id,
            run_a=run_a,
            run_b=run_b,
            seed=seed,
            status="active",
        )

    def _assign_pairs(
        self, session_id: str, items: list[sqlite3.Row], seed: int,
    ) -> None:
        """Group items by video fingerprint and pair adjacent clips.

        Each clip appears in at most one pair.  Side assignment (which
        run appears on the left) is randomised with ``seed``.
        """
        # Group by video fingerprint
        groups: dict[str, list[str]] = {}
        for item in items:
            fp = item["video_fingerprint"]
            groups.setdefault(fp, []).append(item["item_id"])

        rng = random.Random(seed)
        pair_index = 0

        for fp in sorted(groups):
            group = sorted(groups[fp])  # deterministic ordering
            for i in range(0, len(group) - 1, 2):
                a_id, b_id = group[i], group[i + 1]
                left_token = secrets.token_urlsafe(16)
                right_token = secrets.token_urlsafe(16)
                left_is_run_a = 1 if rng.random() < 0.5 else 0

                self._db.execute(
                    """INSERT INTO ab_pairs
                       (pair_index, session_id, item_a_id, item_b_id,
                        left_token, right_token, left_is_run_a)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (pair_index, session_id, a_id, b_id,
                     left_token, right_token, left_is_run_a),
                )
                pair_index += 1

    # ------------------------------------------------------------------
    # next_pair
    # ------------------------------------------------------------------

    def next_pair(self, session_id: str) -> BlindPair | None:
        """Return the first unjudged pair, or ``None`` if all are judged."""
        pair_row = self._db.execute(
            """SELECT p.pair_index, p.left_token, p.right_token
               FROM ab_pairs p
               LEFT JOIN ab_judgments j
                   ON j.session_id = p.session_id AND j.pair_index = p.pair_index
               WHERE p.session_id = ? AND j.judgment_id IS NULL
               ORDER BY p.pair_index
               LIMIT 1""",
            (session_id,),
        ).fetchone()

        if pair_row is None:
            return None

        return BlindPair(
            pair_index=pair_row["pair_index"],
            left_token=pair_row["left_token"],
            right_token=pair_row["right_token"],
        )

    # ------------------------------------------------------------------
    # record
    # ------------------------------------------------------------------

    def record(self, session_id: str, pair_id: str, choice: Choice) -> None:
        """Record a judgment for a pair.

        Raises ``ValueError`` if the pair has already been judged or the
        session does not exist.
        """
        pair_index = int(pair_id)
        now = _utcnow()

        # Validate session
        sess = self._db.execute(
            "SELECT status FROM ab_sessions WHERE session_id=?",
            (session_id,),
        ).fetchone()
        if sess is None:
            raise ValueError(f"Session not found: {session_id}")
        if sess["status"] != "active":
            raise ValueError(f"Session is {sess['status']}, not active")

        # Validate pair exists
        pair = self._db.execute(
            "SELECT 1 FROM ab_pairs WHERE session_id=? AND pair_index=?",
            (session_id, pair_index),
        ).fetchone()
        if pair is None:
            raise ValueError(f"Pair {pair_index} not found in session {session_id}")

        try:
            self._db.execute(
                """INSERT INTO ab_judgments
                   (session_id, pair_index, choice, created_at)
                   VALUES (?, ?, ?, ?)""",
                (session_id, pair_index, choice, now),
            )
            self._db.commit()
        except sqlite3.IntegrityError:
            self._db.rollback()
            raise ValueError(
                f"Pair {pair_index} in session {session_id} already judged"
            ) from None

    # ------------------------------------------------------------------
    # reveal
    # ------------------------------------------------------------------

    def reveal(self, session_id: str) -> ABResult:
        """Reveal the mapping and compute win/tie counts.

        Raises ``ValueError`` if any pair remains unjudged.
        """
        # Count judged vs total pairs
        total = self._db.execute(
            "SELECT COUNT(*) AS cnt FROM ab_pairs WHERE session_id=?",
            (session_id,),
        ).fetchone()["cnt"]

        judged = self._db.execute(
            "SELECT COUNT(*) AS cnt FROM ab_judgments WHERE session_id=?",
            (session_id,),
        ).fetchone()["cnt"]

        if judged < total:
            raise ValueError(
                f"Cannot reveal: {total - judged} pair(s) remain unjudged"
            )

        # Get session info and run configs
        sess = self._db.execute(
            "SELECT * FROM ab_sessions WHERE session_id=?",
            (session_id,),
        ).fetchone()
        if sess is None:
            raise ValueError(f"Session not found: {session_id}")

        run_a = sess["run_a"]
        run_b = sess["run_b"]

        config_a = self._db.execute(
            "SELECT config_id FROM experiment_runs WHERE run_id=?", (run_a,)
        ).fetchone()["config_id"]
        config_b = self._db.execute(
            "SELECT config_id FROM experiment_runs WHERE run_id=?", (run_b,)
        ).fetchone()["config_id"]

        # Compute counts from judgments + pair assignments
        rows = self._db.execute(
            """SELECT j.choice, p.left_is_run_a
               FROM ab_judgments j
               JOIN ab_pairs p
                   ON p.session_id = j.session_id AND p.pair_index = j.pair_index
               WHERE j.session_id = ?""",
            (session_id,),
        ).fetchall()

        run_a_wins = 0
        run_b_wins = 0
        ties = 0
        both_bad = 0

        for r in rows:
            choice = r["choice"]
            left_is_run_a = bool(r["left_is_run_a"])

            if choice == "tie":
                ties += 1
            elif choice == "both_bad":
                both_bad += 1
            elif choice == "left":
                if left_is_run_a:
                    run_a_wins += 1
                else:
                    run_b_wins += 1
            elif choice == "right":
                if left_is_run_a:
                    run_b_wins += 1
                else:
                    run_a_wins += 1

        return ABResult(
            session_id=session_id,
            run_a=run_a,
            run_b=run_b,
            config_a=config_a,
            config_b=config_b,
            run_a_wins=run_a_wins,
            run_b_wins=run_b_wins,
            ties=ties,
            both_bad=both_bad,
        )


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()
