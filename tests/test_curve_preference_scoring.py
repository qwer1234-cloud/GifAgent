"""Tests for the centralized curve preference model loader and scoring."""

from __future__ import annotations

import json
import math
import sqlite3

import numpy as np
import pytest

from app.services.curve_preference import (
    CurvePreferenceModel,
    EvidenceUnavailable,
    curve_logit,
    curve_probability,
    load_curve_model,
)
from app.services.preference_schema import apply_preference_schema

FEATURE_NAMES = ("extension_mean", "motion_smoothness")

MANIFEST_COMMON = {
    "kind": "curve-training-manifest-v1",
    "feature_version": "curve-v1",
    "sources": ["src-1"],
}


def canonical_sha(payload: dict) -> str:
    import hashlib

    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _model_row(
    *,
    status: str = "DRAFT",
    feature_version: str = "curve-v1",
    train_manifest: dict | None = None,
    holdout_manifest: dict | None = None,
    train_sha: str | None = None,
    holdout_sha: str | None = None,
) -> dict:
    train = train_manifest if train_manifest is not None else {
        **MANIFEST_COMMON, "rows": []
    }
    holdout = holdout_manifest if holdout_manifest is not None else {
        **MANIFEST_COMMON, "kind": "curve-holdout-manifest-v1", "rows": []
    }
    return {
        "model_version": "m-1",
        "feature_version": feature_version,
        "extractor_sha256": "ex",
        "status": status,
        "coefficients_json": json.dumps({"extension_mean": 0.5, "motion_smoothness": -0.25}),
        "intercept": 0.1,
        "feature_mean_json": json.dumps({"extension_mean": 0.4, "motion_smoothness": 0.6}),
        "feature_std_json": json.dumps({"extension_mean": 0.2, "motion_smoothness": 0.1}),
        "feature_names_json": json.dumps(list(FEATURE_NAMES)),
        "l2_lambda": 1.0,
        "training_event_watermark": "wm",
        "training_source_set_sha256": "src-sha",
        "holdout_source_set_sha256": "hold-sha",
        "training_manifest_sha256": train_sha or canonical_sha(train),
        "holdout_manifest_sha256": holdout_sha or canonical_sha(holdout),
        "training_manifest_json": json.dumps(train, sort_keys=True),
        "holdout_manifest_json": json.dumps(holdout, sort_keys=True),
        "holdout_row_count": 10,
        "metrics_json": "{}",
        "curve_model_confidence": None,
    }


def _insert_model(conn: sqlite3.Connection, row: dict) -> None:
    columns = ", ".join(row)
    placeholders = ", ".join(f":{name}" for name in row)
    conn.execute(
        f"INSERT INTO curve_preference_models ({columns}) VALUES ({placeholders})",
        row,
    )
    conn.commit()


def _loaded_model(**kwargs) -> CurvePreferenceModel:
    conn = sqlite3.connect(":memory:")
    apply_preference_schema(conn)
    _insert_model(conn, _model_row(**kwargs))
    return load_curve_model(
        conn, "m-1", allowed_statuses=frozenset({"DRAFT"})
    )


def test_load_curve_model_returns_frozen_model():
    model = _loaded_model()

    assert model.model_version == "m-1"
    assert model.feature_version == "curve-v1"
    assert model.feature_names == FEATURE_NAMES
    assert model.coefficients.shape == (2,)
    assert model.intercept == pytest.approx(0.1)
    assert model.training_manifest["kind"] == "curve-training-manifest-v1"


def test_load_curve_model_rejects_non_draft_status():
    conn = sqlite3.connect(":memory:")
    apply_preference_schema(conn)
    _insert_model(conn, _model_row(status="PUBLISHED"))

    with pytest.raises(ValueError, match="status"):
        load_curve_model(conn, "m-1", allowed_statuses=frozenset({"DRAFT"}))


def test_load_curve_model_rejects_malformed_feature_key_sets():
    conn = sqlite3.connect(":memory:")
    apply_preference_schema(conn)
    row = _model_row()
    row["coefficients_json"] = json.dumps({"extension_mean": 0.5})
    _insert_model(conn, row)

    with pytest.raises(ValueError, match="coefficient"):
        load_curve_model(conn, "m-1", allowed_statuses=frozenset({"DRAFT"}))


def test_load_curve_model_rejects_zero_or_non_finite_std():
    conn = sqlite3.connect(":memory:")
    apply_preference_schema(conn)
    row = _model_row()
    row["feature_std_json"] = json.dumps(
        {"extension_mean": 0.0, "motion_smoothness": 0.1}
    )
    _insert_model(conn, row)

    with pytest.raises(ValueError, match="std"):
        load_curve_model(conn, "m-1", allowed_statuses=frozenset({"DRAFT"}))


def test_load_curve_model_rejects_non_numeric_intercept():
    conn = sqlite3.connect(":memory:")
    apply_preference_schema(conn)
    row = _model_row()
    row["intercept"] = "oops"
    _insert_model(conn, row)

    with pytest.raises(ValueError, match="intercept"):
        load_curve_model(conn, "m-1", allowed_statuses=frozenset({"DRAFT"}))


def test_load_curve_model_rejects_training_manifest_hash_mismatch():
    conn = sqlite3.connect(":memory:")
    apply_preference_schema(conn)
    _insert_model(conn, _model_row(train_sha="0" * 64))

    with pytest.raises(ValueError, match="training_manifest"):
        load_curve_model(conn, "m-1", allowed_statuses=frozenset({"DRAFT"}))


def test_load_curve_model_rejects_holdout_manifest_hash_mismatch():
    conn = sqlite3.connect(":memory:")
    apply_preference_schema(conn)
    _insert_model(conn, _model_row(holdout_sha="0" * 64))

    with pytest.raises(ValueError, match="holdout_manifest"):
        load_curve_model(conn, "m-1", allowed_statuses=frozenset({"DRAFT"}))


def test_load_curve_model_rejects_manifest_feature_version_mismatch():
    mismatched = {**MANIFEST_COMMON, "feature_version": "curve-v9", "rows": []}
    conn = sqlite3.connect(":memory:")
    apply_preference_schema(conn)
    _insert_model(conn, _model_row(train_manifest=mismatched))

    with pytest.raises(ValueError, match="feature_version"):
        load_curve_model(conn, "m-1", allowed_statuses=frozenset({"DRAFT"}))


def test_curve_logit_uses_standardized_linear_score():
    model = _loaded_model()

    logit = curve_logit(model, {"extension_mean": 0.6, "motion_smoothness": 0.6})

    expected = (
        0.5 * (0.6 - 0.4) / 0.2
        + (-0.25) * (0.6 - 0.6) / 0.1
        + 0.1
    )
    assert logit == pytest.approx(expected)


def test_curve_logit_raises_evidence_unavailable_on_missing_feature():
    model = _loaded_model()

    with pytest.raises(EvidenceUnavailable):
        curve_logit(model, {"extension_mean": 0.6})

    with pytest.raises(EvidenceUnavailable):
        curve_logit(model, {"extension_mean": "bad", "motion_smoothness": 0.6})

    with pytest.raises(EvidenceUnavailable):
        curve_logit(
            model,
            {"extension_mean": float("nan"), "motion_smoothness": 0.6},
        )


def test_curve_probability_is_stable_sigmoid_in_unit_interval():
    model = _loaded_model()

    mid = curve_probability(model, {"extension_mean": 0.4, "motion_smoothness": 0.6})
    high = curve_probability(model, {"extension_mean": 5.0, "motion_smoothness": 5.0})
    low = curve_probability(model, {"extension_mean": -5.0, "motion_smoothness": -5.0})
    extreme = curve_probability(
        model, {"extension_mean": 1e9, "motion_smoothness": -1e9}
    )

    # Both features at their training mean leave only the intercept.
    assert mid == pytest.approx(1.0 / (1.0 + math.exp(-0.1)))
    assert 0.0 <= low <= high <= 1.0
    assert math.isfinite(extreme)
    assert 0.0 <= extreme <= 1.0
    assert not math.isnan(curve_probability(
        model, {"extension_mean": -1e9, "motion_smoothness": 1e9}
    ))


def test_curve_probability_matches_manual_sigmoid_of_logit():
    model = _loaded_model()
    features = {"extension_mean": 0.7, "motion_smoothness": 0.3}

    logit = curve_logit(model, features)
    probability = curve_probability(model, features)

    assert probability == pytest.approx(1.0 / (1.0 + math.exp(-logit)))
