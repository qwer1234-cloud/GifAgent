"""P1-7: PreferenceEvaluationService — holdout evaluation and publish gating.

Evaluates a built preference profile against a holdout set of judgments.
Computes ranking metrics (Like@20, Dislike@20, NDCG@20) and enforces
gates before allowing the profile to be published as the active preference
memory.
"""

from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Gate constants
# ---------------------------------------------------------------------------

MIN_HOLDOUT_JUDGMENTS = 30

# NDCG gain values
GAIN_MAP: dict[str, int] = {"like": 3, "neutral": 1, "dislike": 0}


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class PreferenceEvaluationService:
    """Holdout evaluation and publish gating for preference profiles.

    Constructed with a ``sqlite3.Connection`` that already has the preference
    schema applied.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def evaluate(
        self,
        profile_version: str,
        *,
        holdout_path: Path | None = None,
        holdout_count: int = 0,
    ) -> dict[str, Any]:
        """Evaluate a built profile against a holdout set.

        Parameters
        ----------
        profile_version:
            The profile build to evaluate (must exist and be completed).
        holdout_path:
            Path to a JSONL file of holdout judgments. Each line must be a
            JSON object with ``candidate_id`` (str), ``rating`` (one of
            ``"like"``, ``"dislike"``, ``"neutral"``), and ``judged_at``
            (ISO-8601 timestamp).
        holdout_count:
            When ``holdout_path`` is not provided, generates this many
            synthetic judgments for testing. Synthetic candidates will not
            match any real database rows, so the overlap gate always passes
            and metrics will be zero.

        Returns
        -------
        dict
            Keys: ``can_publish`` (bool), ``gate_reasons`` (list[str]),
            ``like_at_20`` (float), ``dislike_at_20`` (float),
            ``ndcg_at_20`` (float).
        """
        # ---- 1. Verify build exists ----------------------------------------
        build = self.conn.execute(
            """SELECT profile_version, event_watermark, status
               FROM preference_profile_builds
               WHERE profile_version = ?""",
            (profile_version,),
        ).fetchone()

        if build is None:
            raise ValueError(f"Build not found: {profile_version}")

        # ---- 2. Load holdout judgments -------------------------------------
        holdout_judgments: dict[str, dict[str, str]] = {}
        if holdout_path is not None:
            holdout_judgments = self._load_holdout_file(holdout_path)
        elif holdout_count > 0:
            holdout_judgments = self._synthetic_holdout(holdout_count)

        # ---- 3. Evaluate gates ---------------------------------------------
        gate_reasons: list[str] = []

        # Gate A: minimum holdout judgments
        if len(holdout_judgments) < MIN_HOLDOUT_JUDGMENTS:
            gate_reasons.append(
                f"holdout_judgment_count={len(holdout_judgments)}"
                f" < {MIN_HOLDOUT_JUDGMENTS}"
            )

        # Gate B: source-video overlap between training and holdout
        if holdout_judgments:
            overlap = self._check_source_video_overlap(
                event_watermark=build["event_watermark"],
                holdout_candidate_ids=list(holdout_judgments.keys()),
            )
            gate_reasons.extend(overlap)

        can_publish = len(gate_reasons) == 0

        # ---- 4. Compute ranking metrics ------------------------------------
        like_at_20, dislike_at_20, ndcg_at_20 = self._compute_metrics(
            holdout_judgments
        )

        return {
            "can_publish": can_publish,
            "gate_reasons": gate_reasons,
            "like_at_20": like_at_20,
            "dislike_at_20": dislike_at_20,
            "ndcg_at_20": ndcg_at_20,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_holdout_file(self, path: Path) -> dict[str, dict[str, str]]:
        """Load judgments from a JSONL file."""
        judgments: dict[str, dict[str, str]] = {}
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                cid = obj["candidate_id"]
                judgments[cid] = {
                    "candidate_id": cid,
                    "rating": obj["rating"],
                    "judged_at": obj.get("judged_at", ""),
                }
        return judgments

    def _synthetic_holdout(self, count: int) -> dict[str, dict[str, str]]:
        """Generate synthetic judgments for testing."""
        judgments: dict[str, dict[str, str]] = {}
        for i in range(count):
            cid = f"synth-holdout-{i}"
            judgments[cid] = {
                "candidate_id": cid,
                "rating": "like" if i < int(count * 0.6) else "dislike",
                "judged_at": "2026-01-01T00:00:00Z",
            }
        return judgments

    def _check_source_video_overlap(
        self,
        *,
        event_watermark: str,
        holdout_candidate_ids: list[str],
    ) -> list[str]:
        """Return gate reasons for any source-video overlap between training
        events and holdout candidates."""
        # Collect training source videos (events up to the watermark)
        training_rows = self.conn.execute(
            """SELECT DISTINCT source_video_sha256
               FROM preference_events
               WHERE created_at <= ?""",
            (event_watermark,),
        ).fetchall()

        training_videos: set[str] = {row[0] for row in training_rows}

        if not training_videos or not holdout_candidate_ids:
            return []

        # Look up source videos for holdout candidates
        placeholders = ",".join(["?"] * len(holdout_candidate_ids))
        holdout_rows = self.conn.execute(
            f"""SELECT candidate_id, source_video_sha256
                 FROM candidate_gifs
                 WHERE candidate_id IN ({placeholders})""",
            holdout_candidate_ids,
        ).fetchall()

        reasons: list[str] = []
        for row in holdout_rows:
            vid = row["source_video_sha256"]
            if vid in training_videos:
                reasons.append(
                    f"source_video_overlap: holdout candidate {row['candidate_id']}"
                    f" shares source video {vid} with training data"
                )
        return reasons

    def _compute_metrics(
        self,
        holdout_judgments: dict[str, dict[str, str]],
    ) -> tuple[float, float, float]:
        """Compute Like@20, Dislike@20, and NDCG@20.

        Candidates are ranked by ``final_score`` descending (from the
        ``candidate_gifs`` table).  If no candidates have scores, metrics
        return 0.0.
        """
        # ---- Ranked candidate list ------------------------------------------
        ranked_rows = self.conn.execute(
            """SELECT candidate_id, final_score
               FROM candidate_gifs
               WHERE final_score IS NOT NULL
               ORDER BY final_score DESC"""
        ).fetchall()

        top_20_ids = [row["candidate_id"] for row in ranked_rows[:20]]

        # ---- Partition holdout judgments by rating --------------------------
        holdout_liked: set[str] = set()
        holdout_disliked: set[str] = set()
        holdout_neutral: set[str] = set()

        for cid, judgment in holdout_judgments.items():
            rating = judgment["rating"]
            if rating == "like":
                holdout_liked.add(cid)
            elif rating == "dislike":
                holdout_disliked.add(cid)
            elif rating == "neutral":
                holdout_neutral.add(cid)

        # ---- Like@20 --------------------------------------------------------
        liked_in_top20 = sum(1 for cid in top_20_ids if cid in holdout_liked)
        like_at_20 = (
            liked_in_top20 / len(holdout_liked) if holdout_liked else 0.0
        )

        # ---- Dislike@20 -----------------------------------------------------
        disliked_in_top20 = sum(
            1 for cid in top_20_ids if cid in holdout_disliked
        )
        dislike_at_20 = disliked_in_top20 / 20.0 if top_20_ids else 0.0

        # ---- NDCG@20 --------------------------------------------------------
        ndcg_at_20 = self._compute_ndcg_at_20(
            top_20_ids=top_20_ids,
            holdout_judgments=holdout_judgments,
        )

        return (
            round(like_at_20, 4),
            round(dislike_at_20, 4),
            round(ndcg_at_20, 4),
        )

    def _compute_ndcg_at_20(
        self,
        *,
        top_20_ids: list[str],
        holdout_judgments: dict[str, dict[str, str]],
    ) -> float:
        """Compute NDCG@20 with gain values: like=3, neutral=1, dislike=0."""

        def _gain(candidate_id: str) -> int:
            judgment = holdout_judgments.get(candidate_id)
            if judgment is None:
                return 0
            return GAIN_MAP.get(judgment["rating"], 0)

        # ---- DCG@20 ---------------------------------------------------------
        dcg = 0.0
        for i, cid in enumerate(top_20_ids):
            gain = _gain(cid)
            if gain > 0:
                dcg += gain / math.log2(i + 2)  # i is 0-indexed, rank = i+1

        # ---- IDCG@20 --------------------------------------------------------
        # Sort all holdout judgments by gain descending, take top 20
        all_gains = sorted(
            (GAIN_MAP.get(j["rating"], 0) for j in holdout_judgments.values()),
            reverse=True,
        )
        ideal_gains = all_gains[:20]

        idcg = 0.0
        for i, gain in enumerate(ideal_gains):
            if gain > 0:
                idcg += gain / math.log2(i + 2)

        return dcg / idcg if idcg > 0 else 0.0
