from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Split = Literal["tune", "holdout"]

Choice = Literal["left", "right", "tie", "both_bad"]


@dataclass(frozen=True)
class ABSession:
    session_id: str
    run_a: str
    run_b: str
    seed: int
    status: str  # "active" | "completed"


@dataclass(frozen=True)
class BlindPair:
    pair_index: int
    left_token: str
    right_token: str


@dataclass(frozen=True)
class ABResult:
    session_id: str
    run_a: str
    run_b: str
    config_a: str
    config_b: str
    run_a_wins: int
    run_b_wins: int
    ties: int
    both_bad: int


@dataclass(frozen=True)
class BenchmarkItem:
    item_id: str
    source_path: str
    video_fingerprint: str
    duration_bucket: str
    resolution_bucket: str
    pace_bucket: str
    difficulty_tags: tuple[str, ...]
    split: Split


@dataclass(frozen=True)
class BenchmarkManifest:
    manifest_id: str
    version: int
    items: tuple[BenchmarkItem, ...]


@dataclass(frozen=True)
class ExperimentConfig:
    config_id: str
    config_json: str
    provenance_json: str


@dataclass(frozen=True)
class ExperimentRun:
    run_id: str
    manifest_id: str
    config_id: str
    split: Split
    status: str
