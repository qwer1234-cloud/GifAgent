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
    """Resolve VLM and quality endpoint states once for immutable storage."""
    snapshot = json.loads(json.dumps(dict(config_data)))
    quality = snapshot.get("quality_moe")
    judge = quality.get("judge") if isinstance(quality, dict) else None
    vlm = snapshot.get("vlm")
    if not isinstance(judge, dict) and not isinstance(vlm, dict):
        return snapshot

    from app.services.ollama_runtime import (
        EmbeddingRuntimeConfig,
        normalize_base_url,
    )

    embedding = snapshot.get("embedding")
    embedding = embedding if isinstance(embedding, dict) else {}
    resolver = ready_resolver or _ensure_quality_runtime_ready

    def runtime_config(primary: Mapping[str, Any]) -> EmbeddingRuntimeConfig:
        return EmbeddingRuntimeConfig(
            base_url="auto",
            manage_lifecycle=bool(primary.get(
                "manage_lifecycle",
                embedding.get("manage_lifecycle", False),
            )),
            launch_mode=str(primary.get(
                "launch_mode", embedding.get("launch_mode", "wsl") or "wsl"
            )).lower(),
            wsl_distro=str(primary.get(
                "wsl_distro", embedding.get("wsl_distro", "Ubuntu-20.04")
            )),
            startup_timeout_s=float(primary.get(
                "startup_timeout_s", embedding.get("startup_timeout_s", 120.0)
            )),
            request_timeout_s=float(primary.get(
                "timeout_seconds", embedding.get("request_timeout_s", 60.0)
            )),
            retry_attempts=int(primary.get(
                "retry_attempts", embedding.get("retry_attempts", 3)
            )),
            retry_backoff_s=float(primary.get(
                "retry_backoff_s", embedding.get("retry_backoff_s", 2.0)
            )),
            keep_alive=str(embedding.get("keep_alive", "30m")),
            embedding_model=str(
                embedding.get("text_model", "nomic-embed-text:latest")
            ),
            embedding_dim=int(embedding.get("embedding_dim", 768)),
        )

    def resolve_auto(primary: Mapping[str, Any], *, name: str) -> str:
        state = resolver(runtime_config(primary))
        resolved = normalize_base_url(str(state.base_url))
        if not resolved.startswith(("http://", "https://")):
            raise ValueError(f"resolved {name} base_url must be absolute")
        return resolved

    vlm_mapping = vlm if isinstance(vlm, dict) else {}
    configured_vlm = str(vlm_mapping.get("base_url", "") or "").strip()
    resolved_vlm = ""
    if configured_vlm.lower() == "auto":
        resolved_vlm = resolve_auto(vlm_mapping, name="VLM")
        vlm_mapping["base_url"] = resolved_vlm
    elif configured_vlm:
        resolved_vlm = normalize_base_url(configured_vlm)
        vlm_mapping["base_url"] = resolved_vlm

    if not isinstance(judge, dict):
        return snapshot
    configured_judge = str(judge.get("base_url", "") or "").strip()
    judge_state = configured_judge.lower()
    if judge_state == "inherit_vlm":
        if not resolved_vlm:
            resolved_vlm = resolve_auto(vlm_mapping, name="VLM")
            if isinstance(vlm, dict):
                vlm["base_url"] = resolved_vlm
        judge["base_url"] = resolved_vlm
    elif judge_state == "auto":
        judge["base_url"] = resolve_auto(judge, name="quality judge")
    elif configured_judge:
        judge["base_url"] = normalize_base_url(configured_judge)
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
        experts = _freeze_expert_mapping(values["experts"])
        judge = _freeze_judge_mapping(values["judge"])
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


def _freeze_expert_mapping(value: object) -> Mapping[str, Mapping[str, object]]:
    name = "quality_moe.experts"
    source = _mapping(value, name=name)
    allowed_experts = {"technical_aesthetic", "cinematic", "temporal"}
    unknown_experts = set(source) - allowed_experts
    if unknown_experts:
        raise ValueError(
            f"{name} has unknown experts: {', '.join(sorted(unknown_experts))}"
        )
    result: dict[str, Mapping[str, object]] = {}
    for key, nested in source.items():
        nested_mapping = _mapping(nested, name=f"{name}.{key}")
        allowed_fields = {"enabled", "version", "model_id"}
        unknown_fields = set(nested_mapping) - allowed_fields
        if unknown_fields:
            raise ValueError(
                f"{name}.{key} has unknown fields: "
                f"{', '.join(sorted(unknown_fields))}"
            )
        frozen: dict[str, object] = {}
        for nested_key, nested_value in nested_mapping.items():
            if nested_key == "enabled":
                if not isinstance(nested_value, bool):
                    raise ValueError(f"{name}.{key}.enabled must be a boolean")
                if not nested_value:
                    raise ValueError(
                        f"{name}.{key} is mandatory in quality-moe-v1"
                    )
            elif not isinstance(nested_value, str) or not nested_value.strip():
                raise ValueError(
                    f"{name}.{key}.{nested_key} must be a non-empty string"
                )
            frozen[nested_key] = nested_value
        result[key] = MappingProxyType(frozen)
    return MappingProxyType(result)


def _freeze_judge_mapping(value: object) -> Mapping[str, object]:
    name = "quality_moe.judge"
    source = _mapping(value, name=name)
    allowed_fields = {
        "base_url",
        "launch_mode",
        "manage_lifecycle",
        "model_id",
        "retry_attempts",
        "retry_backoff_s",
        "schema_version",
        "startup_timeout_s",
        "temperature",
        "timeout_seconds",
        "wsl_distro",
    }
    unknown_fields = set(source) - allowed_fields
    if unknown_fields:
        raise ValueError(
            f"{name} has unknown fields: {', '.join(sorted(unknown_fields))}"
        )
    result: dict[str, object] = {}
    for key, item in source.items():
        if key == "manage_lifecycle":
            if not isinstance(item, bool):
                raise ValueError(f"{name}.{key} must be a boolean")
        elif key in {"model_id", "base_url", "schema_version", "launch_mode", "wsl_distro"}:
            if not isinstance(item, str) or not item.strip():
                raise ValueError(f"{name}.{key} must be a non-empty string")
        elif key == "temperature":
            if isinstance(item, bool) or not isinstance(item, (int, float)) or item != 0:
                raise ValueError(f"{name}.temperature must be 0")
        elif key == "retry_attempts":
            if isinstance(item, bool) or not isinstance(item, int) or item < 1:
                raise ValueError(f"{name}.retry_attempts must be a positive integer")
        elif key in {"timeout_seconds", "startup_timeout_s"}:
            if isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(float(item)) or float(item) <= 0:
                raise ValueError(f"{name}.{key} must be positive and finite")
        elif key == "retry_backoff_s":
            if isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(float(item)) or float(item) < 0:
                raise ValueError(f"{name}.{key} must be non-negative and finite")
        result[key] = item
    return MappingProxyType(result)


def _plain_mapping(value: Mapping[str, Mapping[str, object]]) -> dict[str, dict[str, object]]:
    return {key: dict(nested) for key, nested in value.items()}
