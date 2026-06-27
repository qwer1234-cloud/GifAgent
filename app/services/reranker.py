"""P1-6: PreferenceReranker — availability-aware reranking behind a feature flag.

Consumes the current published preference profile (if any) and adjusts
candidate scores by measuring cosine similarity to liked/disliked centroids.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import numpy as np

from app.services.preference_types import ScoreBreakdown

# ---------------------------------------------------------------------------
# Nominal weight configuration (before renormalization)
# ---------------------------------------------------------------------------

_NOMINAL_POSITIVE_WEIGHTS: dict[str, float] = {
    "base_rag": 0.55,
    "global_like": 0.25,
    "scenario_like": 0.15,
}

_NOMINAL_NEGATIVE_WEIGHTS: dict[str, float] = {
    "global_dislike": 0.20,
}


def _normalize(vec: np.ndarray) -> np.ndarray:
    """L2-normalize a vector in place (or return a zero vector unchanged)."""
    norm = np.linalg.norm(vec)
    if norm > 0:
        vec = vec / norm
    return vec


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class PreferenceReranker:
    """Availability-aware scoring layer on top of RAG similarity.

    Constructed with a ``sqlite3.Connection`` that already has the preference
    schema applied.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def score(
        self,
        *,
        candidate_vector: np.ndarray,
        base_rag_similarity: float,
        scenario_keys: list[str],
        profile_version: str | None,
        enabled: bool,
    ) -> ScoreBreakdown:
        """Compute the final score for a candidate.

        When ``enabled`` is ``False`` the baseline RAG similarity is returned
        unchanged.  When enabled but no profile is published (or the requested
        ``profile_version`` does not exist) the result is likewise identical
        to baseline.

        Parameters
        ----------
        candidate_vector:
            Normalized float32 embedding of shape ``(768,)``.
        base_rag_similarity:
            The RAG cosine similarity (float in [0, 1]).
        scenario_keys:
            Tags / emotion keys used to look up scenario profiles (e.g.
            ``["emotion:joy", "tag:smile"]``).
        profile_version:
            Explicit profile version to use, or ``None`` to resolve from the
            ``preference_profile_current`` table.
        enabled:
            When ``False`` the reranker is a no-op.

        Returns
        -------
        ScoreBreakdown
            Dict with fields: ``base_rag_similarity``, ``profile_score``,
            ``raw_score``, ``final_score``, ``active_weights``,
            ``inactive_reasons``, ``preference_profile_version``.
        """
        # ---- Fast path: feature disabled -----------------------------------
        if not enabled:
            return self._baseline(base_rag_similarity)

        # ---- Resolve profile version ---------------------------------------
        if profile_version is None:
            row = self.conn.execute(
                "SELECT profile_version FROM preference_profile_current WHERE slot='current'"
            ).fetchone()
            if row is None:
                return self._baseline(
                    base_rag_similarity,
                    inactive_reasons={"profile": "no published profile in preference_profile_current"},
                )
            profile_version = row["profile_version"]

        # ---- Load global profile -------------------------------------------
        global_row = self.conn.execute(
            """SELECT liked_centroid_blob, disliked_centroid_blob
               FROM preference_profiles
               WHERE profile_version = ? AND scope = 'global'""",
            (profile_version,),
        ).fetchone()

        if global_row is None:
            return self._baseline(
                base_rag_similarity,
                inactive_reasons={"profile": f"profile_version {profile_version} not found or has no global scope"},
            )

        active_weights: dict[str, float] = {}
        inactive_reasons: dict[str, str] = {}

        # ---- Base RAG similarity (always available) ------------------------
        active_weights["base_rag"] = _NOMINAL_POSITIVE_WEIGHTS["base_rag"]

        # ---- Global like similarity ----------------------------------------
        global_like_sim: float = 0.0
        if global_row["liked_centroid_blob"] is not None:
            liked_centroid = _normalize(
                np.frombuffer(global_row["liked_centroid_blob"], dtype=np.float32)
            )
            global_like_sim = float(np.dot(candidate_vector, liked_centroid))
            active_weights["global_like"] = _NOMINAL_POSITIVE_WEIGHTS["global_like"]
        else:
            inactive_reasons["global_like"] = "no liked centroid available"

        # ---- Global dislike similarity -------------------------------------
        global_dislike_sim: float = 0.0
        if global_row["disliked_centroid_blob"] is not None:
            disliked_centroid = _normalize(
                np.frombuffer(global_row["disliked_centroid_blob"], dtype=np.float32)
            )
            global_dislike_sim = float(np.dot(candidate_vector, disliked_centroid))
            active_weights["global_dislike"] = _NOMINAL_NEGATIVE_WEIGHTS[
                "global_dislike"
            ]
        else:
            inactive_reasons["global_dislike"] = "no disliked centroid available"

        # ---- Scenario like similarity --------------------------------------
        scenario_like_sim: float = 0.0
        if scenario_keys:
            placeholders = ",".join(["?"] * len(scenario_keys))
            scenario_rows = self.conn.execute(
                f"""SELECT scenario_key, liked_centroid_blob
                     FROM preference_profiles
                     WHERE profile_version = ? AND scope = 'scenario'
                       AND scenario_key IN ({placeholders})""",
                (profile_version, *scenario_keys),
            ).fetchall()

            if scenario_rows:
                sims: list[float] = []
                for srow in scenario_rows:
                    if srow["liked_centroid_blob"] is not None:
                        centroid = _normalize(
                            np.frombuffer(
                                srow["liked_centroid_blob"], dtype=np.float32
                            )
                        )
                        sims.append(float(np.dot(candidate_vector, centroid)))
                if sims:
                    scenario_like_sim = sum(sims) / len(sims)
                    active_weights["scenario_like"] = _NOMINAL_POSITIVE_WEIGHTS[
                        "scenario_like"
                    ]
                else:
                    inactive_reasons["scenario_like"] = (
                        "matching scenario profile(s) found but no liked centroids"
                    )
            else:
                inactive_reasons["scenario_like"] = "no matching scenario profiles"
        else:
            inactive_reasons["scenario_like"] = "no scenario keys provided"

        # ---- Renormalize positive weights ----------------------------------
        positive_sum = sum(
            w
            for k, w in active_weights.items()
            if k in _NOMINAL_POSITIVE_WEIGHTS
        )
        if positive_sum > 0:
            for k in list(active_weights.keys()):
                if k in _NOMINAL_POSITIVE_WEIGHTS:
                    active_weights[k] = active_weights[k] / positive_sum

        # ---- Compute raw score ---------------------------------------------
        raw_score: float = 0.0
        if "base_rag" in active_weights:
            raw_score += active_weights["base_rag"] * base_rag_similarity
        if "global_like" in active_weights:
            raw_score += active_weights["global_like"] * global_like_sim
        if "scenario_like" in active_weights:
            raw_score += active_weights["scenario_like"] * scenario_like_sim
        if "global_dislike" in active_weights:
            raw_score -= active_weights["global_dislike"] * global_dislike_sim

        final_score = float(max(0.0, min(1.0, raw_score)))

        # ---- Compute profile_score (preference signal alone) ---------------
        profile_score: float | None = None
        positive_contrib = 0.0
        negative_contrib = 0.0
        if "global_like" in active_weights:
            positive_contrib += active_weights["global_like"] * global_like_sim
        if "scenario_like" in active_weights:
            positive_contrib += active_weights["scenario_like"] * scenario_like_sim
        if "global_dislike" in active_weights:
            negative_contrib += active_weights["global_dislike"] * global_dislike_sim

        has_pref_signal = (
            "global_like" in active_weights
            or "scenario_like" in active_weights
            or "global_dislike" in active_weights
        )
        if has_pref_signal:
            profile_score = positive_contrib - negative_contrib

        return {
            "base_rag_similarity": base_rag_similarity,
            "profile_score": profile_score,
            "raw_score": raw_score,
            "final_score": final_score,
            "active_weights": active_weights,
            "inactive_reasons": inactive_reasons,
            "preference_profile_version": profile_version,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _baseline(
        self,
        base_rag_similarity: float,
        *,
        inactive_reasons: dict[str, str] | None = None,
    ) -> ScoreBreakdown:
        """Return a baseline ScoreBreakdown (no-op path)."""
        return {
            "base_rag_similarity": base_rag_similarity,
            "profile_score": None,
            "raw_score": base_rag_similarity,
            "final_score": base_rag_similarity,
            "active_weights": {},
            "inactive_reasons": inactive_reasons or {},
            "preference_profile_version": None,
        }
