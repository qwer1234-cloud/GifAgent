"""Tests for curve online routing, shadow/active scoring, and config binding."""
from __future__ import annotations

import pytest

from app.services.curve_online import (
    curve_route,
    score_curve_candidate,
)


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
    candidate = {
        "candidate_id": "c1",
        "gif_worthiness": 0.70,
        "base_score": 0.70,
    }
    snapshot = {
        "mode": "off",
        "qualified": False,
        "confidence": 0.0,
    }
    res = score_curve_candidate(None, snapshot, candidate)
    assert res["route"] == "BASELINE"
    assert res["final_score"] == 0.70
    assert res["nominal_shadow_score"] is None
    assert res["effective_shadow_score"] is None


def test_score_curve_candidate_shadow_mode_does_not_alter_final_score():
    candidate = {
        "candidate_id": "c2",
        "base_score": 0.60,
        "curve_probability": 0.90,
    }
    snapshot = {
        "mode": "shadow",
        "qualified": False,  # DRAFT or unqualified
        "confidence": 0.0,
        "nominal_weight": 0.10,
    }
    res = score_curve_candidate(None, snapshot, candidate)
    assert res["route"] == "SHADOW"
    # In shadow mode, production final score MUST remain exactly 0.60!
    assert res["final_score"] == 0.60
    # Shadow scores computed for diagnostic purposes:
    # nominal = 0.9 * 0.6 + 0.1 * 0.9 = 0.54 + 0.09 = 0.63
    assert pytest.approx(res["nominal_shadow_score"], 1e-4) == 0.63
    # effective with 0 confidence = 0.60
    assert pytest.approx(res["effective_shadow_score"], 1e-4) == 0.60


def test_score_curve_candidate_active_mode_updates_final_score():
    candidate = {
        "candidate_id": "c3",
        "base_score": 0.60,
        "curve_probability": 0.90,
    }
    snapshot = {
        "mode": "active",
        "qualified": True,
        "confidence": 0.80,
        "compatible": True,
        "quality_pass": True,
        "nominal_weight": 0.10,
    }
    res = score_curve_candidate(None, snapshot, candidate)
    assert res["route"] == "ACTIVE"
    # effective weight = 0.10 * 0.80 = 0.08
    # final_score = 0.92 * 0.60 + 0.08 * 0.90 = 0.552 + 0.072 = 0.624
    assert pytest.approx(res["final_score"], 1e-4) == 0.624


def test_validate_deployment():
    from app.services.curve_online import validate_deployment

    assert "model_not_published" in validate_deployment("DRAFT", True, True)
    assert "model_not_qualified" in validate_deployment("PUBLISHED", False, True)
    assert "config_mismatch" in validate_deployment("PUBLISHED", True, False)
    assert validate_deployment("PUBLISHED", True, True) == []


# ── Task 0 regression (R4): one shared production scorer ────────────────────


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

