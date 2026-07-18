from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypedDict
from collections.abc import Sequence

# Phase 3: expanded rating set includes "favorite".
FeedbackRating = Literal["like", "dislike", "neutral", "skip", "quality_reject", "favorite"]
EventKind = Literal["feedback", "correction"]

# Legacy alias for callers not yet migrated.
Rating = FeedbackRating

CandidateStatus = Literal["candidate", "liked", "disliked", "neutral", "promoted", "rejected", "archived"]


class ProfileBuildResult(TypedDict, total=False):
    profile_version: str
    event_watermark: str
    effective_feedback_count: int
    status: Literal["built", "blocked"]
    gate_reasons: list[str]


class RerankerScoreBreakdown(TypedDict, total=False):
    base_rag_similarity: float
    profile_score: float | None
    raw_score: float
    final_score: float
    positive_similarity: float | None
    negative_similarity: float | None
    active_weights: dict[str, float]
    inactive_reasons: dict[str, str]
    preference_profile_version: str | None

RATING_TO_STATUS: dict[str, CandidateStatus] = {
    "like": "liked",
    "dislike": "disliked",
    "neutral": "neutral",
    "quality_reject": "rejected",
    # "skip" and "favorite" deliberately omitted — no status change
}


@dataclass(frozen=True)
class ProfileBuildConfig:
    """Controls how preference profiles are built: recency decay, rating
    weights, and scenario thresholds.  Frozen/immutable so callers can
    safely share a single instance across builds."""

    recency_enabled: bool = True
    recency_half_life_days: float = 90.0
    favorite_weight: float = 2.0
    like_weight: float = 1.0
    dislike_weight: float = 1.0
    scenario_min_feedback: int = 8


@dataclass(frozen=True)
class ProfilePreview:
    """Result of ``preview_profile()`` — shows whether a build would succeed
    with the given config without writing any data."""

    profile_version: str
    status: Literal["ready", "blocked"]
    gate_reasons: tuple[str, ...]
    metrics: dict[str, float]


@dataclass(frozen=True)
class MaterializedCandidate:
    candidate_id: str
    source_run_id: str
    source_run_candidate_id: str
    source_video_sha256: str
    start_sec: float
    end_sec: float
    status: CandidateStatus


@dataclass(frozen=True)
class FeedbackEvent:
    event_id: str
    target_type: Literal["media", "candidate_gif"]
    target_id: str
    rating: FeedbackRating
    event_kind: EventKind
    supersedes_event_id: str | None
    source_video_sha256: str
    created_at: str


@dataclass(frozen=True)
class VectorExclusion:
    candidate_id: str
    reason: str
    created_at: str


class BackfillReport(TypedDict, total=False):
    total: int
    inserted: int
    skipped_existing: int
    failed: int
    exclusions: list[dict[str, str]]
    batch_commits: int
