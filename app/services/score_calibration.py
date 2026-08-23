"""Frozen isotonic calibrator for VLM gif_worthiness scores."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class Calibrator:
    """Monotone piecewise-constant map from raw scores to calibrated scores."""

    def __init__(
        self,
        thresholds: list[float],
        values: list[float],
        *,
        model_id: str,
        prompt_mode: str,
        sample_count: int,
    ) -> None:
        self.thresholds = [float(item) for item in thresholds]
        self.values = [float(item) for item in values]
        self.model_id = model_id
        self.prompt_mode = prompt_mode
        self.sample_count = int(sample_count)

    def apply(self, score: float) -> float:
        raw = float(score)
        if not math.isfinite(raw):
            return raw
        raw = min(1.0, max(0.0, raw))
        if not self.thresholds:
            return raw
        value = self.values[0]
        for threshold, mapped in zip(self.thresholds, self.values):
            if raw <= threshold:
                value = mapped
                break
            value = mapped
        return min(1.0, max(0.0, float(value)))


def load_calibrator(
    path: str | Path,
    *,
    model_id: str,
    prompt_mode: str,
) -> Calibrator | None:
    """Load a frozen calibrator, or return None on any mismatch / IO error."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        print(f"  score calibrator skipped: {exc}")
        return None
    if not isinstance(payload, dict):
        print("  score calibrator skipped: payload is not an object")
        return None
    stored_model = str(payload.get("model_id") or "")
    stored_mode = str(payload.get("prompt_mode") or "")
    if stored_model != str(model_id) or stored_mode != str(prompt_mode):
        print(
            "  score calibrator skipped: provenance mismatch "
            f"(file model_id={stored_model!r} prompt_mode={stored_mode!r}, "
            f"job model_id={model_id!r} prompt_mode={prompt_mode!r})"
        )
        return None
    thresholds = payload.get("thresholds") or []
    values = payload.get("values") or []
    if (
        not isinstance(thresholds, list)
        or not isinstance(values, list)
        or len(thresholds) != len(values)
        or not thresholds
    ):
        print("  score calibrator skipped: malformed thresholds/values")
        return None
    try:
        return Calibrator(
            [float(item) for item in thresholds],
            [float(item) for item in values],
            model_id=stored_model,
            prompt_mode=stored_mode,
            sample_count=int(payload.get("sample_count") or 0),
        )
    except (TypeError, ValueError) as exc:
        print(f"  score calibrator skipped: {exc}")
        return None


def apply_calibrated_worthiness(
    payload: dict[str, Any],
    calibrator: Calibrator | None,
) -> dict[str, Any]:
    """Write raw + calibrated scores; thresholding uses the calibrated value."""
    if calibrator is None or not isinstance(payload, dict):
        return payload
    try:
        raw = float(payload.get("gif_worthiness"))
    except (TypeError, ValueError):
        return payload
    if not math.isfinite(raw):
        return payload
    payload["gif_worthiness_raw"] = raw
    payload["gif_worthiness"] = calibrator.apply(raw)
    return payload
