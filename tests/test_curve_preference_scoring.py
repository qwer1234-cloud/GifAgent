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


# ---------------------------------------------------------------------------
# Curve-aware reranker contract
# ---------------------------------------------------------------------------

from tests.test_preference_reranker import (  # noqa: E402
    _characterization_db,
)


def _score(curve_probability=None, curve_model_version=None, curve_model_confidence=None):
    from app.services.reranker import PreferenceReranker
    from app.services.vector_math import l2_normalize

    rng = np.random.default_rng(42)
    vec = l2_normalize(rng.normal(0, 1, 768).astype(np.float32)).astype(np.float32)
    liked = l2_normalize(rng.normal(0, 1, 768).astype(np.float32)).astype(np.float32)
    disliked = l2_normalize(rng.normal(0, 1, 768).astype(np.float32)).astype(np.float32)
    scenario = l2_normalize(rng.normal(0, 1, 768).astype(np.float32)).astype(np.float32)
    conn = _characterization_db(liked=liked, disliked=disliked, scenario=scenario, current=True)
    return PreferenceReranker(conn).score(
        candidate_vector=vec,
        base_rag_similarity=0.7,
        scenario_keys=["emotion:joy"],
        profile_version=None,
        enabled=True,
        curve_probability=curve_probability,
        curve_model_version=curve_model_version,
        curve_model_confidence=curve_model_confidence,
    )


def test_curve_inputs_all_none_matches_no_curve_path():
    without = _score()
    with_none = _score(curve_probability=None, curve_model_version=None,
                       curve_model_confidence=None)

    assert with_none == without
    assert "curve_preference_score" not in with_none


def test_curve_active_uses_reduced_base_rag_and_curve_weight():
    result = _score(curve_probability=0.9, curve_model_version="m-1",
                    curve_model_confidence=1.0)

    weights = result["active_weights"]
    assert "curve_preference" in weights
    # Availability-aware nominal set: 0.45 base, 0.10 curve, and the
    # profile-confidences 0.8/0.75 scale global_like/scenario_like before
    # the positive renormalization.
    nominal = 0.45 + 0.10 + 0.25 * 0.8 + 0.15 * 0.75
    assert weights["base_rag"] == pytest.approx(0.45 / nominal)
    assert weights["curve_preference"] == pytest.approx(0.10 / nominal)
    assert weights["global_like"] == pytest.approx((0.25 * 0.8) / nominal)
    assert weights["scenario_like"] == pytest.approx((0.15 * 0.75) / nominal)
    assert result["curve_preference_score"] == pytest.approx(0.9)
    assert result["curve_model_version"] == "m-1"
    assert result["curve_preference_weight"] == pytest.approx(0.10)
    assert result["profile_score"] is not None
    assert 0.0 <= result["final_score"] <= 1.0


def test_curve_confidence_scales_prenormalization_weight():
    full = _score(curve_probability=0.9, curve_model_version="m-1",
                  curve_model_confidence=1.0)
    half = _score(curve_probability=0.9, curve_model_version="m-1",
                  curve_model_confidence=0.5)

    assert half["curve_preference_weight"] == pytest.approx(
        0.5 * full["curve_preference_weight"]
    )


def test_curve_confidence_zero_produces_no_ranking_influence():
    no_curve = _score()
    zero_conf = _score(curve_probability=0.9, curve_model_version="m-1",
                       curve_model_confidence=0.0)

    assert zero_conf["final_score"] == no_curve["final_score"]
    assert zero_conf["raw_score"] == no_curve["raw_score"]
    assert "curve_preference" not in zero_conf["active_weights"]
    assert "curve_preference" in zero_conf["inactive_reasons"]


def test_curve_partial_inputs_inactive_with_reason():
    result = _score(curve_probability=0.9, curve_model_version=None,
                    curve_model_confidence=None)

    assert result["inactive_reasons"]["curve_preference"] == "partial curve inputs"
    assert "curve_preference_score" not in result


@pytest.mark.parametrize("bad_probability", [1.5, -0.1, float("nan")])
def test_curve_probability_out_of_range_inactive(bad_probability):
    result = _score(curve_probability=bad_probability, curve_model_version="m-1",
                    curve_model_confidence=1.0)

    assert result["inactive_reasons"]["curve_preference"] == (
        "curve probability out of range"
    )


def test_curve_confidence_out_of_range_inactive():
    result = _score(curve_probability=0.9, curve_model_version="m-1",
                    curve_model_confidence=1.5)

    assert result["inactive_reasons"]["curve_preference"] == (
        "curve confidence out of range"
    )


def test_curve_probability_monotonic_between_near_zero_and_near_one():
    low = _score(curve_probability=0.01, curve_model_version="m-1",
                 curve_model_confidence=1.0)
    high = _score(curve_probability=0.99, curve_model_version="m-1",
                  curve_model_confidence=1.0)

    assert low["final_score"] < high["final_score"]
    assert 0.0 <= low["final_score"] <= 1.0
    assert 0.0 <= high["final_score"] <= 1.0


def test_curve_unavailable_does_not_inject_neutral_half():
    unavailable = _score()
    half = _score(curve_probability=0.5, curve_model_version="m-1",
                  curve_model_confidence=1.0)

    # An unavailable curve must restore the no-curve weights, not a 0.5 score.
    assert unavailable["final_score"] != half["final_score"]
    assert "curve_preference" not in unavailable["active_weights"]
    assert half["active_weights"]["curve_preference"] > 0
