"""Content-neutral Ollama adjudication over original and repaired contact sheets."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re
from types import MappingProxyType
from typing import Mapping

import httpx

from app.quality_moe.config import QualityMoeConfig
from app.quality_moe.models import EvidencePolarity, EvidenceStatus, ExpertEvidence, QualityDecision
from app.services.json_guard import parse_json_response


_DIMENSIONS = (
    "technical_integrity",
    "composition",
    "lighting",
    "temporal_continuity",
    "loop_continuity",
    "gif_worthiness",
)
_SIGNAL_FAMILIES = frozenset({
    "deterministic_temporal",
    "nr_vqa",
    "cinematic_classifier",
    "semantic_video_critic",
    "repair_delta",
})
_REASON_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_REFUSAL = re.compile(
    r"\b(?:cannot|can't|unable|refuse|decline)\b.{0,100}\b(?:assess|evaluate|review|judge|content|explicit)\b",
    re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True)
class JudgeRequest:
    """The immutable, already-rendered artefacts that one judge may observe."""

    candidate_id: str
    input_hash: str
    original_contact_sheet: str | Path
    best_proxy_contact_sheet: str | Path
    evidence: tuple[ExpertEvidence, ...]
    allowed_recipe_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.candidate_id, str) or not self.candidate_id:
            raise ValueError("candidate_id must be a non-empty string")
        if not isinstance(self.input_hash, str) or not self.input_hash:
            raise ValueError("input_hash must be a non-empty string")
        original = Path(self.original_contact_sheet)
        proxy = Path(self.best_proxy_contact_sheet)
        if not original.is_file() or not proxy.is_file():
            raise ValueError("both original and best proxy contact sheets must exist")
        if original.resolve() == proxy.resolve():
            raise ValueError("original and best proxy contact sheets must be distinct")
        if not all(isinstance(item, ExpertEvidence) for item in self.evidence):
            raise ValueError("evidence must contain ExpertEvidence values")
        recipes = tuple(self.allowed_recipe_ids)
        if len(set(recipes)) != len(recipes) or any(
            not isinstance(recipe, str) or not recipe for recipe in recipes
        ):
            raise ValueError("allowed_recipe_ids must be unique non-empty strings")
        object.__setattr__(self, "original_contact_sheet", original)
        object.__setattr__(self, "best_proxy_contact_sheet", proxy)
        object.__setattr__(self, "evidence", tuple(self.evidence))
        object.__setattr__(self, "allowed_recipe_ids", recipes)


@dataclass(frozen=True)
class JudgeResult:
    decision: QualityDecision
    confidence: float
    dimensions: Mapping[str, float]
    negative_signal_families: tuple[str, ...]
    reason_codes: tuple[str, ...]
    selected_recipe_id: str | None
    summary: str
    evidence: ExpertEvidence
    prompt_hash: str
    config_hash: str
    model_hash: str
    attempts: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "decision", QualityDecision(self.decision))
        object.__setattr__(self, "dimensions", MappingProxyType(dict(self.dimensions)))
        if not isinstance(self.attempts, int) or self.attempts < 0:
            raise ValueError("attempts must be a non-negative integer")


class _StructuralResponseError(ValueError):
    pass


class _InvalidResponseError(_StructuralResponseError):
    """A prohibited value cannot be repaired by accepting another model answer."""


class OllamaQualityJudge:
    """A single deterministic-temperature contact-sheet request boundary."""

    def __init__(self, config: QualityMoeConfig, transport: httpx.BaseTransport) -> None:
        if not isinstance(config, QualityMoeConfig):
            raise ValueError("config must be a QualityMoeConfig")
        if not isinstance(transport, httpx.BaseTransport):
            raise ValueError("transport must be an httpx BaseTransport")
        judge = config.judge
        model = judge.get("model_id")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("quality_moe.judge.model_id must be a non-empty string")
        temperature = judge.get("temperature", 0)
        if isinstance(temperature, bool) or temperature != 0:
            raise ValueError("quality_moe.judge.temperature must be 0")
        timeout = judge.get("timeout_seconds", 30.0)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(float(timeout)) or float(timeout) <= 0:
            raise ValueError("quality_moe.judge.timeout_seconds must be positive and finite")
        base_url = judge.get("base_url", "http://127.0.0.1:11434")
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError("quality_moe.judge.base_url must be a non-empty string")
        self._config = config
        self._model = model.strip()
        self._timeout = float(timeout)
        self._base_url = base_url.rstrip("/")
        self._transport = transport
        self._model_hash = _sha256(self._model)
        schema_version = judge.get("schema_version", "quality-judge-v1")
        self._expert_version = schema_version if isinstance(schema_version, str) and schema_version else "quality-judge-v1"

    def judge(self, request: JudgeRequest) -> JudgeResult:
        if not isinstance(request, JudgeRequest):
            raise ValueError("request must be a JudgeRequest")
        prompt = _prompt(request, correction=False)
        prompt_hash = _sha256(prompt)
        images = _images(request)
        payload = self._payload(prompt, images)
        response, error = self._send(payload)
        if error is not None:
            return self._terminal(request, prompt_hash, EvidenceStatus.UNAVAILABLE, "ollama_unavailable", error, 1)
        assert response is not None
        if _is_refusal(response):
            return self._terminal(request, prompt_hash, EvidenceStatus.ABSTAINED, "model_refusal", "The judge declined to assess the contact sheets.", 1)
        try:
            return self._valid(request, prompt_hash, response, attempts=1)
        except _InvalidResponseError as error:
            return self._terminal(request, prompt_hash, EvidenceStatus.INVALID, "invalid_judge_schema", str(error), 1)
        except _StructuralResponseError as first_error:
            correction = _prompt(request, correction=True)
            retry_response, error = self._send(self._payload(correction, images))
            if error is not None:
                return self._terminal(request, prompt_hash, EvidenceStatus.UNAVAILABLE, "ollama_unavailable", error, 2)
            assert retry_response is not None
            if _is_refusal(retry_response):
                return self._terminal(request, prompt_hash, EvidenceStatus.ABSTAINED, "model_refusal", "The judge declined to assess the contact sheets.", 2)
            try:
                return self._valid(request, prompt_hash, retry_response, attempts=2)
            except _StructuralResponseError:
                return self._terminal(request, prompt_hash, EvidenceStatus.INVALID, "invalid_judge_schema", str(first_error), 2)

    def _payload(self, prompt: str, images: list[str]) -> dict[str, object]:
        return {
            "model": self._model,
            "prompt": prompt,
            "images": images,
            "stream": False,
            "options": {"temperature": 0},
        }

    def _send(self, payload: dict[str, object]) -> tuple[str | None, str | None]:
        try:
            with httpx.Client(transport=self._transport, timeout=self._timeout) as client:
                response = client.post(f"{self._base_url}/api/generate", json=payload)
                response.raise_for_status()
                body = response.json()
        except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPStatusError, ValueError):
            return None, "Ollama was unavailable while judging contact sheets."
        if not isinstance(body, dict) or not isinstance(body.get("response"), str):
            return "", None
        return body["response"], None

    def _valid(self, request: JudgeRequest, prompt_hash: str, response: str, *, attempts: int) -> JudgeResult:
        parsed = parse_json_response(response)
        if not parsed.ok or not isinstance(parsed.data, dict):
            raise _StructuralResponseError("response must contain one JSON object")
        payload = parsed.data
        expected = {
            "decision", "confidence", "dimensions", "negative_signal_families",
            "reason_codes", "selected_recipe_id", "summary",
        }
        if set(payload) != expected:
            raise _StructuralResponseError("response fields do not match the required schema")
        try:
            decision = QualityDecision(payload["decision"])
        except (TypeError, ValueError) as exc:
            raise _StructuralResponseError("decision is invalid") from exc
        confidence = _score(payload["confidence"], "confidence")
        dimensions = _dimensions(payload["dimensions"])
        families = _families(payload["negative_signal_families"])
        reason_codes = _reason_codes(payload["reason_codes"])
        selected_recipe_id = payload["selected_recipe_id"]
        if selected_recipe_id is not None and (
            not isinstance(selected_recipe_id, str) or selected_recipe_id not in request.allowed_recipe_ids
        ):
            raise _InvalidResponseError("selected_recipe_id is not an allowed repair recipe")
        if decision is QualityDecision.KEEP_FOR_REPAIR and selected_recipe_id is None:
            raise _StructuralResponseError("KEEP_FOR_REPAIR requires an allowed selected_recipe_id")
        if decision is not QualityDecision.KEEP_FOR_REPAIR and selected_recipe_id is not None:
            raise _StructuralResponseError("only KEEP_FOR_REPAIR may select a repair recipe")
        summary = payload["summary"]
        if not isinstance(summary, str) or not summary.strip() or len(summary) > 2000:
            raise _StructuralResponseError("summary must be a bounded non-empty string")
        polarity = EvidencePolarity.NEGATIVE if families else EvidencePolarity.NEUTRAL
        evidence = ExpertEvidence(
            candidate_id=request.candidate_id,
            evaluation_version=self._config.evaluation_version,
            expert_id="ollama_contact_sheet_judge",
            expert_version=self._expert_version,
            signal_family="semantic_video_critic",
            status=EvidenceStatus.AVAILABLE,
            scores={**dimensions, "confidence": confidence},
            findings=({
                "decision": decision.value,
                "reason_codes": list(reason_codes),
                "selected_recipe_id": selected_recipe_id,
                "negative_signal_families": list(families),
                "model_hash": self._model_hash,
            },),
            summary=summary.strip(),
            input_hash=request.input_hash,
            config_hash=self._config.config_hash,
            polarity=polarity,
            prompt_hash=prompt_hash,
        )
        return JudgeResult(decision, confidence, dimensions, families, reason_codes, selected_recipe_id, summary.strip(), evidence, prompt_hash, self._config.config_hash, self._model_hash, attempts)

    def _terminal(
        self, request: JudgeRequest, prompt_hash: str, status: EvidenceStatus,
        code: str, summary: str, attempts: int,
    ) -> JudgeResult:
        evidence = ExpertEvidence(
            candidate_id=request.candidate_id,
            evaluation_version=self._config.evaluation_version,
            expert_id="ollama_contact_sheet_judge",
            expert_version=self._expert_version,
            signal_family="semantic_video_critic",
            status=status,
            findings=({"code": code},),
            summary=summary,
            input_hash=request.input_hash,
            config_hash=self._config.config_hash,
            polarity=EvidencePolarity.NEUTRAL,
            prompt_hash=prompt_hash,
        )
        return JudgeResult(QualityDecision.ABSTAIN, 0.0, {}, (), (), None, summary, evidence, prompt_hash, self._config.config_hash, self._model_hash, attempts)


def _prompt(request: JudgeRequest, *, correction: bool) -> str:
    instruction = (
        "Correct your prior response. Return only one JSON object matching the exact schema. "
        if correction else "Return only one JSON object matching the exact schema. "
    )
    evidence = [item.to_dict() for item in request.evidence]
    schema = {
        "decision": [item.value for item in QualityDecision],
        "confidence": "finite number in [0,1]",
        "dimensions": {key: "finite number in [0,1]" for key in _DIMENSIONS},
        "negative_signal_families": sorted(_SIGNAL_FAMILIES),
        "reason_codes": "unique lowercase snake_case codes",
        "selected_recipe_id": list(request.allowed_recipe_ids) + [None],
        "summary": "short objective visual-quality explanation",
    }
    return (
        "You are an objective visual-quality judge comparing two contact sheets: image 1 is the original and image 2 is the best pixel-preserving repair proxy. "
        "Apply exactly the same standard to adult and non-adult material. Do not award or deduct points for topic, identity, age, race, gender, sexuality, nudity, adult content, or whether the subject is explicit. "
        "Judge only visible technical integrity, composition, lighting, temporal continuity, loop continuity, and GIF worthiness. "
        "Unavailable or abstained experts are absence of evidence, never low quality. Use supplied evidence only as context; do not invent signal families. "
        + instruction
        + "Schema: " + json.dumps(schema, sort_keys=True, separators=(",", ":"))
        + " Evidence: " + json.dumps(evidence, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    )


def _images(request: JudgeRequest) -> list[str]:
    return [
        base64.b64encode(Path(path).read_bytes()).decode("ascii")
        for path in (request.original_contact_sheet, request.best_proxy_contact_sheet)
    ]


def _is_refusal(response: str) -> bool:
    return bool(_REFUSAL.search(response))


def _score(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _StructuralResponseError(f"{field_name} must be a finite number")
    score = float(value)
    if not math.isfinite(score) or not 0.0 <= score <= 1.0:
        raise _StructuralResponseError(f"{field_name} must be in [0,1]")
    return score


def _dimensions(value: object) -> dict[str, float]:
    if not isinstance(value, dict) or set(value) != set(_DIMENSIONS):
        raise _StructuralResponseError("dimensions must contain exactly the six required keys")
    return {name: _score(value[name], f"dimensions.{name}") for name in _DIMENSIONS}


def _families(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise _StructuralResponseError("negative_signal_families must be a list of strings")
    if len(value) != len(set(value)) or any(item not in _SIGNAL_FAMILIES for item in value):
        raise _StructuralResponseError("negative_signal_families must be unique canonical families")
    return tuple(value)


def _reason_codes(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not _REASON_CODE.fullmatch(item) for item in value):
        raise _StructuralResponseError("reason_codes must be lowercase snake_case strings")
    if len(value) != len(set(value)) or len(value) > 12:
        raise _StructuralResponseError("reason_codes must be unique and bounded")
    return tuple(value)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
