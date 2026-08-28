"""P1-6: PreferenceReranker — availability-aware reranking behind a feature flag.

Consumes the current published preference profile (if any) and adjusts
candidate scores by measuring cosine similarity to liked/disliked centroids.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

import numpy as np

from app.services.preference_types import RerankerScoreBreakdown
from app.services.vector_math import l2_normalize, max_cosine

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

TAG_PRIOR_WEIGHT = 0.05


def blend_export_scores(
    base_score: float,
    preference_score: float,
    base_score_weight: float,
    preference_score_weight: float,
) -> float:
    """Blend two normalized export scores using their configured proportions."""
    if base_score_weight < 0 or preference_score_weight < 0:
        raise ValueError("export score weights must be non-negative")
    weight_total = base_score_weight + preference_score_weight
    if weight_total <= 0:
        raise ValueError("at least one export score weight must be positive")
    return float(
        (base_score * base_score_weight + preference_score * preference_score_weight)
        / weight_total
    )


def clip_scenario_keys(clip: dict[str, Any], *, max_tags: int = 5) -> list[str]:
    """Map a clip's emotion and tags into reranker scenario keys."""
    keys: list[str] = []
    seen: set[str] = set()
    frame = clip.get("best_frame") if isinstance(clip.get("best_frame"), dict) else {}
    emotion = str(frame.get("emotional_core") or clip.get("emotional_core") or "").strip()
    if emotion and emotion != "?":
        key = f"emotion:{emotion}"
        keys.append(key)
        seen.add(key)
    tags = clip.get("tags") or frame.get("tags") or []
    if isinstance(tags, str):
        tags = [tags]
    if not isinstance(tags, list):
        tags = []
    for tag in tags[:max_tags]:
        name = str(tag).strip()
        if not name:
            continue
        key = name if name.startswith("tag:") else f"tag:{name}"
        if key not in seen:
            keys.append(key)
            seen.add(key)
    return keys


def _tag_prior_score(
    scenario_keys: list[str], tag_weights: dict[str, Any]
) -> float | None:
    """Mean signed tag weight for keys that appear in the published profile."""
    if not scenario_keys or not tag_weights:
        return None
    matched: list[float] = []
    for key in scenario_keys:
        value = tag_weights.get(key)
        if value is None:
            bare = key[4:] if key.startswith("tag:") else key
            value = tag_weights.get(bare)
        if value is None:
            continue
        try:
            matched.append(float(value))
        except (TypeError, ValueError):
            continue
    if not matched:
        return None
    return float(sum(matched) / len(matched))


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
    ) -> RerankerScoreBreakdown:
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

        candidate_vector = l2_normalize(candidate_vector)

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
            """SELECT liked_centroid_blob, disliked_centroid_blob,
                      confidence, tag_weights_json
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
        global_confidence = float(global_row["confidence"] or 0.0)
        try:
            tag_weights = json.loads(global_row["tag_weights_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            tag_weights = {}
        if not isinstance(tag_weights, dict):
            tag_weights = {}

        # ---- Base RAG similarity (always available) ------------------------
        active_weights["base_rag"] = _NOMINAL_POSITIVE_WEIGHTS["base_rag"]

        # ---- Global like similarity ----------------------------------------
        global_like_sim: float = 0.0
        if global_row["liked_centroid_blob"] is not None:
            global_like_sim = max_cosine(
                candidate_vector, global_row["liked_centroid_blob"]
            )
            active_weights["global_like"] = (
                _NOMINAL_POSITIVE_WEIGHTS["global_like"] * global_confidence
            )
        else:
            inactive_reasons["global_like"] = "no liked centroid available"

        # ---- Global dislike similarity -------------------------------------
        global_dislike_sim: float = 0.0
        if global_row["disliked_centroid_blob"] is not None:
            global_dislike_sim = max_cosine(
                candidate_vector, global_row["disliked_centroid_blob"]
            )
            active_weights["global_dislike"] = (
                _NOMINAL_NEGATIVE_WEIGHTS["global_dislike"] * global_confidence
            )
        else:
            inactive_reasons["global_dislike"] = "no disliked centroid available"

        # ---- Scenario like similarity --------------------------------------
        scenario_like_sim: float = 0.0
        scenario_confidence_mean = 0.0
        if scenario_keys:
            placeholders = ",".join(["?"] * len(scenario_keys))
            scenario_rows = self.conn.execute(
                f"""SELECT scenario_key, liked_centroid_blob, confidence
                     FROM preference_profiles
                     WHERE profile_version = ? AND scope = 'scenario'
                       AND scenario_key IN ({placeholders})""",
                (profile_version, *scenario_keys),
            ).fetchall()

            if scenario_rows:
                weighted_sims: list[tuple[float, float]] = []
                for srow in scenario_rows:
                    if srow["liked_centroid_blob"] is not None:
                        sim = max_cosine(
                            candidate_vector, srow["liked_centroid_blob"]
                        )
                        conf = float(srow["confidence"] or 0.0)
                        weighted_sims.append((sim, conf))
                conf_sum = sum(conf for _sim, conf in weighted_sims)
                if weighted_sims and conf_sum > 0:
                    scenario_like_sim = (
                        sum(sim * conf for sim, conf in weighted_sims) / conf_sum
                    )
                    scenario_confidence_mean = conf_sum / len(weighted_sims)
                    active_weights["scenario_like"] = (
                        _NOMINAL_POSITIVE_WEIGHTS["scenario_like"]
                        * scenario_confidence_mean
                    )
                elif weighted_sims:
                    scenario_like_sim = sum(sim for sim, _c in weighted_sims) / len(
                        weighted_sims
                    )
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

        tag_prior = _tag_prior_score(scenario_keys, tag_weights)
        if tag_prior is not None:
            raw_score += TAG_PRIOR_WEIGHT * tag_prior

        final_score = float(max(0.0, min(1.0, raw_score)))

        # ---- Compute normalized preference score (preference signal only) --
        profile_score: float | None = None
        preference_components: list[tuple[float, float]] = []
        if "global_like" in active_weights:
            preference_components.append(
                (_NOMINAL_POSITIVE_WEIGHTS["global_like"], (global_like_sim + 1.0) / 2.0)
            )
        if "scenario_like" in active_weights:
            preference_components.append(
                (_NOMINAL_POSITIVE_WEIGHTS["scenario_like"], (scenario_like_sim + 1.0) / 2.0)
            )
        if "global_dislike" in active_weights:
            preference_components.append(
                (_NOMINAL_NEGATIVE_WEIGHTS["global_dislike"], (1.0 - global_dislike_sim) / 2.0)
            )
        if preference_components:
            preference_weight_total = sum(weight for weight, _ in preference_components)
            profile_score = float(
                max(
                    0.0,
                    min(
                        1.0,
                        sum(weight * score for weight, score in preference_components)
                        / preference_weight_total,
                    ),
                )
            )

        # ---- Compute positive / negative similarity for explanations -----
        positive_similarity: float | None = None
        positive_components: list[tuple[float, float]] = []
        if "global_like" in active_weights:
            positive_components.append(
                (_NOMINAL_POSITIVE_WEIGHTS["global_like"], global_like_sim)
            )
        if "scenario_like" in active_weights:
            positive_components.append(
                (_NOMINAL_POSITIVE_WEIGHTS["scenario_like"], scenario_like_sim)
            )
        if positive_components:
            w_total = sum(w for w, _ in positive_components)
            positive_similarity = float(
                sum(w * v for w, v in positive_components) / w_total
            )

        negative_similarity: float | None = (
            float(global_dislike_sim)
            if "global_dislike" in active_weights
            else None
        )

        return {
            "base_rag_similarity": base_rag_similarity,
            "profile_score": profile_score,
            "raw_score": raw_score,
            "final_score": final_score,
            "positive_similarity": positive_similarity,
            "negative_similarity": negative_similarity,
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
    ) -> RerankerScoreBreakdown:
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
