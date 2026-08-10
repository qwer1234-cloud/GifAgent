"""Strict, environment-independent quality MoE configuration freezing."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from types import MappingProxyType
from typing import Any, Mapping


def _mapping(value: object, *, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a mapping")
    return value


def _finite(value: object, *, name: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be finite")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be finite") from exc
    if not math.isfinite(parsed) or not minimum <= parsed <= maximum:
        raise ValueError(f"{name} must be in [{minimum}, {maximum}]")
    return parsed


def _bool(value: object, *, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value


def _positive_int(value: object, *, name: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError(f"{name} must be an integer in [1, {maximum}]")
    return value


def _string(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _strict_merge(defaults: Mapping[str, Any], values: Mapping[str, Any], *, name: str) -> dict[str, Any]:
    unknown = set(values) - set(defaults)
    if unknown:
        raise ValueError(f"{name} has unknown fields: {', '.join(sorted(unknown))}")
    return {key: values.get(key, default) for key, default in defaults.items()}


def _ensure_quality_runtime_ready(runtime_config):
    from app.services.ollama_runtime import ensure_runtime_ready

    return ensure_runtime_ready(runtime_config)


def freeze_quality_runtime_config(
    config_data: Mapping[str, Any],
    *,
    ready_resolver=None,
) -> dict[str, Any]:
    """Resolve quality/VLM sentinel URLs once for immutable job storage."""
    snapshot = json.loads(json.dumps(dict(config_data)))
    quality = snapshot.get("quality_moe")
    judge = quality.get("judge") if isinstance(quality, dict) else None
    if not isinstance(judge, dict):
        return snapshot
    judge_base = str(judge.get("base_url", "") or "").strip()
    vlm = snapshot.get("vlm")
    vlm = vlm if isinstance(vlm, dict) else {}
    vlm_base = str(vlm.get("base_url", "") or "").strip()
    sentinel = judge_base.lower() in {"inherit_vlm", "auto"}
    vlm_auto = vlm_base.lower() == "auto"

    from app.services.ollama_runtime import (
        EmbeddingRuntimeConfig,
        normalize_base_url,
    )

    if not sentinel and not vlm_auto:
        if judge_base:
            judge["base_url"] = normalize_base_url(judge_base)
        return snapshot

    embedding = snapshot.get("embedding")
    embedding = embedding if isinstance(embedding, dict) else {}
    runtime_config = EmbeddingRuntimeConfig(
        base_url=(
            str(embedding.get("base_url", "auto") or "auto")
            if vlm_auto
            else vlm_base
        ),
        manage_lifecycle=bool(
            embedding.get("manage_lifecycle", vlm.get("manage_lifecycle", False))
        ),
        launch_mode=str(
            embedding.get("launch_mode", vlm.get("launch_mode", "wsl")) or "wsl"
        ).lower(),
        wsl_distro=str(embedding.get("wsl_distro", "Ubuntu-20.04")),
        startup_timeout_s=float(embedding.get("startup_timeout_s", 120.0)),
        request_timeout_s=float(embedding.get("request_timeout_s", 60.0)),
        retry_attempts=int(embedding.get("retry_attempts", 3)),
        retry_backoff_s=float(embedding.get("retry_backoff_s", 2.0)),
        keep_alive=str(embedding.get("keep_alive", "30m")),
        embedding_model=str(
            embedding.get("text_model", "nomic-embed-text:latest")
        ),
        embedding_dim=int(embedding.get("embedding_dim", 768)),
    )
    resolver = ready_resolver or _ensure_quality_runtime_ready
    state = resolver(runtime_config)
    resolved_base = normalize_base_url(str(state.base_url))
    if not resolved_base.startswith(("http://", "https://")):
        raise ValueError("resolved quality judge base_url must be absolute")
    judge["base_url"] = resolved_base
    if isinstance(snapshot.get("vlm"), dict):
        snapshot["vlm"]["base_url"] = resolved_base
    return snapshot


@dataclass(frozen=True)
class SoftRejectConfig:
    min_judge_confidence: float = 0.80
    min_independent_negative_families: int = 2

    def to_dict(self) -> dict[str, object]:
        return {
            "min_judge_confidence": self.min_judge_confidence,
            "min_independent_negative_families": self.min_independent_negative_families,
        }


@dataclass(frozen=True)
class RepairabilityConfig:
    enabled: bool = True
    max_proxy_variants: int = 12
    min_quality_gain: float = 0.15
    min_confidence: float = 0.80
    photometric_mode: str = "clip_global"
    geometric_mode: str = "fixed_or_validated_smooth"

    def to_dict(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "max_proxy_variants": self.max_proxy_variants,
            "min_quality_gain": self.min_quality_gain,
            "min_confidence": self.min_confidence,
            "photometric_mode": self.photometric_mode,
            "geometric_mode": self.geometric_mode,
        }


@dataclass(frozen=True)
class QualityMoeConfig:
    enabled: bool
    report_only: bool
    evaluation_version: str
    soft_reject: SoftRejectConfig
    repairability: RepairabilityConfig
    experts: Mapping[str, Mapping[str, object]]
    judge: Mapping[str, object]
    config_hash: str

    @classmethod
    def defaults(cls) -> "QualityMoeConfig":
        return cls.from_mapping({})

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any] | None) -> "QualityMoeConfig":
        root = _mapping(mapping or {}, name="configuration")
        raw = root.get("quality_moe", {})
        quality = _mapping(raw, name="quality_moe")
        quality_defaults: dict[str, Any] = {
            "enabled": True,
            "report_only": True,
            "evaluation_version": "quality-moe-v1",
            "soft_reject": {},
            "experts": {},
            "judge": {},
            "repairability": {},
        }
        values = _strict_merge(quality_defaults, quality, name="quality_moe")
        soft_values = _strict_merge(
            {"min_judge_confidence": 0.80, "min_independent_negative_families": 2},
            _mapping(values["soft_reject"], name="quality_moe.soft_reject"),
            name="quality_moe.soft_reject",
        )
        repair_values = _strict_merge(
            {
                "enabled": True,
                "max_proxy_variants": 12,
                "min_quality_gain": 0.15,
                "min_confidence": 0.80,
                "photometric_mode": "clip_global",
                "geometric_mode": "fixed_or_validated_smooth",
            },
            _mapping(values["repairability"], name="quality_moe.repairability"),
            name="quality_moe.repairability",
        )
        soft_reject = SoftRejectConfig(
            min_judge_confidence=_finite(soft_values["min_judge_confidence"], name="min_judge_confidence", minimum=0.80, maximum=1.0),
            min_independent_negative_families=_positive_int(soft_values["min_independent_negative_families"], name="min_independent_negative_families", maximum=5),
        )
        if soft_reject.min_independent_negative_families < 2:
            raise ValueError("min_independent_negative_families must be at least 2")
        repairability = RepairabilityConfig(
            enabled=_bool(repair_values["enabled"], name="repairability.enabled"),
            max_proxy_variants=_positive_int(repair_values["max_proxy_variants"], name="max_proxy_variants", maximum=12),
            min_quality_gain=_finite(repair_values["min_quality_gain"], name="min_quality_gain", minimum=0.15, maximum=1.0),
            min_confidence=_finite(repair_values["min_confidence"], name="min_confidence", minimum=0.80, maximum=1.0),
            photometric_mode=_string(repair_values["photometric_mode"], name="photometric_mode"),
            geometric_mode=_string(repair_values["geometric_mode"], name="geometric_mode"),
        )
        if repairability.photometric_mode != "clip_global":
            raise ValueError("photometric_mode must be clip_global")
        if repairability.geometric_mode != "fixed_or_validated_smooth":
            raise ValueError(
                "geometric_mode must be fixed_or_validated_smooth"
            )
        experts = _freeze_mapping(values["experts"], name="quality_moe.experts")
        judge = _freeze_primitive_mapping(
            values["judge"], name="quality_moe.judge"
        )
        resolved = {
            "enabled": _bool(values["enabled"], name="enabled"),
            "report_only": _bool(values["report_only"], name="report_only"),
            "evaluation_version": _string(values["evaluation_version"], name="evaluation_version"),
            "soft_reject": soft_reject.to_dict(),
            "experts": _plain_mapping(experts),
            "judge": dict(judge),
            "repairability": repairability.to_dict(),
        }
        config_hash = hashlib.sha256(json.dumps(resolved, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()
        return cls(
            enabled=resolved["enabled"],
            report_only=resolved["report_only"],
            evaluation_version=resolved["evaluation_version"],
            soft_reject=soft_reject,
            repairability=repairability,
            experts=experts,
            judge=judge,
            config_hash=config_hash,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "report_only": self.report_only,
            "evaluation_version": self.evaluation_version,
            "soft_reject": self.soft_reject.to_dict(),
            "experts": _plain_mapping(self.experts),
            "judge": dict(self.judge),
            "repairability": self.repairability.to_dict(),
        }


def _freeze_mapping(value: object, *, name: str) -> Mapping[str, Mapping[str, object]]:
    source = _mapping(value, name=name)
    result: dict[str, Mapping[str, object]] = {}
    for key, nested in source.items():
        if not isinstance(key, str) or not key:
            raise ValueError(f"{name} keys must be non-empty strings")
        nested_mapping = _mapping(nested, name=f"{name}.{key}")
        frozen: dict[str, object] = {}
        for nested_key, nested_value in nested_mapping.items():
            if not isinstance(nested_key, str):
                raise ValueError(f"{name}.{key} keys must be strings")
            if isinstance(nested_value, float) and not math.isfinite(nested_value):
                raise ValueError(f"{name}.{key}.{nested_key} must be finite")
            if not isinstance(nested_value, (str, bool, int, float)):
                raise ValueError(f"{name}.{key}.{nested_key} must be JSON-safe")
            frozen[nested_key] = nested_value
        result[key] = MappingProxyType(frozen)
    return MappingProxyType(result)


def _freeze_primitive_mapping(value: object, *, name: str) -> Mapping[str, object]:
    source = _mapping(value, name=name)
    result: dict[str, object] = {}
    for key, item in source.items():
        if not isinstance(key, str):
            raise ValueError(f"{name} keys must be strings")
        if isinstance(item, float) and not math.isfinite(item):
            raise ValueError(f"{name}.{key} must be finite")
        if not isinstance(item, (str, bool, int, float)):
            raise ValueError(f"{name}.{key} must be JSON-safe")
        result[key] = item
    return MappingProxyType(result)


def _plain_mapping(value: Mapping[str, Mapping[str, object]]) -> dict[str, dict[str, object]]:
    return {key: dict(nested) for key, nested in value.items()}
