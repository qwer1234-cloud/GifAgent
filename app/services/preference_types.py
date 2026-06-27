from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Rating = Literal["like", "neutral", "dislike", "quality_reject", "skip"]
CandidateStatus = Literal["candidate", "liked", "disliked", "neutral", "promoted", "rejected", "archived"]

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
