"""P3T5/P4T7: Explainable ranking breakdown + Why This explanations.

This module provides:

- ``ScoreBreakdown`` — a TypedDict that labels the *what* of a ranking
  score (descriptive, not causal).
- ``compute_ranking_explanation(...)`` — assembles breakdown from
  PreferenceReranker results.
- ``SelectionExplanation`` — dataclass for "Why This" explanations
  containing Chinese summary text, persisted score components, and
  provenance IDs.
- ``explain_selection(...)`` — reads stored scores and provenance from
  the database to produce a ``SelectionExplanation``.

Both functions answer "what went into it" without claiming causality.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Literal, TypedDict

import numpy as np

from app.services.preference_types import RerankerScoreBreakdown


# ===================================================================
# Why This — SelectionExplanation
# ===================================================================


@dataclass(frozen=True)
class SelectionExplanation:
    """Human-readable "Why This" explanation for a selected candidate.

    Attributes
    ----------
    summary:
        Chinese natural-language summary describing why the candidate
        was selected.  Descriptive only — does not claim causality.
    score_components:
        Key-value pairs of score component names to their stored values
        (e.g. ``{"base_quality": 0.75, "final_score": 0.82}``).
    provenance_ids:
        List of identifiers that contributed to the selection, such as
        profile version, config ID, or pipeline run ID.
    """

    summary: str
    score_components: dict[str, float] = field(default_factory=dict)
    provenance_ids: list[str] = field(default_factory=list)


def explain_selection(
    conn: sqlite3.Connection,
    candidate_id: str,
    *,
    context: Literal["search", "review", "collection"] = "search",
) -> SelectionExplanation:
    """Produce a "Why This" explanation for a candidate.

    Reads score components stored in the ``candidate_gifs`` table and
    provenance metadata from the preference profile.  The returned
    *summary* is a descriptive Chinese sentence — it does not claim
    causality.

    Parameters
    ----------
    conn:
        Read-only SQLite connection (library database).
    candidate_id:
        The candidate being explained.
    context:
        Semantic context for phrasing the explanation.
        ``"search"`` — shown in search results.
        ``"review"`` — shown during active review.
        ``"collection"`` — shown in collection export.

    Returns
    -------
    SelectionExplanation
        Always returned (never ``None``).  When the candidate is not
        found, the summary states that the candidate is unknown and
        score components are empty.
    """
    context_labels = {
        "search": "搜索",
        "review": "审查",
        "collection": "合集",
    }
    ctx_label = context_labels.get(context, "搜索")

    # Read candidate row
    row = conn.execute(
        """SELECT candidate_id, base_rag_similarity, profile_score,
                  final_score, score_profile_version,
                  source_video_path, start_sec, end_sec
           FROM candidate_gifs
           WHERE candidate_id = ?""",
        (candidate_id,),
    ).fetchone()

    if row is None:
        return SelectionExplanation(
            summary=f"未找到候选片段「{candidate_id}」的信息。",
            score_components={},
            provenance_ids=[],
        )

    # Build score components from stored data
    components: dict[str, float] = {}
    if row["base_rag_similarity"] is not None:
        components["base_quality"] = row["base_rag_similarity"]
    if row["profile_score"] is not None:
        components["profile_score"] = row["profile_score"]
    if row["final_score"] is not None:
        components["final_score"] = row["final_score"]

    # Build provenance IDs
    provenance: list[str] = []
    if row["score_profile_version"]:
        provenance.append(row["score_profile_version"])

    # ---- Build Chinese summary -------------------------------------------
    parts: list[str] = [f"在{ctx_label}场景中"]

    if components.get("base_quality") is not None:
        parts.append(f"基础质量分 {components['base_quality']:.2f}")

    if components.get("profile_score") is not None:
        parts.append(f"偏好分 {components['profile_score']:.2f}")

    if components.get("final_score") is not None:
        parts.append(f"最终得分 {components['final_score']:.2f}")

    parts.append("。")

    if provenance:
        parts.append(f"（来源：{'、'.join(provenance)}）")

    summary = "".join(parts)

    return SelectionExplanation(
        summary=summary,
        score_components=components,
        provenance_ids=provenance,
    )


# ===================================================================
# ScoreBreakdown (existing)
# ===================================================================


class ScoreBreakdown(TypedDict):
    """Explanatory breakdown of a single candidate's ranking score.

    All fields are required.  When a component could not be computed
    (e.g. missing profile, missing vector) the corresponding field
    holds ``None`` or an empty list, and the reason is recorded in
    ``inactive_reasons``.

    ``base_quality`` is the raw RAG similarity before preference
    adjustment. ``positive_similarity`` and ``negative_penalty``
    reflect preference-model similarity in ``[-1, 1]`` (higher is
    more similar). ``diversity_adjustment`` and
    ``temporal_coverage_adjustment`` are reserved for future phases
    and are always ``0.0`` for now.
    """

    base_quality: float
    positive_similarity: float | None
    negative_penalty: float | None
    diversity_adjustment: float
    temporal_coverage_adjustment: float
    final_score: float
    nearest_positive_ids: list[str]
    inactive_reasons: dict[str, str]
    preference_profile_version: str | None


def compute_ranking_explanation(
    conn: sqlite3.Connection,
    candidate_id: str,
    candidate_vector: np.ndarray,
    base_rag_similarity: float,
    scenario_keys: list[str],
    profile_version: str | None = None,
    enabled: bool = True,
) -> ScoreBreakdown:
    """Compute an explainable ranking breakdown for *candidate_id*.

    Delegates the core score computation to ``PreferenceReranker`` and
    enriches the result with:

    * ``nearest_positive_ids`` — up to 5 candidate IDs whose vectors
      are most similar to *candidate_vector*, drawn only from
      Like/Favourite events that were included in the published profile.
    * ``diversity_adjustment`` / ``temporal_coverage_adjustment`` —
      placeholders (``0.0``) reserved for future phases.

    Availability errors (missing profile, missing vectors, feature
    disabled) are caught at the service boundary and surfaced through
    ``inactive_reasons`` with an unmodified ``final_score`` equal to
    ``base_rag_similarity``.

    Parameters
    ----------
    conn:
        SQLite connection with the preference schema applied.
    candidate_id:
        The ID of the candidate being explained.
    candidate_vector:
        Normalised float32 embedding of shape ``(768,)``.
    base_rag_similarity:
        The RAG cosine similarity (float in ``[0, 1]``).
    scenario_keys:
        Tags / emotion keys for scenario profile lookup.
    profile_version:
        Explicit profile version, or ``None`` to auto-resolve.
    enabled:
        When ``False`` the reranker is a no-op.

    Returns
    -------
    ScoreBreakdown
        Fully populated explanation dict.

    Raises
    ------
    ValueError
        If *candidate_vector* has the wrong shape (programming error
        **not** caught here).
    """
    from app.services.reranker import PreferenceReranker

    reranker = PreferenceReranker(conn)
    reranker_result: RerankerScoreBreakdown = reranker.score(
        candidate_vector=candidate_vector,
        base_rag_similarity=base_rag_similarity,
        scenario_keys=scenario_keys,
        profile_version=profile_version,
        enabled=enabled,
    )

    effective_pv = reranker_result.get("preference_profile_version")

    # ---- Find nearest positive examples ---------------------------------
    nearest_ids: list[str] = []
    if effective_pv is not None and enabled:
        try:
            nearest_ids = _find_nearest_positive_ids(
                conn=conn,
                candidate_vector=candidate_vector,
                profile_version=effective_pv,
                exclude_candidate_id=candidate_id,
                top_n=5,
            )
        except (sqlite3.Error, ValueError):
            # Availability error at the service boundary — return empty list.
            pass

    positive_sim: float | None = reranker_result.get("positive_similarity")
    negative_sim: float | None = reranker_result.get("negative_similarity")

    return ScoreBreakdown(
        base_quality=reranker_result["base_rag_similarity"],
        positive_similarity=positive_sim,
        negative_penalty=negative_sim,
        diversity_adjustment=0.0,
        temporal_coverage_adjustment=0.0,
        final_score=reranker_result["final_score"],
        nearest_positive_ids=nearest_ids,
        inactive_reasons=reranker_result.get("inactive_reasons", {}),
        preference_profile_version=effective_pv,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _find_nearest_positive_ids(
    conn: sqlite3.Connection,
    candidate_vector: np.ndarray,
    profile_version: str,
    exclude_candidate_id: str,
    top_n: int = 5,
) -> list[str]:
    """Return the *top_n* positive candidate IDs nearest to *candidate_vector*.

    Only candidates with ``like`` or ``favorite`` feedback up to the
    profile's ``event_watermark`` are considered.  *exclude_candidate_id*
    is skipped so that a candidate is not listed as its own neighbour.

    Returns an empty list when the profile, watermark, or vectors are
    unavailable.
    """
    row = conn.execute(
        "SELECT event_watermark FROM preference_profile_builds WHERE profile_version = ?",
        (profile_version,),
    ).fetchone()
    if row is None:
        return []

    watermark: str = row["event_watermark"]

    # Collect positive candidate IDs up to the watermark.
    rows = conn.execute(
        """SELECT DISTINCT e.target_id
           FROM preference_events e
           WHERE e.target_type = 'candidate_gif'
             AND e.rating IN ('like', 'favorite')
             AND e.undone_at IS NULL
             AND e.created_at <= ?
             AND e.target_id != ?
           ORDER BY e.target_id""",
        (watermark, exclude_candidate_id),
    ).fetchall()

    positive_ids = [r["target_id"] for r in rows]
    if not positive_ids:
        return []

    # Fetch vectors for these candidates.
    placeholders = ",".join(["?"] * len(positive_ids))
    vector_rows = conn.execute(
        f"""SELECT cv.candidate_id, cv.vector_blob
             FROM candidate_vectors cv
             WHERE cv.candidate_id IN ({placeholders})
               AND cv.vector_type = 'clip'""",
        positive_ids,
    ).fetchall()

    if not vector_rows:
        return []

    similarities: list[tuple[str, float]] = []
    for vr in vector_rows:
        vec = np.frombuffer(vr["vector_blob"], dtype=np.float32)
        norm = np.linalg.norm(vec)
        if norm > 0:
            vec = vec / norm
        sim = float(np.dot(candidate_vector, vec))
        similarities.append((vr["candidate_id"], sim))

    # Sort descending by similarity, keep top_n.
    similarities.sort(key=lambda x: x[1], reverse=True)
    return [cid for cid, _ in similarities[:top_n]]
