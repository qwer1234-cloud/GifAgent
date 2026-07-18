"""Phase 3 Task 3: Active review queue.

Builds a priority-ordered queue of candidate GIFs for human review by
balancing three strategies:

* **exploit**  — candidates with the highest preference scores
* **uncertain** — candidates whose calibrated probability is closest to 0.5
                 (the decision boundary)
* **explore**  — candidates farthest from the exploit-set centroid,
                 favouring source-video diversity
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

import numpy as np

# ── public types ─────────────────────────────────────────────────────────────

ReviewReason = Literal["exploit", "uncertain", "explore"]


@dataclass(frozen=True)
class ReviewQueueItem:
    candidate_id: str
    reason: ReviewReason
    reason_detail: str
    score: float


@dataclass(frozen=True)
class ReviewCandidate:
    candidate_id: str
    source_video_sha256: str
    preference_score: float
    calibrated_probability: float
    vector: np.ndarray
    cluster_key: str


# ── internal constants ───────────────────────────────────────────────────────

_REASON_DETAILS: dict[ReviewReason, str] = {
    "exploit": "偏好得分较高",
    "uncertain": "模型判断最不确定",
    "explore": "探索较少出现的风格",
}


# ── public API ───────────────────────────────────────────────────────────────


def build_review_queue(
    candidates: Sequence[ReviewCandidate],
    *,
    limit: int,
    exploit_ratio: float = 0.70,
    uncertain_ratio: float = 0.20,
    explore_ratio: float = 0.10,
    seed: int,
) -> list[ReviewQueueItem]:
    """Build a priority-ordered review queue from *candidates*.

    Parameters
    ----------
    candidates:
        The pool of candidate GIFs to select from.
    limit:
        Maximum number of items in the returned queue.
    exploit_ratio, uncertain_ratio, explore_ratio:
        Relative proportion to allocate to each strategy.  Must sum to 1.0.
    seed:
        PRNG seed for deterministic tie-breaking.

    Returns
    -------
    list[ReviewQueueItem]
        Queue ordered: exploit (highest score first), uncertain (most
        uncertain first), explore (farthest-first).
    """
    if not candidates or limit <= 0:
        return []

    cand_list = list(candidates)
    actual_limit = min(limit, len(cand_list))
    rng = random.Random(seed)

    # ── initial quotas ────────────────────────────────────────────────────
    q_exploit = max(0, int(actual_limit * exploit_ratio))
    q_uncertain = max(0, int(actual_limit * uncertain_ratio))
    q_explore = actual_limit - q_exploit - q_uncertain

    # ── sorting keys ──────────────────────────────────────────────────────
    exploit_sorted = sorted(cand_list, key=lambda c: -c.preference_score)
    uncertain_sorted = sorted(
        cand_list, key=lambda c: abs(c.calibrated_probability - 0.5)
    )

    # ── exploit selection (highest preference score) ──────────────────────
    selected_ids: set[str] = set()
    exploit_items = _take_until(
        exploit_sorted,
        q_exploit,
        selected_ids,
        "exploit",
        lambda c: c.preference_score,
    )

    # ── uncertain selection (closest to 0.5 decision boundary) ────────────
    uncertain_items = _take_until(
        uncertain_sorted,
        q_uncertain,
        selected_ids,
        "uncertain",
        lambda c: abs(c.calibrated_probability - 0.5),
    )

    # ── explore selection (farthest-first from exploit centroid) ─────────
    exploit_selected = [c for c in cand_list if c.candidate_id in selected_ids]
    remaining = [c for c in cand_list if c.candidate_id not in selected_ids]
    explore_scores = _compute_explore_scores(remaining, exploit_selected)
    explore_items = _select_explore_items(
        remaining, q_explore, explore_scores, selected_ids, rng,
    )

    # ── redistribute shortfall ────────────────────────────────────────────
    total = len(exploit_items) + len(uncertain_items) + len(explore_items)
    shortfall = actual_limit - total
    if shortfall > 0:
        extra = _redistribute_shortfall(
            shortfall, exploit_ratio, uncertain_ratio, explore_ratio,
            len(exploit_items), q_exploit,
            len(uncertain_items), q_uncertain,
            len(explore_items), q_explore,
        )
        if extra["exploit"] > 0:
            extra_items = _take_until(
                exploit_sorted,
                len(exploit_items) + extra["exploit"],
                selected_ids,
                "exploit",
                lambda c: c.preference_score,
                already_have=len(exploit_items),
            )
            exploit_items.extend(extra_items)
        if extra["uncertain"] > 0:
            extra_items = _take_until(
                uncertain_sorted,
                len(uncertain_items) + extra["uncertain"],
                selected_ids,
                "uncertain",
                lambda c: abs(c.calibrated_probability - 0.5),
                already_have=len(uncertain_items),
            )
            uncertain_items.extend(extra_items)
        if extra["explore"] > 0:
            remaining = [c for c in cand_list if c.candidate_id not in selected_ids]
            extra_items = _select_explore_items(
                remaining, extra["explore"], explore_scores, selected_ids, rng,
            )
            explore_items.extend(extra_items)

    return exploit_items + uncertain_items + explore_items


# ── internal helpers ─────────────────────────────────────────────────────────


def _take_until(
    sorted_candidates: list[ReviewCandidate],
    n: int,
    selected_ids: set[str],
    reason: ReviewReason,
    score_fn,
    already_have: int = 0,
) -> list[ReviewQueueItem]:
    """Take the first *n* (minus *already_have*) candidates not yet selected.

    Iterates the already-sorted *sorted_candidates* list, skipping IDs in
    *selected_ids*, and produces ``ReviewQueueItem`` instances until the
    desired count is reached.
    """
    need = n - already_have
    if need <= 0:
        return []

    result: list[ReviewQueueItem] = []
    for c in sorted_candidates:
        if len(result) >= need:
            break
        if c.candidate_id not in selected_ids:
            result.append(ReviewQueueItem(
                candidate_id=c.candidate_id,
                reason=reason,
                reason_detail=_REASON_DETAILS[reason],
                score=score_fn(c),
            ))
            selected_ids.add(c.candidate_id)
    return result


def _compute_explore_scores(
    candidates: list[ReviewCandidate],
    exploit_selected: list[ReviewCandidate],
) -> dict[str, float]:
    """Score remaining candidates by Euclidean distance from exploit centroid.

    For each *cluster_key* that has exploit-selected members, a per-cluster
    centroid is computed.  Candidates are scored by the distance from their
    vector to their own cluster's centroid.  Clusters with no exploit-selected
    members fall back to the global centroid of *all* exploit-selected vectors.
    """
    if not exploit_selected:
        return {c.candidate_id: 0.0 for c in candidates}

    # Per-cluster centroids from exploit-selected candidates.
    cluster_vectors: dict[str, list[np.ndarray]] = {}
    for c in exploit_selected:
        cluster_vectors.setdefault(c.cluster_key, []).append(c.vector)

    centroids: dict[str, np.ndarray] = {}
    for key, vecs in cluster_vectors.items():
        centroids[key] = np.mean(vecs, axis=0).astype(np.float32)

    # Global fallback centroid.
    all_vecs = [v for vecs in cluster_vectors.values() for v in vecs]
    global_centroid: np.ndarray = (
        np.mean(all_vecs, axis=0).astype(np.float32) if all_vecs
        else np.zeros(768, dtype=np.float32)
    )

    scores: dict[str, float] = {}
    for c in candidates:
        centroid = centroids.get(c.cluster_key, global_centroid)
        delta = c.vector.astype(np.float32) - centroid
        scores[c.candidate_id] = float(np.sqrt(np.dot(delta, delta)))
    return scores


def _select_explore_items(
    pool: list[ReviewCandidate],
    n: int,
    explore_scores: dict[str, float],
    selected_ids: set[str],
    rng: random.Random,
) -> list[ReviewQueueItem]:
    """Select *n* explore items via farthest-first + source-video diversity.

    Iteratively picks the candidate with the highest explore score that
    also introduces a new ``source_video_sha256`` when possible.
    """
    if n <= 0 or not pool:
        return []

    remaining = [c for c in pool if c.candidate_id not in selected_ids]
    result: list[ReviewQueueItem] = []
    seen_videos: set[str] = set()

    for _ in range(n):
        if not remaining:
            break

        # Sort by: (video novelty, explore score, random tiebreaker)
        remaining.sort(
            key=lambda c: (
                1 if c.source_video_sha256 in seen_videos else 2,
                explore_scores.get(c.candidate_id, 0.0),
                rng.random(),
            ),
            reverse=True,
        )
        best = remaining.pop(0)

        result.append(ReviewQueueItem(
            candidate_id=best.candidate_id,
            reason="explore",
            reason_detail=_REASON_DETAILS["explore"],
            score=explore_scores.get(best.candidate_id, 0.0),
        ))
        selected_ids.add(best.candidate_id)
        seen_videos.add(best.source_video_sha256)

    return result


def _redistribute_shortfall(
    shortfall: int,
    exploit_ratio: float,
    uncertain_ratio: float,
    explore_ratio: float,
    got_exploit: int,
    quota_exploit: int,
    got_uncertain: int,
    quota_uncertain: int,
    got_explore: int,
    quota_explore: int,
) -> dict[str, int]:
    """Distribute *shortfall* slots proportionally to buckets that fell short.

    If all buckets met their quotas the shortfall is split among all three
    buckets using their original ratios.
    """
    needs: dict[str, float] = {}
    if got_exploit < quota_exploit:
        needs["exploit"] = exploit_ratio
    if got_uncertain < quota_uncertain:
        needs["uncertain"] = uncertain_ratio
    if got_explore < quota_explore:
        needs["explore"] = explore_ratio

    # If no bucket was short (e.g., we exhausted the pool) split by ratio.
    if not needs:
        needs = {
            "exploit": exploit_ratio,
            "uncertain": uncertain_ratio,
            "explore": explore_ratio,
        }

    total_ratio = sum(needs.values())
    result: dict[str, int] = {"exploit": 0, "uncertain": 0, "explore": 0}
    assigned = 0
    for bucket in ("exploit", "uncertain", "explore"):
        if bucket in needs:
            result[bucket] = int(shortfall * needs[bucket] / total_ratio)
            assigned += result[bucket]

    # Distribute any rounding remainder, prioritising the buckets that needed it.
    remainder = shortfall - assigned
    for bucket in ("exploit", "uncertain", "explore"):
        if remainder <= 0:
            break
        if bucket in needs:
            result[bucket] += 1
            remainder -= 1

    return result
