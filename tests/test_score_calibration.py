"""Task 15: frozen isotonic score calibration."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.score_calibration import (
    Calibrator,
    apply_calibrated_worthiness,
    load_calibrator,
)
from scripts.test_video_adaptive import extract_config


def _write_calibrator(path: Path, **overrides) -> Path:
    payload = {
        "model_id": "vlm-a",
        "prompt_mode": "adult",
        "sample_count": 240,
        "created_at": "2026-08-23T00:00:00+00:00",
        "thresholds": [0.3, 0.6, 1.0],
        "values": [0.1, 0.5, 0.9],
    }
    payload.update(overrides)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_calibrator_is_monotone():
    cal = Calibrator([0.25, 0.5, 1.0], [0.2, 0.4, 0.8], model_id="m", prompt_mode="adult", sample_count=200)
    mapped = [cal.apply(score) for score in (0.0, 0.25, 0.4, 0.5, 0.9, 1.0)]
    assert mapped == sorted(mapped)
    assert mapped[0] <= mapped[-1]


def test_load_refuses_provenance_mismatch(tmp_path: Path):
    path = _write_calibrator(tmp_path / "cal.json")
    assert load_calibrator(path, model_id="vlm-b", prompt_mode="adult") is None
    assert load_calibrator(path, model_id="vlm-a", prompt_mode="default") is None
    loaded = load_calibrator(path, model_id="vlm-a", prompt_mode="adult")
    assert loaded is not None
    assert loaded.apply(0.6) == pytest.approx(0.5)


def test_missing_or_malformed_file_returns_none(tmp_path: Path):
    assert load_calibrator(tmp_path / "missing.json", model_id="m", prompt_mode="adult") is None
    bad = tmp_path / "bad.json"
    bad.write_text("{not-json", encoding="utf-8")
    assert load_calibrator(bad, model_id="m", prompt_mode="adult") is None
    empty = _write_calibrator(tmp_path / "empty.json", thresholds=[], values=[])
    assert load_calibrator(empty, model_id="vlm-a", prompt_mode="adult") is None


def test_apply_records_raw_and_calibrated():
    cal = Calibrator([1.0], [0.4], model_id="m", prompt_mode="adult", sample_count=200)
    payload = apply_calibrated_worthiness({"gif_worthiness": 0.8}, cal)
    assert payload["gif_worthiness_raw"] == pytest.approx(0.8)
    assert payload["gif_worthiness"] == pytest.approx(0.4)


def test_thresholding_uses_calibrated_value():
    cal = Calibrator([1.0], [0.1], model_id="m", prompt_mode="adult", sample_count=200)
    payload = apply_calibrated_worthiness({"gif_worthiness": 0.9}, cal)
    assert payload["gif_worthiness"] < 0.55
    assert payload["gif_worthiness_raw"] >= 0.55


def test_extract_config_defaults_leave_calibration_off():
    cfg = extract_config({"adaptive": {}})
    assert cfg["score_calibration_enabled"] is False
    assert cfg["score_calibration_path"] == ""
