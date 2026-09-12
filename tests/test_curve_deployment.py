"""Tests for curve deployment activation, rollback, and revision compare-and-swap.

Deployment is bundle-driven: a PUBLISHED model can only be activated from a
verified qualification bundle with a bound runtime config and a row-exact
score replay; refusals leave the deployment state untouched.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app.services.curve_online import (
    _canonical_config_hash,
    activate_deployment,
    curve_algorithm_config,
    load_curve_runtime_snapshot,
    rollback_deployment,
)
from app.services.preference_schema import apply_preference_schema

from tests.test_curve_preference_qualification import (  # noqa: F401
    _file_source_db,
)


def _promoted_fixture(tmp_path: Path) -> tuple[sqlite3.Connection, Path, str]:
    """A PUBLISHED model plus its verified qualification bundle directory."""
    from app.services.curve_preference import promote_model
    from app.services.curve_preference_qualification import execute_qualification_run

    db_path = _file_source_db(tmp_path)
    result = execute_qualification_run(
        db_path=db_path,
        model_version="m-qual",
        work_dir=tmp_path / "runs",
        qualification_id="q-accept",
    )
    assert result["status"] == "QUALIFIED"
    run_dir = Path(result["run_dir"])

    conn = sqlite3.connect(str(db_path))
    promoted = promote_model(conn, model_version="m-qual", run_dir=run_dir)
    assert promoted["status"] == "PROMOTED"
    return conn, run_dir, "m-qual"


def _runtime_config(conn: sqlite3.Connection, run_dir: Path, model_version: str) -> dict:
    """Runtime config whose algorithm hash is derived from the bundle exactly
    the way activation derives it."""
    input_manifest = json.loads(
        (run_dir / "input_manifest.json").read_text(encoding="utf-8")
    )
    feature_version = conn.execute(
        "SELECT feature_version FROM curve_preference_models WHERE model_version = ?",
        (model_version,),
    ).fetchone()[0]
    extractor = conn.execute(
        "SELECT extractor_sha256 FROM curve_preference_models WHERE model_version = ?",
        (model_version,),
    ).fetchone()[0]
    expected_hash = _canonical_config_hash(
        curve_algorithm_config(
            {
                "base_score_weight": input_manifest["target_base_score_weight"],
                "preference_score_weight": input_manifest["target_preference_score_weight"],
                "curve_feature_version": feature_version,
            }
        )
    )
    return {
        "algorithm_config_hash": expected_hash,
        "base_score_weight": input_manifest["target_base_score_weight"],
        "preference_score_weight": input_manifest["target_preference_score_weight"],
        "feature_version": feature_version,
        "extractor_sha256": extractor,
    }


def test_activate_and_rollback_deployment(tmp_path):
    conn, run_dir, model_version = _promoted_fixture(tmp_path)
    config = _runtime_config(conn, run_dir, model_version)

    result = activate_deployment(
        conn,
        qualification_dir=run_dir,
        expected_revision=0,
        operator="admin",
        runtime_config=config,
    )
    assert result.status == "ACTIVATED"
    rev1 = result.revision
    assert rev1 == 1

    pointer = conn.execute(
        "SELECT revision FROM curve_model_deployment_current WHERE slot='current'"
    ).fetchone()[0]
    assert pointer == rev1
    row = conn.execute(
        "SELECT model_version, qualification_bundle_path, qualification_bundle_sha256 "
        "FROM curve_model_deployments WHERE revision = ?",
        (rev1,),
    ).fetchone()
    assert row[0] == model_version
    assert row[1] and row[2]

    # The frozen snapshot resolves the active deployment through the pointer.
    snapshot = load_curve_runtime_snapshot(
        conn,
        {"curve_preference_enabled": True, "curve_preference_mode": "active"},
    )
    assert snapshot.model_version == model_version
    assert snapshot.deployment_revision == rev1

    # Stale expected revision conflicts.
    conflict = activate_deployment(
        conn,
        qualification_dir=run_dir,
        expected_revision=0,
        operator="admin",
        runtime_config=config,
    )
    assert conflict.status == "CONFLICT"

    # Rollback to baseline appends an audited revision and clears the model.
    rb = rollback_deployment(
        conn,
        target_revision=0,
        expected_revision=rev1,
        operator="admin",
        runtime_config=config,
    )
    assert rb.status == "ROLLED_BACK"
    rev2 = rb.revision
    assert rev2 == 2
    active = conn.execute(
        "SELECT COUNT(*) FROM curve_model_deployments WHERE is_active = 1 AND "
        "model_version != 'none'"
    ).fetchone()[0]
    assert active == 0
    snapshot_after = load_curve_runtime_snapshot(
        conn,
        {"curve_preference_enabled": True, "curve_preference_mode": "active"},
    )
    assert snapshot_after.deployment_available is False
    conn.close()


def test_activate_refuses_tampered_runtime_config(tmp_path):
    conn, run_dir, model_version = _promoted_fixture(tmp_path)
    config = _runtime_config(conn, run_dir, model_version)
    config["algorithm_config_hash"] = "c" * 64

    result = activate_deployment(
        conn,
        qualification_dir=run_dir,
        expected_revision=0,
        operator="admin",
        runtime_config=config,
    )
    assert result.status == "REFUSED"
    assert "algorithm_config_mismatch" in result.reasons
    # No half-applied state: nothing active, no pointer.
    active = conn.execute(
        "SELECT COUNT(*) FROM curve_model_deployments WHERE is_active = 1"
    ).fetchone()[0]
    assert active == 0
    pointer = conn.execute(
        "SELECT COUNT(*) FROM curve_model_deployment_current WHERE slot='current'"
    ).fetchone()[0]
    assert pointer == 0
    conn.close()


def test_activate_refuses_missing_or_wrong_extractor_identity(tmp_path):
    conn, run_dir, model_version = _promoted_fixture(tmp_path)
    config = _runtime_config(conn, run_dir, model_version)
    config["extractor_sha256"] = "f" * 64

    result = activate_deployment(
        conn,
        qualification_dir=run_dir,
        expected_revision=0,
        operator="admin",
        runtime_config=config,
    )
    assert result.status == "REFUSED"
    assert "extractor_sha256_mismatch" in result.reasons
    conn.close()


def test_activate_refuses_tampered_bundle(tmp_path):
    conn, run_dir, model_version = _promoted_fixture(tmp_path)
    config = _runtime_config(conn, run_dir, model_version)

    report = run_dir / "report.md"
    report.write_text(
        report.read_text(encoding="utf-8") + "\ntampered\n", encoding="utf-8"
    )

    result = activate_deployment(
        conn,
        qualification_dir=run_dir,
        expected_revision=0,
        operator="admin",
        runtime_config=config,
    )
    assert result.status == "REFUSED"
    assert any("bundle" in reason or "hash" in reason for reason in result.reasons)
    conn.close()


def test_activate_refuses_drafted_scores_drift(tmp_path):
    """Rewriting a replay score file breaks the hash binding: refused."""
    conn, run_dir, model_version = _promoted_fixture(tmp_path)
    config = _runtime_config(conn, run_dir, model_version)

    baseline_path = run_dir / "baseline_scores.jsonl"
    rows = [
        json.loads(line)
        for line in baseline_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    rows[0]["baseline_score"] = 0.123456
    baseline_path.write_text(
        "\n".join(json.dumps(r, sort_keys=True) for r in rows), encoding="utf-8"
    )

    result = activate_deployment(
        conn,
        qualification_dir=run_dir,
        expected_revision=0,
        operator="admin",
        runtime_config=config,
    )
    assert result.status == "REFUSED"
    conn.close()


def test_concurrent_activation_single_winner(tmp_path):
    conn1, run_dir, model_version = _promoted_fixture(tmp_path)
    config = _runtime_config(conn1, run_dir, model_version)
    db_path = conn1.execute("PRAGMA database_list").fetchone()[2]
    conn2 = sqlite3.connect(db_path)

    winner = activate_deployment(
        conn1,
        qualification_dir=run_dir,
        expected_revision=0,
        operator="w1",
        runtime_config=config,
    )
    loser = activate_deployment(
        conn2,
        qualification_dir=run_dir,
        expected_revision=0,
        operator="w2",
        runtime_config=config,
    )
    assert winner.status == "ACTIVATED"
    assert loser.status == "CONFLICT"
    conn1.close()
    conn2.close()


def test_rollback_to_model_revision_revalidates_bundle(tmp_path):
    conn, run_dir, model_version = _promoted_fixture(tmp_path)
    config = _runtime_config(conn, run_dir, model_version)

    first = activate_deployment(
        conn,
        qualification_dir=run_dir,
        expected_revision=0,
        operator="admin",
        runtime_config=config,
    )
    assert first.status == "ACTIVATED"

    # Baseline rollback, then rollback back to the model revision 1: the
    # bundle is re-verified and activation is restored.
    baseline = rollback_deployment(
        conn, target_revision=0, expected_revision=first.revision,
        operator="admin", runtime_config=config,
    )
    assert baseline.status == "ROLLED_BACK"

    restore = rollback_deployment(
        conn, target_revision=first.revision, expected_revision=baseline.revision,
        operator="admin", runtime_config=config,
    )
    assert restore.status == "ROLLED_BACK"
    snapshot = load_curve_runtime_snapshot(
        conn,
        {"curve_preference_enabled": True, "curve_preference_mode": "active"},
    )
    assert snapshot.model_version == model_version

    # Tampered bundle: rollback to that revision is refused.
    tampered = run_dir / "metrics.json"
    metrics = json.loads(tampered.read_text(encoding="utf-8"))
    metrics["effective_confidence"] = 0.5
    tampered.write_text(json.dumps(metrics, sort_keys=True), encoding="utf-8")

    latest = conn.execute(
        "SELECT revision FROM curve_model_deployment_current WHERE slot='current'"
    ).fetchone()[0]
    refused = rollback_deployment(
        conn, target_revision=first.revision, expected_revision=latest,
        operator="admin", runtime_config=config,
    )
    assert refused.status == "REFUSED"
    conn.close()


def test_draft_is_not_deployable(tmp_path):
    conn = sqlite3.connect(":memory:")
    apply_preference_schema(conn)
    conn.execute(
        """
        INSERT INTO curve_preference_models (
            model_version, feature_version, extractor_sha256, status,
            coefficients_json, intercept, feature_mean_json, feature_std_json,
            feature_names_json, l2_lambda, training_event_watermark,
            training_source_set_sha256, holdout_row_count, metrics_json
        ) VALUES ('draft-1', 'curve-v1', ?, 'DRAFT', '{}', 0.0, '{}', '{}',
            '[]', 1.0, 'wm', 'ts', 50, '{}')
        """,
        ("b" * 64,),
    )
    conn.commit()

    result = activate_deployment(
        conn,
        qualification_dir=tmp_path / "missing-bundle",
        expected_revision=0,
        operator="admin",
        runtime_config={"algorithm_config_hash": "c" * 64},
    )
    assert result.status == "REFUSED"
    conn.close()


def test_profile_drift_degrades_active_to_baseline(tmp_path):
    """P1 remediation: after deployment, publishing a NEW profile must
    degrade ACTIVE to BASELINE until re-qualification; restoring the
    qualified profile identity restores ACTIVE."""
    conn, run_dir, model_version = _promoted_fixture(tmp_path)
    config = _runtime_config(conn, run_dir, model_version)

    bundle_profile_version = json.loads(
        (run_dir / "profile_snapshot.json").read_text(encoding="utf-8")
    )["profile_version"]

    result = activate_deployment(
        conn,
        qualification_dir=run_dir,
        expected_revision=0,
        operator="admin",
        runtime_config=config,
    )
    assert result.status == "ACTIVATED"

    def snapshot_for(profile_version):
        if profile_version is not None:
            conn.execute("DELETE FROM preference_profile_current")
            conn.execute(
                "INSERT INTO preference_profile_current (slot, profile_version) "
                "VALUES ('current', ?)",
                (profile_version,),
            )
            conn.commit()
        return load_curve_runtime_snapshot(
            conn,
            {"curve_preference_enabled": True, "curve_preference_mode": "active"},
        )

    # No current profile at all: drift -> BASELINE.
    none_snapshot = snapshot_for(None)
    assert "profile_version_mismatch" in none_snapshot.inactive_reasons

    # A newer published profile: drift -> BASELINE.
    newer = snapshot_for("profile-v2-after-qualification")
    assert "profile_version_mismatch" in newer.inactive_reasons
    assert newer.compatible is False

    # The qualified profile identity: ACTIVE again.
    qualified = snapshot_for(bundle_profile_version)
    assert "profile_version_mismatch" not in qualified.inactive_reasons
    assert qualified.model_version == model_version
    conn.close()
