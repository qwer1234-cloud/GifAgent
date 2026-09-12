"""Tests for curve model deployment activation, rollback, and revision compare-and-swap."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.services.curve_online import (
    activate_deployment,
    rollback_deployment,
    validate_deployment,
)
from app.services.preference_schema import apply_preference_schema


def test_draft_is_not_deployable():
    assert "model_not_published" in validate_deployment("DRAFT", True, True)


def test_activate_and_rollback_deployment():
    conn = sqlite3.connect(":memory:")
    apply_preference_schema(conn)

    # Insert a published model and qualification record
    conn.execute(
        """
        INSERT INTO curve_preference_models (
            model_version, feature_version, extractor_sha256, status,
            coefficients_json, intercept, feature_mean_json, feature_std_json,
            feature_names_json, l2_lambda, training_event_watermark,
            training_source_set_sha256, holdout_row_count, metrics_json
        ) VALUES (
            'curve-v1-pub', 'curve-v1', 'ext_sha', 'PUBLISHED',
            '{}', 0.0, '{}', '{}', '[]', 1.0, 'wm', 'ts', 50, '{}'
        )
        """
    )
    conn.execute(
        """
        INSERT INTO curve_preference_qualification_runs (
            qualification_id, model_version, status, input_manifest_sha256,
            profile_snapshot_sha256, baseline_scores_sha256, proposed_scores_sha256,
            metrics_json, report_path, report_sha256, source_db_snapshot_sha256,
            created_at
        ) VALUES (
            'qual_001', 'curve-v1-pub', 'QUALIFIED', 'in_sha',
            'prof_sha', 'base_sha', 'prop_sha', '{}', 'rep_path', 'rep_sha', 'db_sha',
            '2026-09-09T00:00:00Z'
        )
        """
    )
    conn.commit()

    # Initial activation from revision 0
    res = activate_deployment(conn, qualification_id="qual_001", expected_revision=0, operator="admin")
    assert res["status"] == "ACTIVATED"
    rev1 = res["revision"]
    assert rev1 == 1

    # Conflict on wrong expected revision
    conflict_res = activate_deployment(conn, qualification_id="qual_001", expected_revision=0, operator="admin")
    assert conflict_res["status"] == "CONFLICT"

    # Rollback to revision 0 (deactivation)
    rb_res = rollback_deployment(conn, target_revision=0, expected_revision=rev1, operator="admin")
    assert rb_res["status"] == "ROLLED_BACK"
    rev2 = rb_res["revision"]
    assert rev2 == 2

    # Check active status in deployments table
    active_row = conn.execute("SELECT model_version, is_active FROM curve_model_deployments WHERE is_active = 1").fetchone()
    assert active_row is None


# ── Task 0 regression (R5): deployment re-validates qualification evidence ──


def test_activate_deployment_revalidates_qualification_bundle(tmp_path):
    """R5: activation must take a qualification bundle directory plus the
    runtime config, replay the qualification evidence, and refuse when the
    algorithm config hash drifts -- never trust config_matches=True."""
    from app.services.curve_online import activate_deployment

    conn = sqlite3.connect(":memory:")
    apply_preference_schema(conn)
    conn.execute(
        """
        INSERT INTO curve_preference_models (
            model_version, feature_version, extractor_sha256, status,
            coefficients_json, intercept, feature_mean_json, feature_std_json,
            feature_names_json, l2_lambda, training_event_watermark,
            training_source_set_sha256, holdout_row_count, metrics_json
        ) VALUES (?, ?, ?, 'PUBLISHED', '{}', 0.0, '{}', '{}', '[]', 1.0, 'wm', 'ts', 50, '{}')
        """,
        ("curve-v1-pub", "curve-v1", "b" * 64),
    )
    conn.execute(
        """
        INSERT INTO curve_preference_qualification_runs (
            qualification_id, model_version, status, input_manifest_sha256,
            profile_snapshot_sha256, baseline_scores_sha256, proposed_scores_sha256,
            metrics_json, report_path, report_sha256, source_db_snapshot_sha256,
            created_at
        ) VALUES ('qual_001', 'curve-v1-pub', 'QUALIFIED', 'in_sha',
            'prof_sha', 'base_sha', 'prop_sha', '{}', 'rep_path', 'rep_sha', 'db_sha',
            '2026-09-09T00:00:00Z')
        """
    )
    conn.commit()

    bundle_dir = tmp_path / "qual-bundle"
    bundle_dir.mkdir()

    result = activate_deployment(
        conn,
        qualification_dir=bundle_dir,
        expected_revision=0,
        operator="admin",
        runtime_config={
            "algorithm_config_hash": "c" * 64,
            "feature_version": "curve-v1",
            "extractor_sha256": "b" * 64,
        },
    )
    assert result.status == "REFUSED"
    active = conn.execute(
        "SELECT COUNT(*) FROM curve_model_deployments WHERE is_active = 1"
    ).fetchone()[0]
    assert active == 0
