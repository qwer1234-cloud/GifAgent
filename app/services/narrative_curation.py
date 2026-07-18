"""Phase 4 Task 7: Narrative Curation — beat-based candidate selection.

``curate_narrative`` assigns candidates from a pool to a sequence of narrative
beats (e.g. opening, development, climax, ending).  It scores each candidate
on beat-tag fit, quality, preference, diversity, and temporal order, then
greedily assigns the best available candidate to each beat.

Every requested beat receives either a selection or an explicit ``missing_reason``.
No candidate is reused across beats.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Optional

import numpy as np


@dataclass(frozen=True)
class CurationCandidate:
    """A candidate GIF available for narrative curation.

    Attributes
    ----------
    candidate_id:
        Unique identifier for the candidate.
    source_video:
        Identifier of the source video (used for diversity scoring).
    start_time:
        Start time in seconds within the source video.
    beat_scores:
        Pre-computed scores for how well this candidate fits each beat.
        Keys are beat names (e.g. ``"opening"``), values are floats in
        ``[0, 1]`` (higher = better fit).
    quality:
        Base quality score (e.g. from VLM or heuristic).
    preference:
        Preference-model score (from reranker).
    vector:
        Embedding vector used for diversity measurement.
    """

    candidate_id: str
    source_video: str
    start_time: float
    beat_scores: dict[str, float] = field(default_factory=dict)
    quality: float = 0.0
    preference: float = 0.0
    vector: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.float32))


@dataclass(frozen=True)
class CuratedBeat:
    """Result of selecting a candidate for a narrative beat.

    Attributes
    ----------
    beat:
        Name of the narrative beat.
    selected_candidate_id:
        The chosen candidate ID, or ``None`` when no candidate matches.
    component_scores:
        Breakdown of how the candidate scored on each dimension.
    missing_reason:
        Human-readable reason when *selected_candidate_id* is ``None``.
    """

    beat: str
    selected_candidate_id: str | None
    component_scores: dict[str, float] = field(default_factory=dict)
    missing_reason: str | None = None


DEFAULT_BEATS: tuple[str, ...] = ("opening", "development", "climax", "ending")


def curate_narrative(
    candidates: Sequence[CurationCandidate],
    beats: Sequence[str] = DEFAULT_BEATS,
) -> list[CuratedBeat]:
    """Select up to one candidate per narrative beat from *candidates*.

    The algorithm is a **greedy assignment**: beats are processed in order,
    and for each beat the highest-scoring remaining candidate is selected.

    Score formula (per candidate per beat)::

        combined = (
            0.40 * beat_fit +
            0.30 * norm_quality +
            0.20 * norm_preference +
            0.10 * diversity_bonus
        )

    where *norm_quality* and *norm_preference* are min-max scaled across
    the candidate pool, and *diversity_bonus* rewards candidates whose
    source video has not yet appeared in the selection.

    Parameters
    ----------
    candidates:
        Pool of available candidates.
    beats:
        Ordered sequence of beat names.  Defaults to ``("opening",
        "development", "climax", "ending")``.

    Returns
    -------
    list[CuratedBeat]
        One entry per beat, in the same order as *beats*.  A beat whose
        *selected_candidate_id* is ``None`` also carries a ``missing_reason``.
    """
    # --- handle empty pool ---
    if not candidates:
        return [
            CuratedBeat(
                beat=b,
                selected_candidate_id=None,
                component_scores={},
                missing_reason="No candidates available for curation.",
            )
            for b in beats
        ]

    # --- precompute normalised quality & preference ---
    qualities = np.array([c.quality for c in candidates], dtype=np.float64)
    preferences = np.array([c.preference for c in candidates], dtype=np.float64)

    q_min, q_max = float(qualities.min()), float(qualities.max())
    p_min, p_max = float(preferences.min()), float(preferences.max())

    def _norm(value: float, vmin: float, vmax: float) -> float:
        if vmax - vmin < 1e-12:
            return 0.5  # all equal → neutral
        return (value - vmin) / (vmax - vmin)

    # --- greedy assignment ---
    selected_ids: set[str] = set()
    selected_videos: set[str] = set()
    result: list[CuratedBeat] = []

    for beat in beats:
        best_idx: int | None = None
        best_combined = -float("inf")
        best_components: dict[str, float] = {}

        for i, cand in enumerate(candidates):
            if cand.candidate_id in selected_ids:
                continue

            # Score components
            beat_fit = cand.beat_scores.get(beat, 0.0)
            norm_q = _norm(cand.quality, q_min, q_max)
            norm_p = _norm(cand.preference, p_min, p_max)
            diversity_bonus = 0.1 if cand.source_video not in selected_videos else 0.0

            combined = (
                0.40 * beat_fit
                + 0.30 * norm_q
                + 0.20 * norm_p
                + 0.10 * diversity_bonus
            )

            if combined > best_combined:
                best_combined = combined
                best_idx = i
                best_components = {
                    "beat_fit": beat_fit,
                    "quality": cand.quality,
                    "preference": cand.preference,
                    "diversity_bonus": diversity_bonus,
                }

        if best_idx is not None and best_idx >= 0:
            cand = candidates[best_idx]
            selected_ids.add(cand.candidate_id)
            selected_videos.add(cand.source_video)
            result.append(
                CuratedBeat(
                    beat=beat,
                    selected_candidate_id=cand.candidate_id,
                    component_scores=best_components,
                )
            )
        else:
            result.append(
                CuratedBeat(
                    beat=beat,
                    selected_candidate_id=None,
                    component_scores={},
                    missing_reason=(
                        f"No remaining candidate suitable for beat '{beat}'."
                    ),
                )
            )

    return result
