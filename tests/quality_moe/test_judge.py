from __future__ import annotations

import json
import math
from pathlib import Path

import httpx
import pytest

from app.quality_moe.config import QualityMoeConfig
from app.quality_moe.models import EvidencePolarity, EvidenceStatus, ExpertEvidence, QualityDecision


DIMENSIONS = {
    "technical_integrity": 0.8,
    "composition": 0.8,
    "lighting": 0.8,
    "temporal_continuity": 0.8,
    "loop_continuity": 0.8,
    "gif_worthiness": 0.8,
}


class FakeTransport(httpx.BaseTransport):
    def __init__(self) -> None:
        self._responses: list[object] = []
        self.requests: list[httpx.Request] = []

    def respond(self, response: object) -> None:
        self._responses.append(response)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        response = self._responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        if isinstance(response, httpx.Response):
            return httpx.Response(
                response.status_code, content=response.content,
                headers=response.headers, request=request,
            )
        return httpx.Response(200, json=response, request=request)


@pytest.fixture
def fake_transport() -> FakeTransport:
    return FakeTransport()


@pytest.fixture
def sheets(tmp_path: Path) -> tuple[Path, Path]:
    original, proxy = tmp_path / "original.png", tmp_path / "proxy.png"
    original.write_bytes(b"original-sheet")
    proxy.write_bytes(b"proxy-sheet")
    return original, proxy


def valid_payload(**changes: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "decision": "KEEP_FOR_REPAIR",
        "confidence": 0.8,
        "dimensions": dict(DIMENSIONS),
        "negative_signal_families": ["nr_vqa", "cinematic_classifier"],
        "reason_codes": ["underexposed"],
        "selected_recipe_id": "repair-1",
        "summary": "The proxy improves exposure without changing the judged subject.",
    }
    payload.update(changes)
    return payload


def config() -> QualityMoeConfig:
    return QualityMoeConfig.from_mapping({
        "quality_moe": {"judge": {"model_id": "llava:latest", "timeout_seconds": 3.0}}
    })


def request(sheets: tuple[Path, Path]):
    from app.quality_moe.judge import JudgeRequest

    original, proxy = sheets
    return JudgeRequest(
        candidate_id="candidate-1",
        input_hash="source-sha",
        original_contact_sheet=original,
        best_proxy_contact_sheet=proxy,
        evidence=(
            ExpertEvidence(
                candidate_id="candidate-1", evaluation_version="quality-moe-v1",
                expert_id="technical", expert_version="v1", signal_family="nr_vqa",
                status=EvidenceStatus.AVAILABLE, scores={"technical_integrity": 0.4},
                input_hash="source-sha", config_hash=config().config_hash,
                polarity=EvidencePolarity.NEGATIVE,
            ),
        ),
        allowed_recipe_ids=("repair-1",),
    )


def judge(fake_transport: FakeTransport):
    from app.quality_moe.judge import OllamaQualityJudge

    return OllamaQualityJudge(config(), fake_transport)


def test_valid_json_sends_both_sheets_with_neutral_prompt_and_traces_hashes(
    fake_transport: FakeTransport, sheets: tuple[Path, Path],
) -> None:
    fake_transport.respond({"response": json.dumps(valid_payload())})

    result = judge(fake_transport).judge(request(sheets))

    assert result.decision is QualityDecision.KEEP_FOR_REPAIR
    assert result.evidence.status is EvidenceStatus.AVAILABLE
    assert result.selected_recipe_id == "repair-1"
    assert result.evidence.prompt_hash == result.prompt_hash
    assert result.evidence.config_hash == config().config_hash
    assert len(result.model_hash) == 64
    payload = json.loads(fake_transport.requests[0].content)
    assert fake_transport.requests[0].url.path == "/api/generate"
    assert payload["model"] == "llava:latest"
    assert payload["options"] == {"temperature": 0}
    assert len(payload["images"]) == 2
    assert "adult" in payload["prompt"].lower()
    assert "topic" in payload["prompt"].lower()
    assert "identity" in payload["prompt"].lower()
    assert "do not award or deduct" in payload["prompt"].lower()


def test_refusal_becomes_abstain_without_negative_vote(
    fake_transport: FakeTransport, sheets: tuple[Path, Path],
) -> None:
    fake_transport.respond({"response": "I cannot assess explicit content."})

    result = judge(fake_transport).judge(request(sheets))

    assert result.decision is QualityDecision.ABSTAIN
    assert result.evidence.status is EvidenceStatus.ABSTAINED
    assert result.negative_signal_families == ()


def test_malformed_output_retries_once_with_same_sheets_then_invalid_abstain(
    fake_transport: FakeTransport, sheets: tuple[Path, Path],
) -> None:
    fake_transport.respond({"response": "not JSON"})
    fake_transport.respond({"response": "still not JSON"})

    result = judge(fake_transport).judge(request(sheets))

    assert result.decision is QualityDecision.ABSTAIN
    assert result.evidence.status is EvidenceStatus.INVALID
    assert result.negative_signal_families == ()
    assert len(fake_transport.requests) == 2
    first, second = (json.loads(item.content) for item in fake_transport.requests)
    assert first["images"] == second["images"]
    assert "correct" in second["prompt"].lower()
    assert len(result.attempt_prompt_hashes) == 2
    assert result.attempt_prompt_hashes[0] != result.evidence.prompt_hash
    assert result.attempt_prompt_hashes[-1] == result.evidence.prompt_hash


def test_unknown_recipe_id_retries_with_same_sheets_then_invalid(
    fake_transport: FakeTransport, sheets: tuple[Path, Path],
) -> None:
    fake_transport.respond({"response": json.dumps(valid_payload(selected_recipe_id="invented"))})
    fake_transport.respond({"response": json.dumps(valid_payload(selected_recipe_id="invented"))})

    result = judge(fake_transport).judge(request(sheets))

    assert result.evidence.status is EvidenceStatus.INVALID
    assert result.decision is QualityDecision.ABSTAIN
    assert len(fake_transport.requests) == 2
    first, second = (json.loads(item.content) for item in fake_transport.requests)
    assert first["images"] == second["images"]


@pytest.mark.parametrize("bad_value", [math.nan, math.inf, -0.01, 1.01])
def test_non_finite_or_out_of_range_dimension_is_invalid(
    fake_transport: FakeTransport, sheets: tuple[Path, Path], bad_value: float,
) -> None:
    dimensions = dict(DIMENSIONS, composition=bad_value)
    fake_transport.respond({"response": json.dumps(valid_payload(dimensions=dimensions))})
    fake_transport.respond({"response": json.dumps(valid_payload(dimensions=dimensions))})

    result = judge(fake_transport).judge(request(sheets))

    assert result.evidence.status is EvidenceStatus.INVALID
    assert result.decision is QualityDecision.ABSTAIN


@pytest.mark.parametrize(
    "changes",
    [
        {"decision": "INVENTED"},
        {"negative_signal_families": ["invented_family"]},
        {"reason_codes": ["Not A Normalized Code"]},
    ],
)
def test_invalid_decision_family_or_reason_retries_once_then_invalid(
    fake_transport: FakeTransport, sheets: tuple[Path, Path], changes: dict[str, object],
) -> None:
    invalid = {"response": json.dumps(valid_payload(**changes))}
    fake_transport.respond(invalid)
    fake_transport.respond(invalid)

    result = judge(fake_transport).judge(request(sheets))

    assert result.decision is QualityDecision.ABSTAIN
    assert result.evidence.status is EvidenceStatus.INVALID
    assert len(fake_transport.requests) == 2


def test_network_failure_is_unavailable_without_retry(
    fake_transport: FakeTransport, sheets: tuple[Path, Path],
) -> None:
    fake_transport.respond(httpx.ConnectError("offline"))

    result = judge(fake_transport).judge(request(sheets))

    assert result.evidence.status is EvidenceStatus.UNAVAILABLE
    assert result.decision is QualityDecision.ABSTAIN
    assert result.negative_signal_families == ()
    assert len(fake_transport.requests) == 1


def test_http_service_error_is_unavailable_without_retry(
    fake_transport: FakeTransport, sheets: tuple[Path, Path],
) -> None:
    fake_transport.respond(httpx.Response(503, content=b"service unavailable"))

    result = judge(fake_transport).judge(request(sheets))

    assert result.decision is QualityDecision.ABSTAIN
    assert result.evidence.status is EvidenceStatus.UNAVAILABLE
    assert result.negative_signal_families == ()
    assert len(fake_transport.requests) == 1


@pytest.mark.parametrize(
    "response",
    [
        {"response": "I can't help with that."},
        {"response": "I cannot comply with that request."},
        {"response": "Sorry, I can't assist with that."},
        {"response": "抱歉，我无法协助评估此内容。"},
    ],
)
def test_common_content_neutral_refusals_abstain_without_retry(
    fake_transport: FakeTransport, sheets: tuple[Path, Path], response: dict[str, str],
) -> None:
    fake_transport.respond(response)

    result = judge(fake_transport).judge(request(sheets))

    assert result.decision is QualityDecision.ABSTAIN
    assert result.evidence.status is EvidenceStatus.ABSTAINED
    assert result.evidence.polarity is EvidencePolarity.NEUTRAL
    assert result.negative_signal_families == ()
    assert len(fake_transport.requests) == 1


def test_structured_abstain_never_becomes_available_negative_evidence(
    fake_transport: FakeTransport, sheets: tuple[Path, Path],
) -> None:
    fake_transport.respond({"response": json.dumps(valid_payload(
        decision="ABSTAIN", selected_recipe_id=None,
    ))})

    result = judge(fake_transport).judge(request(sheets))

    assert result.decision is QualityDecision.ABSTAIN
    assert result.evidence.status is EvidenceStatus.ABSTAINED
    assert result.evidence.polarity is EvidencePolarity.NEUTRAL
    assert result.negative_signal_families == ()


@pytest.mark.parametrize(
    "first_response",
    [
        [],
        httpx.Response(200, content=b"{not-json"),
    ],
)
def test_non_object_or_invalid_envelope_retries_as_structural_failure(
    fake_transport: FakeTransport, sheets: tuple[Path, Path], first_response: object,
) -> None:
    fake_transport.respond(first_response)
    fake_transport.respond({"response": json.dumps(valid_payload())})

    result = judge(fake_transport).judge(request(sheets))

    assert result.decision is QualityDecision.KEEP_FOR_REPAIR
    assert result.evidence.status is EvidenceStatus.AVAILABLE
    assert result.attempts == 2
    assert len(fake_transport.requests) == 2
    first, second = (json.loads(item.content) for item in fake_transport.requests)
    assert first["images"] == second["images"]
    assert result.evidence.prompt_hash == result.attempt_prompt_hashes[-1]
