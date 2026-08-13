import json
from io import BytesIO

import numpy as np
import pytest
from PIL import Image

from app.services.action_boundary import ActionBoundaryCandidate
from app.services.action_vlm import (
    build_action_contact_sheet,
    build_action_verification_prompt,
    parse_action_vlm_decision,
    verify_action_candidates,
)
from app.services.temporal_evidence import TemporalEvidence, TemporalFrame


def _evidence(frame_count: int = 8) -> TemporalEvidence:
    frames = tuple(
        TemporalFrame(
            sample_index=index,
            timestamp_s=float(index),
            gray=np.full((20, 30), index * 20, dtype=np.uint8),
            hsv=np.zeros((20, 30, 3), dtype=np.uint8),
        )
        for index in range(frame_count)
    )
    return TemporalEvidence(0.0, float(max(0, frame_count - 1)), 1.0, 30, frames, ())


def _candidates() -> tuple[ActionBoundaryCandidate, ...]:
    return (
        ActionBoundaryCandidate(0.0, 3.0, 7.0, 0.9, 1.0, 1.0, 1.0, 1.0),
        ActionBoundaryCandidate(1.0, 4.0, 6.0, 0.8, 1.0, 1.0, 1.0, 1.0),
    )


def _valid_response(**overrides: object) -> str:
    payload: dict[str, object] = {
        "selected_candidate_index": 1,
        "action_label": "stands up",
        "first_phase": "preparation",
        "anchor_phase": "ongoing",
        "last_phase": "complete",
        "complete": True,
        "confidence": 0.82,
        "reason": "motion starts after rest and settles at the end",
    }
    payload.update(overrides)
    return json.dumps(payload)


def test_parser_accepts_only_a_candidate_index():
    decision = parse_action_vlm_decision(_valid_response(), candidate_count=2)

    assert decision is not None
    assert decision.selected_candidate_index == 1
    assert decision.complete is True


@pytest.mark.parametrize(
    "raw",
    [
        _valid_response(selected_candidate_index=-1),
        _valid_response(selected_candidate_index=2),
        _valid_response(selected_candidate_index=True),
        _valid_response(confidence=True),
        _valid_response(confidence=float("nan")),
        _valid_response(first_phase="before"),
        _valid_response(start_s=1.0),
        json.dumps({"selected_candidate_index": 0}),
        "not-json",
    ],
)
def test_parser_rejects_unconstrained_or_invalid_responses(raw: str):
    assert parse_action_vlm_decision(raw, candidate_count=2) is None


def test_contact_sheet_has_six_frames_when_evidence_has_six_or_more_samples():
    sheet = build_action_contact_sheet(_evidence(9), _candidates())
    image = Image.open(BytesIO(sheet))

    assert sheet.startswith(b"\xff\xd8")
    assert sheet.endswith(b"\xff\xd9")
    assert image.size == (120, 80)
    assert [image.getpixel((x, y))[0] for x, y in ((0, 0), (30, 0), (60, 0), (90, 0), (0, 40), (30, 40))] == pytest.approx([0, 40, 60, 100, 120, 160], abs=5)


def test_contact_sheet_uses_every_available_frame_when_evidence_is_short():
    sheet = build_action_contact_sheet(_evidence(4), _candidates())

    assert sheet.startswith(b"\xff\xd8")
    assert sheet.endswith(b"\xff\xd9")


def test_contact_sheet_supports_one_available_frame():
    sheet = build_action_contact_sheet(_evidence(1), _candidates())

    assert sheet.startswith(b"\xff\xd8")


def test_contact_sheet_never_exceeds_eight_frames():
    sheet = build_action_contact_sheet(_evidence(12), _candidates(), min_frames=8, max_frames=8)
    image = Image.open(BytesIO(sheet))

    assert image.size == (120, 80)


def test_contact_sheet_rejects_requested_bounds_outside_six_to_eight_frames():
    with pytest.raises(ValueError):
        build_action_contact_sheet(_evidence(12), _candidates(), min_frames=9, max_frames=9)


def test_prompt_limits_selection_to_candidate_indexes_and_frame_indexes():
    prompt = build_action_verification_prompt(_candidates(), [(0, 0.0), (3, 3.0), (7, 7.0)])

    assert "candidate index 0" in prompt
    assert "start frame 0" in prompt
    assert "peak frame 3" in prompt
    assert "end frame 7" in prompt
    assert "free-form timestamps" in prompt


def test_verifier_calls_generator_once(evidence, candidates):
    calls = []

    def generator(image_bytes: bytes, prompt: str) -> str:
        calls.append((image_bytes, prompt))
        return _valid_response(selected_candidate_index=0)

    decision = verify_action_candidates(evidence, candidates, generator)

    assert decision is not None
    assert len(calls) == 1
    assert b"\xff\xd8" in calls[0][0]


@pytest.fixture
def evidence() -> TemporalEvidence:
    return _evidence()


@pytest.fixture
def candidates() -> tuple[ActionBoundaryCandidate, ...]:
    return _candidates()
