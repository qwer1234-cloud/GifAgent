"""Tests for curve online routing, shadow/active scoring, and config binding."""
from __future__ import annotations

import sqlite3

import pytest

from app.services.curve_online import (
    curve_route,
    load_curve_runtime_snapshot,
    score_curve_candidate,
)
from app.services.preference_schema import apply_preference_schema


class _CurveAwareReranker:
    """Profile authority whose profile_score responds to curve confidence.

    Mirrors how the real reranker's curve component raises the preference
    signal: profile_score = base + confidence-scaled curve contribution.
    """

    def __init__(self, base_profile: float = 0.5) -> None:
        self.base_profile = base_profile
        self.calls: list[dict] = []

    def score(self, **kwargs):
        self.calls.append(kwargs)
        confidence = kwargs.get("curve_model_confidence")
        if confidence is None:
            profile = self.base_profile
        else:
            profile = min(1.0, self.base_profile + 0.2 * confidence)
        return {
            "profile_score": profile,
            "preference_profile_version": "profile_test",
        }


def _snapshot(**overrides) -> dict:
    snapshot = {
        "profile_version": "profile_test",
        "reranker": _CurveAwareReranker(),
        "base_score_weight": 0.5,
        "preference_score_weight": 0.5,
        "model_version": "curve-pref-test",
    }
    snapshot.update(overrides)
    return snapshot


def _candidate(**overrides) -> dict:
    candidate = {
        "base_score": 0.60,
        "gif_worthiness": 0.60,
        "candidate_vector": [1.0, 0.0],
        "scenario_keys": ["tag:test"],
    }
    candidate.update(overrides)
    return candidate


def test_unqualified_curve_never_changes_output():
    assert curve_route("active", False, 1.0, True, True) == "BASELINE"
    assert curve_route("active", True, 0.0, True, True) == "BASELINE"
    assert curve_route("active", True, 1.0, True, False) == "BASELINE"
    assert curve_route("active", True, 1.0, False, True) == "BASELINE"


def test_curve_route_modes():
    # shadow mode
    assert curve_route("shadow", False, 0.0, True, True) == "SHADOW"
    assert curve_route("shadow", True, 1.0, True, True) == "SHADOW"

    # off mode
    assert curve_route("off", True, 1.0, True, True) == "BASELINE"
    assert curve_route("disabled", True, 1.0, True, True) == "BASELINE"

    # active mode with all conditions met
    assert curve_route("active", True, 0.85, True, True) == "ACTIVE"


def test_score_curve_candidate_baseline_fallback():
    reranker = _CurveAwareReranker()
    snapshot = _snapshot(mode="off", qualified=False, confidence=0.0, reranker=reranker)
    res = score_curve_candidate(
        None, snapshot, _candidate(base_score=0.70, gif_worthiness=0.70)
    )
    assert res["route"] == "BASELINE"
    # No curve evidence: nominal/effective stay empty and the production
    # score is the shared-scorer baseline (0.5*0.70 + 0.5*0.5 = 0.60).
    assert res["baseline_score"] == pytest.approx(0.5 * 0.70 + 0.5 * 0.50)
    assert res["final_score"] == res["baseline_score"]
    assert res["nominal_shadow_score"] is None
    assert res["effective_shadow_score"] is None
    assert "curve_evidence_unavailable" in res["inactive_reasons"]


def test_score_curve_candidate_shadow_never_alters_final_score():
    """Shadow keeps the production final score at the no-curve baseline while
    still recording nominal/effective diagnostics on the same scale."""
    reranker = _CurveAwareReranker()
    snapshot = _snapshot(mode="shadow", qualified=False, confidence=0.0, reranker=reranker)
    res = score_curve_candidate(
        None, snapshot, _candidate(curve_probability=0.90)
    )
    assert res["route"] == "SHADOW"
    assert res["final_score"] == res["baseline_score"]
    # nominal (confidence 1.0) pulls the preference signal up: 0.5 -> 0.7
    assert res["nominal_score"] == pytest.approx(0.5 * 0.60 + 0.5 * 0.70)
    # effective at confidence 0.0 equals the baseline
    assert res["effective_score"] == res["baseline_score"]
    # all three routes went through the one shared scorer
    assert len(reranker.calls) == 3


def test_score_curve_candidate_active_updates_final_score():
    reranker = _CurveAwareReranker()
    snapshot = _snapshot(
        mode="active", qualified=True, confidence=0.8,
        compatible=True, deployment_available=True, reranker=reranker,
    )
    res = score_curve_candidate(
        None, snapshot, _candidate(curve_probability=0.90)
    )
    assert res["route"] == "ACTIVE"
    # effective profile: 0.5 + 0.2*0.8 = 0.66 -> blend(0.6, 0.66) = 0.63
    assert res["effective_score"] == pytest.approx(0.5 * 0.60 + 0.5 * 0.66)
    assert res["final_score"] == res["effective_score"]
    assert res["production_score"] == res["effective_score"]


def test_active_without_deployment_degrades_to_baseline():
    reranker = _CurveAwareReranker()
    snapshot = _snapshot(
        mode="active", qualified=True, confidence=1.0,
        compatible=True, deployment_available=False, reranker=reranker,
    )
    res = score_curve_candidate(
        None, snapshot, _candidate(curve_probability=0.90)
    )
    assert res["route"] == "BASELINE"
    assert res["final_score"] == res["baseline_score"]
    assert "deployment_unavailable" in res["inactive_reasons"]


def test_curve_online_uses_shared_export_scorer(monkeypatch):
    """R4: all three score routes must flow through
    score_preference_export_candidate, never a private linear blend
    (0.9*80 + 0.1*1.0 == 72.1 must not happen again)."""
    calls = []

    def shared_score(**kwargs):
        calls.append(kwargs)
        return {"final_score": 81.25, "profile_score": 0.73}

    monkeypatch.setattr(
        "app.services.curve_online.score_preference_export_candidate",
        shared_score,
        raising=False,
    )
    result = score_curve_candidate(
        None,
        {
            "mode": "active",
            "qualified": True,
            "confidence": 1.0,
            "compatible": True,
            "nominal_weight": 0.10,
        },
        {"base_score": 80.0, "curve_probability": 1.0},
    )
    assert calls
    assert result["final_score"] == pytest.approx(81.25)


def test_online_baseline_matches_replay_within_tolerance():
    """Task 4 Step 5: the online baseline/nominal/effective numbers are the
    qualification-replay numbers (same shared scorer, same inputs)."""
    from app.services.reranker import score_preference_export_candidate

    reranker = _CurveAwareReranker()
    snapshot = _snapshot(
        mode="shadow", qualified=False, confidence=0.6, reranker=reranker
    )
    candidate = _candidate(curve_probability=0.73)
    online = score_curve_candidate(None, snapshot, candidate)

    def replay(confidence, probability):
        return float(
            score_preference_export_candidate(
                reranker=reranker,
                candidate_vector=candidate["candidate_vector"],
                gif_worthiness=candidate["gif_worthiness"],
                scenario_keys=candidate["scenario_keys"],
                profile_version=snapshot["profile_version"],
                base_export_score=candidate["base_score"],
                base_score_weight=snapshot["base_score_weight"],
                preference_score_weight=snapshot["preference_score_weight"],
                curve_probability=probability,
                curve_model_version=snapshot["model_version"],
                curve_model_confidence=confidence,
            )["final_score"]
        )

    assert online["baseline_score"] == pytest.approx(replay(None, None), abs=1e-12)
    assert online["nominal_score"] == pytest.approx(replay(1.0, 0.73), abs=1e-12)
    assert online["effective_score"] == pytest.approx(replay(0.6, 0.73), abs=1e-12)


def test_load_curve_runtime_snapshot_without_deployment():
    conn = sqlite3.connect(":memory:")
    apply_preference_schema(conn)
    conn.execute(
        "INSERT INTO preference_profile_current (slot, profile_version) "
        "VALUES ('current', 'profile_v1')"
    )
    conn.commit()

    config = {
        "curve_preference_enabled": True,
        "curve_preference_mode": "active",
        "base_score_weight": 0.7,
        "preference_score_weight": 0.3,
    }
    snapshot = load_curve_runtime_snapshot(conn, config)
    assert snapshot.mode == "active"
    assert snapshot.profile_version == "profile_v1"
    assert snapshot.deployment_available is False
    assert snapshot.qualified is False
    assert "deployment_unavailable" in snapshot.inactive_reasons

    # Disabled/off config resolves a no-op snapshot without DB reads.
    off = load_curve_runtime_snapshot(
        conn, {"curve_preference_enabled": False, "curve_preference_mode": "active"}
    )
    assert off.mode == "off"


def test_load_curve_runtime_snapshot_binds_deployment_identity():
    conn = sqlite3.connect(":memory:")
    apply_preference_schema(conn)
    conn.execute(
        "INSERT INTO preference_profile_current (slot, profile_version) "
        "VALUES ('current', 'profile_v1')"
    )
    conn.execute(
        """
        INSERT INTO curve_preference_models (
            model_version, feature_version, extractor_sha256, status,
            coefficients_json, intercept, feature_mean_json, feature_std_json,
            feature_names_json, l2_lambda, training_event_watermark,
            training_source_set_sha256, holdout_row_count, metrics_json,
            curve_model_confidence
        ) VALUES ('mv-1', 'curve-source-v1', ?, 'PUBLISHED',
            '{}', 0.0, '{}', '{}', '[]', 1.0, 'wm', 'ts', 50, '{}', 0.8)
        """,
        ("e" * 64,),
    )
    conn.execute(
        """
        INSERT INTO curve_preference_qualification_runs (
            qualification_id, model_version, status, input_manifest_sha256,
            profile_snapshot_sha256, baseline_scores_sha256, proposed_scores_sha256,
            metrics_json, report_path, report_sha256, source_db_snapshot_sha256,
            created_at
        ) VALUES ('q-1', 'mv-1', 'QUALIFIED', 'in', 'prof', 'b', 'p', '{}',
            'r', 'rs', 'db', '2026-09-09T00:00:00Z')
        """
    )
    conn.execute(
        """
        INSERT INTO curve_model_deployments (
            model_version, qualification_id, config_hash, extractor_sha256,
            is_active, approved_by, approved_at
        ) VALUES ('mv-1', 'q-1', ?, ?, 1, 'op', '2026-09-09T00:00:00Z')
        """,
        ("not-the-algorithm-hash", "e" * 64),
    )
    conn.commit()

    config = {
        "curve_preference_enabled": True,
        "curve_preference_mode": "active",
    }
    snapshot = load_curve_runtime_snapshot(conn, config)
    assert snapshot.model_version == "mv-1"
    assert snapshot.deployment_revision == 1
    assert snapshot.confidence == 0.8
    # The deployment config hash does not match the frozen algorithm config:
    # fail closed, no ACTIVE route.
    assert snapshot.compatible is False
    assert "algorithm_config_mismatch" in snapshot.inactive_reasons


def test_validate_deployment():
    from app.services.curve_online import validate_deployment

    assert "model_not_published" in validate_deployment("DRAFT", True, True)
    assert "model_not_qualified" in validate_deployment("PUBLISHED", False, True)
    assert "config_mismatch" in validate_deployment("PUBLISHED", True, False)
    assert validate_deployment("PUBLISHED", True, True) == []


def _draft_model_db(model_version: str = "draft-shadow-1"):
    """A library DB with a published profile and a DRAFT source-domain model."""
    import json as _json

    from app.services.curve_source_features import CURVE_SOURCE_FEATURE_NAMES

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_preference_schema(conn)
    conn.execute(
        "INSERT INTO preference_profile_current (slot, profile_version) "
        "VALUES ('current', 'profile_v1')"
    )
    conn.execute(
        "INSERT INTO preference_profiles (profile_version, scope, scenario_key, "
        "like_count, dislike_count, neutral_count, confidence, liked_centroid_blob) "
        "VALUES ('profile_v1', 'global', NULL, 3, 1, 0, 0.9, ?)",
        (bytes(range(128)),),
    )
    from app.services.curve_preference import canonical_json_sha256

    manifest = {"feature_version": "curve-source-v1", "sources": [], "rows": []}
    manifest_sha = canonical_json_sha256(manifest)
    coefficients = {name: 0.1 for name in CURVE_SOURCE_FEATURE_NAMES}
    conn.execute(
        """
        INSERT INTO curve_preference_models (
            model_version, feature_version, extractor_sha256, status,
            coefficients_json, intercept, feature_mean_json, feature_std_json,
            feature_names_json, l2_lambda, training_event_watermark,
            training_source_set_sha256, holdout_row_count, metrics_json
        ) VALUES (?, 'curve-source-v1', ?, 'DRAFT', ?, 0.0, ?, ?, ?, 1.0,
            'wm', 'ts', 40, '{}')
        """,
        (
            model_version,
            "e" * 64,
            _json.dumps(coefficients),
            _json.dumps({name: 0.5 for name in CURVE_SOURCE_FEATURE_NAMES}),
            _json.dumps({name: 0.2 for name in CURVE_SOURCE_FEATURE_NAMES}),
            _json.dumps(list(CURVE_SOURCE_FEATURE_NAMES)),
        ),
    )
    conn.execute(
        "UPDATE curve_preference_models SET training_manifest_json = ?, "
        "holdout_manifest_json = ?, training_manifest_sha256 = ?, "
        "holdout_manifest_sha256 = ? WHERE model_version = ?",
        (
            _json.dumps(manifest, sort_keys=True),
            _json.dumps(manifest, sort_keys=True),
            manifest_sha,
            manifest_sha,
            model_version,
        ),
    )
    conn.commit()
    return conn


def test_draft_shadow_model_reaches_scoring_snapshot(tmp_path):
    """P1 remediation: with no deployment, a configured DRAFT shadow model
    must appear in the runtime snapshot so the shared scorer accepts the
    curve inputs instead of marking them partial."""
    import numpy as np

    from app.pipeline.ranking import _curve_video_context
    from app.services.curve_source_features import CURVE_SOURCE_FEATURE_NAMES

    conn = _draft_model_db()
    cfg = {
        "curve_preference_enabled": True,
        "curve_preference_mode": "shadow",
        "curve_shadow_model_version": "draft-shadow-1",
    }
    ctx = _curve_video_context(conn, cfg)
    assert ctx is not None
    loaded = ctx["model"]
    assert loaded is not None
    snapshot = ctx["snapshot"]
    assert snapshot.model_version == "draft-shadow-1"
    assert snapshot.mode == "shadow"

    features = {name: 0.8 for name in CURVE_SOURCE_FEATURE_NAMES}
    from app.pipeline.ranking import _curve_probability_from_clip

    probability = _curve_probability_from_clip(ctx, {"source_features": features})
    assert probability is not None

    result = score_curve_candidate(
        conn,
        snapshot,
        {
            "base_score": 0.60,
            "gif_worthiness": 0.60,
            "candidate_vector": np.asarray([1.0, 0.0], dtype=np.float32),
            "scenario_keys": ["tag:test"],
            "curve_probability": probability,
        },
    )
    assert result["route"] == "SHADOW"
    # The curve inputs were accepted (not partial): nominal differs from baseline.
    assert result["nominal_score"] is not None
    assert result["nominal_score"] != result["baseline_score"]
    assert result["final_score"] == result["baseline_score"]
    conn.close()
