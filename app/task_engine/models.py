from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

JobStatus = Literal[
    "pending", "leased", "running", "succeeded",
    "retry_wait", "needs_attention", "cancelled",
]

StageName = Literal[
    "discover", "sample", "vlm", "refine", "synthesize",
    "rank_dedup", "gif_clip", "materialize",
]


@dataclass(frozen=True)
class ArtifactRef:
    artifact_id: str
    job_id: str
    video_id: str
    stage_name: StageName
    clip_id: str | None
    path: str
    sha256: str
    size_bytes: int
    provenance_json: str
    stage_id: str = ""
    artifact_kind: str = "generic"


@dataclass(frozen=True)
class CreateJob:
    directory: str
    config_json: str
    limit: int = 0
    extensions: str = ""


@dataclass(frozen=True)
class JobRecord:
    job_id: str
    directory: str
    status: JobStatus


@dataclass(frozen=True)
class VideoRecord:
    video_id: str
    job_id: str
    path: str
    fingerprint: str
    status: JobStatus


@dataclass(frozen=True)
class StageRecord:
    stage_id: str
    video_id: str
    stage_name: StageName
    clip_id: str | None
    status: JobStatus
    attempt_count: int


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay_seconds: int = 5
    max_delay_seconds: int = 300


@dataclass(frozen=True)
class StageError:
    code: str
    message: str
    transient: bool


@dataclass(frozen=True)
class TaskEvent:
    event_id: int
    kind: str
    payload: dict
    created_at: str = ""
