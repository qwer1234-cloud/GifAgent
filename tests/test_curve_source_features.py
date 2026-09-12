"""Tests for source curve features validation, extraction, and compatibility."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.services.curve_source_features import (
    CURVE_SOURCE_FEATURE_NAMES,
    CURVE_SOURCE_FEATURE_VERSION,
    SOURCE_DECODE_PROFILE_V1,
    extract_source_features,
    validate_feature_compatibility,
)


def test_gif_model_cannot_consume_source_features():
    assert "feature_version_mismatch" in validate_feature_compatibility(
        {"feature_version": "curve-v1"},
        {"feature_version": "curve-source-v1"},
    )


def test_extractor_sha256_mismatch_is_rejected():
    assert "extractor_sha256_mismatch" in validate_feature_compatibility(
        {"feature_version": "curve-source-v1", "extractor_sha256": "a" * 64},
        {"feature_version": "curve-source-v1", "extractor_sha256": "b" * 64},
    )


def test_compatible_model_and_features_pass():
    res = validate_feature_compatibility(
        {"feature_version": "curve-source-v1", "extractor_sha256": "a" * 64},
        {"feature_version": "curve-source-v1", "extractor_sha256": "a" * 64},
    )
    assert res == []


def test_extract_source_features_invalid_request():
    response = extract_source_features({})
    assert response["status"] == "UNAVAILABLE"
    assert "source_path_missing" in response["missing_reasons"]


def test_extract_source_features_missing_file(tmp_path: Path):
    req = {
        "source_path": str(tmp_path / "non_existent.mp4"),
        "source_sha256": "0" * 64,
        "start_s": 1.0,
        "end_s": 4.0,
        "feature_version": CURVE_SOURCE_FEATURE_VERSION,
    }
    response = extract_source_features(req)
    assert response["status"] == "UNAVAILABLE"
    assert any("file_not_found" in r for r in response["missing_reasons"])


def test_extract_source_features_synthetic_mock():
    # Test feature dictionary completeness when given valid synthetic numerical values
    from app.services.curve_source_features import format_source_feature_response

    mock_values = {k: 0.5 for k in CURVE_SOURCE_FEATURE_NAMES}
    resp = format_source_feature_response(
        status="AVAILABLE",
        features=mock_values,
        actual_timestamps=[1.0, 2.0, 3.0, 4.0],
        missing_reasons=[],
        input_hash="c" * 64,
        wall_clock_ms=12.5,
    )
    assert resp["status"] == "AVAILABLE"
    assert len(resp["features"]) == len(CURVE_SOURCE_FEATURE_NAMES)
    assert resp["wall_clock_ms"] == 12.5


# ── Task 0 regressions (R9): strict feature completeness and identity ───────


def test_available_features_cannot_be_synthesized_from_missing_values():
    """R9: an AVAILABLE response must carry all ten finite feature values;
    missing entries must never be zero-filled into a fake AVAILABLE."""
    from app.services.curve_source_features import format_source_feature_response

    with pytest.raises(ValueError, match="feature_set_incomplete"):
        format_source_feature_response(status="AVAILABLE", features={})


def test_extractor_identity_is_required_on_both_sides():
    """R9: feature metadata without an extractor identity is incompatible,
    even when feature_version matches."""
    reasons = validate_feature_compatibility(
        {"feature_version": "curve-source-v1", "extractor_sha256": "a" * 64},
        {"feature_version": "curve-source-v1"},
    )
    assert "extractor_sha256_missing" in reasons
