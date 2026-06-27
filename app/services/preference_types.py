from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypedDict

Rating = Literal["like", "neutral", "dislike", "quality_reject", "skip"]
CandidateStatus = Literal["candidate", "liked", "disliked", "neutral", "promoted", "rejected", "archived"]


class ProfileBuildResult(TypedDict, total=False):
    profile_version: str
    event_watermark: str
    effective_feedback_count: int
    status: Literal["built", "blocked"]
    gate_reasons: list[str]


class ScoreBreakdown(TypedDict, total=False):
    base_rag_similarity: float
    profile_score: float | None
    raw_score: float
    final_score: float
    active_weights: dict[str, float]
    inactive_reasons: dict[str, str]
    preference_profile_version: str | None

RATING_TO_STATUS: dict[str, CandidateStatus] = {
    "like": "liked",
    "dislike": "disliked",
    "neutral": "neutral",
    "quality_reject": "rejected",
    # "skip" deliberately omitted — no status change
}


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
    rating: Rating
    source_video_sha256: str
    created_at: str
