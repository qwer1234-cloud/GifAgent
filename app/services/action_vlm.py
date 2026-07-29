"""Constrained, single-call VLM verification for CV action candidates."""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
import math
from typing import Callable, Sequence

import numpy as np
from PIL import Image, ImageDraw

from app.services.action_boundary import ActionBoundaryCandidate
from app.services.json_guard import parse_json_response
from app.services.temporal_evidence import TemporalEvidence, TemporalFrame


_PHASES = frozenset({"preparation", "ongoing", "recovery", "complete", "unknown"})
_DECISION_KEYS = frozenset({
    "selected_candidate_index", "action_label", "first_phase", "anchor_phase",
    "last_phase", "complete", "confidence", "reason",
})


@dataclass(frozen=True)
class ActionVlmDecision:
    selected_candidate_index: int
    action_label: str
    first_phase: str
    anchor_phase: str
    last_phase: str
    complete: bool
    confidence: float
    reason: str


def _selected_frames(
    evidence: TemporalEvidence, candidates: Sequence[ActionBoundaryCandidate], min_frames: int, max_frames: int,
) -> tuple[TemporalFrame, ...]:
    frames = evidence.frames
    if not frames:
        raise ValueError("action VLM contact sheet requires temporal frames")
    if not (6 <= min_frames <= max_frames <= 8):
        raise ValueError("contact sheet frame bounds must stay within six to eight")
    target = len(frames) if len(frames) < min_frames else min_frames
    if target == 1:
        return (frames[0],)
    anchor_s = candidates[0].peak_s if candidates else frames[len(frames) // 2].timestamp_s
    anchor_index = min(range(len(frames)), key=lambda index: abs(frames[index].timestamp_s - anchor_s))
    selected = [round(position * (len(frames) - 1) / (target - 1)) for position in range(target)]
    if anchor_index not in selected:
        replace_at = min(range(1, target - 1), key=lambda index: abs(selected[index] - anchor_index))
        selected[replace_at] = anchor_index
    return tuple(frames[index] for index in sorted(set(selected)))


def _frame_labels(frames: Sequence[TemporalFrame]) -> list[tuple[int, float]]:
    return [(frame.sample_index, frame.timestamp_s) for frame in frames]


def _as_rgb(frame: TemporalFrame) -> Image.Image:
    gray = np.asarray(frame.gray)
    if gray.ndim != 2:
        raise ValueError("temporal frame gray image must be two-dimensional")
    return Image.fromarray(gray.astype(np.uint8, copy=False), mode="L").convert("RGB")


def build_action_contact_sheet(
    evidence: TemporalEvidence,
    candidates: Sequence[ActionBoundaryCandidate],
    min_frames: int = 6,
    max_frames: int = 8,
) -> bytes:
    """Build one labeled JPEG from first, anchor-nearest, last, and interior samples."""
    frames = _selected_frames(evidence, candidates, min_frames, max_frames)
    images = [_as_rgb(frame) for frame in frames]
    tile_width = max(image.width for image in images)
    tile_height = max(image.height for image in images) + 20
    columns = min(4, len(images))
    rows = math.ceil(len(images) / columns)
    sheet = Image.new("RGB", (columns * tile_width, rows * tile_height), "white")
    draw = ImageDraw.Draw(sheet)
    for index, (frame, image) in enumerate(zip(frames, images)):
        x, y = (index % columns) * tile_width, (index // columns) * tile_height
        sheet.paste(image, (x, y))
        draw.text((x + 2, y + image.height + 2), f"#{frame.sample_index}  +{frame.timestamp_s - evidence.start_s:.2f}s", fill="black")
    output = BytesIO()
    sheet.save(output, format="JPEG", quality=90)
    return output.getvalue()


def _label_index_at(timestamp_s: float, frame_labels: Sequence[tuple[int, float]]) -> int:
    return min(frame_labels, key=lambda label: abs(label[1] - timestamp_s))[0]


def build_action_verification_prompt(
    candidates: Sequence[ActionBoundaryCandidate], frame_labels: Sequence[tuple[int, float]],
) -> str:
    """Ask for a candidate index only; timestamps are evidence, never model output."""
    if not frame_labels:
        raise ValueError("action VLM prompt requires contact-sheet frame labels")
    lines = [
        "Review the labeled contact sheet and choose exactly one candidate index.",
        "Do not return free-form timestamps, frame ranges, or extra JSON keys.",
        "Candidate boundaries are constrained to these contact-sheet frame indexes:",
    ]
    for index, candidate in enumerate(candidates):
        lines.append(
            f"candidate index {index}: start frame {_label_index_at(candidate.start_s, frame_labels)}, "
            f"peak frame {_label_index_at(candidate.peak_s, frame_labels)}, "
            f"end frame {_label_index_at(candidate.end_s, frame_labels)}"
        )
    lines.extend((
        "Allowed phases: preparation, ongoing, recovery, complete, unknown.",
        "Return exactly this JSON object shape:",
        '{"selected_candidate_index":0,"action_label":"stands up","first_phase":"preparation",'
        '"anchor_phase":"ongoing","last_phase":"complete","complete":true,"confidence":0.82,'
        '"reason":"the selected range includes preparation, movement, and settling"}',
    ))
    return "\n".join(lines)


def parse_action_vlm_decision(raw_text: str, candidate_count: int) -> ActionVlmDecision | None:
    """Return a decision only when all VLM output fields meet the closed schema."""
    if isinstance(candidate_count, bool) or not isinstance(candidate_count, int) or candidate_count <= 0:
        return None
    parsed = parse_json_response(raw_text)
    data = parsed.data
    if not parsed.ok or not isinstance(data, dict) or set(data) != _DECISION_KEYS:
        return None
    index = data["selected_candidate_index"]
    confidence = data["confidence"]
    if (
        isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < candidate_count
        or isinstance(confidence, bool) or not isinstance(confidence, (int, float))
        or not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0
    ):
        return None
    if not isinstance(data["complete"], bool):
        return None
    for name in ("action_label", "reason"):
        if not isinstance(data[name], str) or not data[name].strip():
            return None
    for name in ("first_phase", "anchor_phase", "last_phase"):
        if not isinstance(data[name], str) or data[name] not in _PHASES:
            return None
    return ActionVlmDecision(
        selected_candidate_index=index,
        action_label=data["action_label"],
        first_phase=data["first_phase"],
        anchor_phase=data["anchor_phase"],
        last_phase=data["last_phase"],
        complete=data["complete"],
        confidence=float(confidence),
        reason=data["reason"],
    )


def verify_action_candidates(
    evidence: TemporalEvidence,
    candidates: Sequence[ActionBoundaryCandidate],
    generator: Callable[[bytes, str], str],
) -> ActionVlmDecision | None:
    """Call the supplied VLM generator once and accept only its constrained decision."""
    if not candidates:
        return None
    try:
        frames = _selected_frames(evidence, candidates, 6, 8)
        image_bytes = build_action_contact_sheet(evidence, candidates)
        prompt = build_action_verification_prompt(candidates, _frame_labels(frames))
        raw_text = generator(image_bytes, prompt)
    except (TypeError, ValueError, OSError):
        return None
    if not isinstance(raw_text, str):
        return None
    return parse_action_vlm_decision(raw_text, len(candidates))
