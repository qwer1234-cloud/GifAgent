"""JSON-safe immutable value objects shared by quality MoE components."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import math
from types import MappingProxyType
from typing import Any, Mapping


class EvidenceStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
    ABSTAINED = "ABSTAINED"
    INVALID = "INVALID"


class QualityDecision(str, Enum):
    KEEP_AS_IS = "KEEP_AS_IS"
    KEEP_FOR_REPAIR = "KEEP_FOR_REPAIR"
    REVIEW = "REVIEW"
    REJECT = "REJECT"
    ABSTAIN = "ABSTAIN"


def _json_value(value: object, *, field_name: str) -> object:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{field_name} must be JSON-safe")
        return value
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _json_value(item, field_name=field_name) for key, item in value.items()})
    if isinstance(value, (tuple, list)):
        return tuple(_json_value(item, field_name=field_name) for item in value)
    raise ValueError(f"{field_name} must be JSON-safe")


def _as_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _as_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_as_json(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    return value


def _score(value: object, *, field_name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a finite score")
    parsed = float(value)
    if not math.isfinite(parsed) or not 0.0 <= parsed <= 1.0:
        raise ValueError(f"{field_name} must be in [0, 1]")
    return parsed


@dataclass(frozen=True)
class ExpertEvidence:
    candidate_id: str
    evaluation_version: str
    expert_id: str
    expert_version: str
    signal_family: str
    status: EvidenceStatus
    scores: Mapping[str, float] = field(default_factory=dict)
    findings: tuple[Mapping[str, object], ...] = ()
    summary: str = ""
    input_hash: str = ""
    config_hash: str = ""
    prompt_hash: str | None = None
    latency_ms: int = 0

    def __post_init__(self) -> None:
        for field_name in (
            "candidate_id", "evaluation_version", "expert_id", "expert_version", "signal_family", "summary", "input_hash", "config_hash"
        ):
            if not isinstance(getattr(self, field_name), str):
                raise ValueError(f"{field_name} must be a string")
        if self.prompt_hash is not None and not isinstance(self.prompt_hash, str):
            raise ValueError("prompt_hash must be a string or None")
        if isinstance(self.latency_ms, bool) or not isinstance(self.latency_ms, int) or self.latency_ms < 0:
            raise ValueError("latency_ms must be a non-negative integer")
        object.__setattr__(self, "status", EvidenceStatus(self.status))
        object.__setattr__(self, "scores", MappingProxyType({
            str(key): _score(value, field_name=f"scores.{key}")
            for key, value in self.scores.items()
        }))
        object.__setattr__(self, "findings", tuple(
            _json_value(finding, field_name="findings")
            for finding in self.findings
        ))

    def available_scores(self) -> dict[str, float]:
        return dict(self.scores) if self.status is EvidenceStatus.AVAILABLE else {}

    def to_dict(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "evaluation_version": self.evaluation_version,
            "expert_id": self.expert_id,
            "expert_version": self.expert_version,
            "signal_family": self.signal_family,
            "status": self.status.value,
            "scores": dict(self.scores),
            "findings": _as_json(self.findings),
            "summary": self.summary,
            "input_hash": self.input_hash,
            "config_hash": self.config_hash,
            "prompt_hash": self.prompt_hash,
            "latency_ms": self.latency_ms,
        }


@dataclass(frozen=True)
class RepairRecipe:
    recipe_id: str
    exposure_ev: float = 0.0
    gamma: float = 1.0
    contrast: float = 0.0
    shadows: float = 0.0
    highlights: float = 0.0
    white_balance: tuple[float, float, float] = (1.0, 1.0, 1.0)
    crop: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0)
    zoom: float = 1.0
    rotation_degrees: float = 0.0
    perspective_corner_movement: float = 0.0
    quality_gain: float = 0.0
    confidence: float = 0.0
    validated: bool = False

    def __post_init__(self) -> None:
        try:
            crop = tuple(float(value) for value in self.crop)
            white_balance = tuple(float(value) for value in self.white_balance)
        except (TypeError, ValueError) as exc:
            raise ValueError("recipe geometry values must be numeric") from exc
        if len(crop) != 4:
            raise ValueError("crop must contain four values")
        object.__setattr__(self, "crop", crop)
        object.__setattr__(self, "white_balance", white_balance)

    def validate(self) -> "RepairRecipe":
        if not self.recipe_id:
            raise ValueError("recipe_id must not be empty")
        numeric = {
            "exposure_ev": self.exposure_ev,
            "gamma": self.gamma,
            "contrast": self.contrast,
            "shadows": self.shadows,
            "highlights": self.highlights,
            "zoom": self.zoom,
            "rotation_degrees": self.rotation_degrees,
            "perspective_corner_movement": self.perspective_corner_movement,
            "quality_gain": self.quality_gain,
            "confidence": self.confidence,
        }
        if any(isinstance(value, bool) or not math.isfinite(float(value)) for value in numeric.values()):
            raise ValueError("recipe values must be finite")
        x, y, width, height = self.crop
        if not all(math.isfinite(float(value)) for value in self.crop) or min(width, height) <= 0 or x < 0 or y < 0 or x + width > 1 or y + height > 1 or width * height < 0.70:
            raise ValueError("crop area must retain at least 70% of the source")
        if not 1.0 <= self.zoom <= 1.25:
            raise ValueError("zoom must be in [1.0, 1.25]")
        if abs(self.rotation_degrees) > 2.0:
            raise ValueError("rotation_degrees must not exceed 2")
        if not 0.0 <= self.perspective_corner_movement <= 0.02:
            raise ValueError("perspective_corner_movement must not exceed 0.02")
        if len(self.white_balance) != 3 or any(not math.isfinite(float(value)) or not 0.92 <= float(value) <= 1.08 for value in self.white_balance):
            raise ValueError("white_balance must stay within 8% per channel")
        if not -0.75 <= self.exposure_ev <= 0.75 or not 0.85 <= self.gamma <= 1.15:
            raise ValueError("photometric recipe values are outside approved bounds")
        if abs(self.contrast) > 0.10 or abs(self.shadows) > 0.15 or abs(self.highlights) > 0.15:
            raise ValueError("photometric recipe values are outside approved bounds")
        if not 0.0 <= self.quality_gain <= 1.0 or not 0.0 <= self.confidence <= 1.0:
            raise ValueError("repair gain and confidence must be in [0, 1]")
        return self

    def to_dict(self) -> dict[str, object]:
        self.validate()
        return {
            "recipe_id": self.recipe_id,
            "exposure_ev": self.exposure_ev,
            "gamma": self.gamma,
            "contrast": self.contrast,
            "shadows": self.shadows,
            "highlights": self.highlights,
            "white_balance": list(self.white_balance),
            "crop": list(self.crop),
            "zoom": self.zoom,
            "rotation_degrees": self.rotation_degrees,
            "perspective_corner_movement": self.perspective_corner_movement,
            "quality_gain": self.quality_gain,
            "confidence": self.confidence,
            "validated": self.validated,
        }


@dataclass(frozen=True)
class QualityAssessment:
    decision: QualityDecision
    confidence: float
    negative_signal_families: tuple[str, ...] = ()
    hard_reasons: tuple[str, ...] = ()
    repair: RepairRecipe | None = None
    reason_codes: tuple[str, ...] = ()
    summary: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "decision", QualityDecision(self.decision))
        object.__setattr__(self, "confidence", _score(self.confidence, field_name="confidence"))
        object.__setattr__(self, "negative_signal_families", tuple(dict.fromkeys(
            family for family in self.negative_signal_families if isinstance(family, str) and family
        )))
        object.__setattr__(self, "hard_reasons", tuple(dict.fromkeys(self.hard_reasons)))
        object.__setattr__(self, "reason_codes", tuple(dict.fromkeys(
            code for code in self.reason_codes if isinstance(code, str) and code
        )))
        if not isinstance(self.summary, str):
            raise ValueError("summary must be a string")

    def to_dict(self) -> dict[str, object]:
        return {
            "decision": self.decision.value,
            "confidence": self.confidence,
            "negative_signal_families": list(self.negative_signal_families),
            "hard_reasons": list(self.hard_reasons),
            "repair": self.repair.to_dict() if self.repair else None,
            "reason_codes": list(self.reason_codes),
            "summary": self.summary,
        }
