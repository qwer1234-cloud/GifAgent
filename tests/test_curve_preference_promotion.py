"""Promotion gate tests: only a verified QUALIFIED bundle can publish."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app.services.curve_preference import (
    load_qualification_bundle,
    promote_model,
)
from app.services.preference_schema import apply_preference_schema
from app.services.curve_preference_qualification import (
    execute_qualification_run,
    logical_source_identity,
)

from tests.test_curve_preference_qualification import (  # noqa: F401
    _file_source_db,
)


@pytest.fixture
def qualified_run(tmp_path: Path) -> tuple[Path, Path]:
    db_path = _file_source_db(tmp_path)
    result = execute_qualification_run(
        db_path=db_path,
        model_version="m-qual",
        work_dir=tmp_path / "runs",
        qualification_id="q-accept",
    )
    assert result["status"] == "QUALIFIED"
    return db_path, Path(result["run_dir"])


def test_promotion_publishes_exactly_one_model(qualified_run):
    db_path, run_dir = qualified_run
    conn = sqlite3.connect(str(db_path))

    result = promote_model(
        conn, model_version="m-qual", run_dir=run_dir
    )

    assert result["status"] == "PROMOTED"
    rows = conn.execute(
        "SELECT model_version, status, curve_model_confidence "
        "FROM curve_preference_models ORDER BY model_version"
    ).fetchall()
    published = [r for r in rows if r[1] == "PUBLISHED"]
    assert len(published) == 1
    assert published[0][0] == "m-qual"
    assert published[0][2] is not None
    runs = conn.execute(
        "SELECT qualification_id, status FROM "
        "curve_preference_qualification_runs"
    ).fetchall()
    assert runs == [("q-accept", "QUALIFIED")]
    conn.close()


def test_promotion_is_refused_for_not_qualified_bundle(tmp_path):
    db_path = _file_source_db(tmp_path)
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "UPDATE candidate_gifs SET artifact_path = :p "
        "WHERE candidate_id = 'cand-q-00'",
        {"p": str(tmp_path / "gone.gif")},
    )
    conn.commit()
    conn.close()

    result = execute_qualification_run(
        db_path=db_path, model_version="m-qual",
        work_dir=tmp_path / "runs", qualification_id="q-nq",
    )
    assert result["status"] == "NOT_QUALIFIED"

    conn = sqlite3.connect(str(db_path))
    refusal = promote_model(
        conn, model_version="m-qual",
        run_dir=Path(result["run_dir"]),
    )
    conn.close()

    assert refusal["status"] == "REFUSED"
    assert "qualification_not_qualified" in refusal["reasons"]


def test_promotion_refuses_tampered_report(qualified_run):
    db_path, run_dir = qualified_run
    report = run_dir / "report.md"
    report.write_text(
        report.read_text(encoding="utf-8") + "\ntampered\n",
        encoding="utf-8",
    )
    conn = sqlite3.connect(str(db_path))

    bundle, reasons = load_qualification_bundle(run_dir)

    assert bundle is None
    assert "bundle_hash_mismatch" in reasons
    result = promote_model(conn, model_version="m-qual", run_dir=run_dir)
    assert result["status"] == "REFUSED"
    conn.close()


def test_promotion_refuses_model_row_changed_after_qualification(qualified_run):
    db_path, run_dir = qualified_run
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "UPDATE curve_preference_models SET intercept = intercept + 0.5 "
        "WHERE model_version = 'm-qual'"
    )
    conn.commit()

    result = promote_model(conn, model_version="m-qual", run_dir=run_dir)

    assert result["status"] == "REFUSED"
    assert "model_row_changed_after_qualification" in result["reasons"]
    conn.close()


def test_promotion_refuses_qualification_id_conflict(qualified_run):
    db_path, run_dir = qualified_run
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        INSERT INTO curve_preference_qualification_runs (
            qualification_id, model_version, status, input_manifest_sha256,
            profile_snapshot_sha256, baseline_scores_sha256,
            proposed_scores_sha256, metrics_json, report_path, report_sha256,
            source_db_snapshot_sha256, created_at
        ) VALUES (
            'q-accept', 'm-qual', 'QUALIFIED', 'different', 'x', 'x', 'x',
            '{}', 'x', 'x', 'x', '2026-01-01'
        )
        """
    )
    conn.commit()

    result = promote_model(conn, model_version="m-qual", run_dir=run_dir)

    assert result["status"] == "REFUSED"
    assert "qualification_id_conflict" in result["reasons"]
    conn.close()


def test_promotion_refuses_when_model_not_draft(qualified_run):
    db_path, run_dir = qualified_run
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "UPDATE curve_preference_models SET status = 'PUBLISHED' "
        "WHERE model_version = 'm-qual'"
    )
    conn.commit()

    result = promote_model(conn, model_version="m-qual", run_dir=run_dir)

    assert result["status"] == "REFUSED"
    assert "model_not_draft" in result["reasons"]
    conn.close()


def test_promotion_refuses_wrong_model_version(qualified_run):
    db_path, run_dir = qualified_run
    conn = sqlite3.connect(str(db_path))

    result = promote_model(
        conn, model_version="some-other-model", run_dir=run_dir
    )

    assert result["status"] == "REFUSED"
    assert "qualification_model_mismatch" in result["reasons"] or (
        "model_not_draft" in result["reasons"]
    )
    conn.close()


def test_dry_run_promotion_writes_nothing(qualified_run):
    db_path, run_dir = qualified_run
    before = logical_source_identity(sqlite3.connect(str(db_path)), "m-qual")

    result = promote_model(
        sqlite3.connect(str(db_path)),
        model_version="m-qual",
        run_dir=run_dir,
        dry_run=True,
    )

    after = logical_source_identity(sqlite3.connect(str(db_path)), "m-qual")
    assert result["status"] == "DRY_RUN_OK"
    assert before == after
    conn = sqlite3.connect(str(db_path))
    rows = conn.execute(
        "SELECT status FROM curve_preference_models WHERE model_version='m-qual'"
    ).fetchone()[0]
    runs = conn.execute(
        "SELECT COUNT(*) FROM curve_preference_qualification_runs"
    ).fetchone()[0]
    conn.close()
    assert rows == "DRAFT"
    assert runs == 0


def test_promotion_supersedes_previous_published_model(tmp_path):
    db_path = _file_source_db(tmp_path)
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        INSERT INTO curve_preference_models (
            model_version, feature_version, extractor_sha256, status,
            coefficients_json, intercept, feature_mean_json, feature_std_json,
            feature_names_json, l2_lambda, training_event_watermark,
            training_source_set_sha256, holdout_row_count, metrics_json,
            curve_model_confidence, published_at
        ) VALUES (
            'm-old', 'curve-v1', 'ex', 'PUBLISHED', '{}', 0.0, '{}', '{}',
            '[]', 1.0, 'wm-old', 'src-old', 0, '{}', 0.5, '2026-08-01'
        )
        """
    )
    conn.commit()

    result = execute_qualification_run(
        db_path=db_path, model_version="m-qual",
        work_dir=tmp_path / "runs", qualification_id="q-supersede",
    )
    assert result["status"] == "QUALIFIED"

    promoted = promote_model(
        conn, model_version="m-qual",
        run_dir=Path(result["run_dir"]),
    )
    assert promoted["status"] == "PROMOTED"

    rows = conn.execute(
        "SELECT model_version, status FROM curve_preference_models"
    ).fetchall()
    published = [r for r in rows if r[1] == "PUBLISHED"]
    assert len(published) == 1 and published[0][0] == "m-qual"
    superseded = dict(rows)
    assert superseded["m-old"] == "SUPERSEDED"
    conn.close()


def test_promotion_refuses_tampered_confidence(qualified_run):
    db_path, run_dir = qualified_run
    metrics_file = run_dir / "metrics.json"
    metrics = json.loads(metrics_file.read_text(encoding="utf-8"))
    metrics["effective_confidence"] = 0.99
    metrics_file.write_text(json.dumps(metrics), encoding="utf-8")

    conn = sqlite3.connect(str(db_path))
    result = promote_model(conn, model_version="m-qual", run_dir=run_dir)
    conn.close()

    assert result["status"] == "REFUSED"
    assert "qualification_not_qualified" in result["reasons"]


def test_promotion_refuses_missing_required_artifact_hash(qualified_run):
    db_path, run_dir = qualified_run
    metrics_file = run_dir / "metrics.json"
    metrics = json.loads(metrics_file.read_text(encoding="utf-8"))
    del metrics["artifacts"]["baseline_scores_sha256"]
    metrics_file.write_text(json.dumps(metrics), encoding="utf-8")

    conn = sqlite3.connect(str(db_path))
    bundle, reasons = load_qualification_bundle(run_dir)
    assert bundle is None
    assert "bundle_hash_mismatch" in reasons
    result = promote_model(conn, model_version="m-qual", run_dir=run_dir)
    conn.close()

    assert result["status"] == "REFUSED"
    assert "bundle_hash_mismatch" in result["reasons"]


def test_promotion_recalculates_and_refuses_when_scores_regressed(qualified_run):
    db_path, run_dir = qualified_run
    # Invert nominal scores to force negative LCB upon recalculation
    nominal_file = run_dir / "nominal_scores.jsonl"
    lines = [json.loads(line) for line in nominal_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    for line in lines:
        line["nominal_score"] = 1.0 - float(line["nominal_score"])
    nominal_file.write_text("\n".join(json.dumps(l) for l in lines) + "\n", encoding="utf-8")

    # Update hash in metrics.json so hash passes, but recomputed gate fails
    import hashlib
    h = hashlib.sha256(nominal_file.read_bytes()).hexdigest()
    metrics_file = run_dir / "metrics.json"
    metrics = json.loads(metrics_file.read_text(encoding="utf-8"))
    metrics["artifacts"]["nominal_scores_sha256"] = h
    metrics_file.write_text(json.dumps(metrics), encoding="utf-8")

    conn = sqlite3.connect(str(db_path))
    result = promote_model(conn, model_version="m-qual", run_dir=run_dir)
    conn.close()

    assert result["status"] == "REFUSED"
    assert "qualification_not_qualified" in result["reasons"] or "nominal_pairwise_lcb_not_positive" in result["reasons"]


def test_promotion_refuses_partial_score_subset(qualified_run):
    """Refuse promotion if score files only contain a subset of the holdout manifest."""
    import hashlib

    db_path, run_dir = qualified_run
    # Keep only the first 2 candidates in baseline, nominal, effective score files
    for fname, score_key in (
        ("baseline_scores.jsonl", "baseline_score"),
        ("nominal_scores.jsonl", "nominal_score"),
        ("effective_scores.jsonl", "effective_score"),
    ):
        p = run_dir / fname
        lines = [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]
        subset = lines[:2]
        p.write_text("\n".join(json.dumps(l) for l in subset) + "\n", encoding="utf-8")
        h = hashlib.sha256(p.read_bytes()).hexdigest()
        metrics_file = run_dir / "metrics.json"
        metrics = json.loads(metrics_file.read_text(encoding="utf-8"))
        metrics["artifacts"][fname.replace(".jsonl", "_sha256")] = h
        metrics_file.write_text(json.dumps(metrics), encoding="utf-8")

    conn = sqlite3.connect(str(db_path))
    result = promote_model(conn, model_version="m-qual", run_dir=run_dir)
    conn.close()

    assert result["status"] == "REFUSED"
    assert "qualification_model_mismatch" in result["reasons"]


def test_promotion_refuses_score_label_drift(qualified_run):
    """Refuse promotion if score file changes candidate rating from manifest."""
    import hashlib

    db_path, run_dir = qualified_run
    p = run_dir / "baseline_scores.jsonl"
    lines = [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]
    lines[0]["rating"] = "dislike" if lines[0]["rating"] == "like" else "like"
    p.write_text("\n".join(json.dumps(l) for l in lines) + "\n", encoding="utf-8")
    h = hashlib.sha256(p.read_bytes()).hexdigest()

    metrics_file = run_dir / "metrics.json"
    metrics = json.loads(metrics_file.read_text(encoding="utf-8"))
    metrics["artifacts"]["baseline_scores_sha256"] = h
    metrics_file.write_text(json.dumps(metrics), encoding="utf-8")

    conn = sqlite3.connect(str(db_path))
    result = promote_model(conn, model_version="m-qual", run_dir=run_dir)
    conn.close()

    assert result["status"] == "REFUSED"
    assert "candidate_identity_mismatch" in result["reasons"]


def test_promotion_verifies_vectors_artifact_binding(qualified_run):
    """Verify vectors.jsonl is required, verified, and bound to candidate set."""
    db_path, run_dir = qualified_run
    vectors_file = run_dir / "vectors.jsonl"
    assert vectors_file.is_file()
    vector_lines = [json.loads(line) for line in vectors_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(vector_lines) == 32
    assert "preference_embedding_text" in vector_lines[0]
    assert "candidate_vector" in vector_lines[0]
    assert "embedding_model" in vector_lines[0]

    # Tamper with vectors.jsonl
    vectors_file.write_text(vectors_file.read_text(encoding="utf-8") + "\n{}\n", encoding="utf-8")
    conn = sqlite3.connect(str(db_path))
    bundle, reasons = load_qualification_bundle(run_dir)
    assert bundle is None
    assert "bundle_hash_mismatch" in reasons
    conn.close()


def test_shared_evidence_validator_isolates_model_statuses(qualified_run):
    """The extracted validator enforces the caller's allowed statuses:
    promotion only accepts DRAFT, deployment only accepts PUBLISHED."""
    from types import SimpleNamespace

    from app.services.curve_preference import (
        load_qualification_bundle,
        validate_qualification_evidence,
    )

    db_path, run_dir = qualified_run
    conn = sqlite3.connect(str(db_path))
    bundle, reasons = load_qualification_bundle(run_dir)
    assert bundle is not None and reasons == []

    # PUBLISHED-allowed on a still-DRAFT model: refused as not published.
    draft_refusal = validate_qualification_evidence(
        conn,
        model_version="m-qual",
        bundle=bundle,
        run_dir=run_dir,
        allowed_model_statuses=frozenset({"PUBLISHED"}),
        model_status_error="model_not_published",
    )
    assert "model_not_published" in draft_refusal

    # DRAFT-allowed on a PUBLISHED model: refused as not draft (deployment
    # cannot be satisfied by a promotion-grade status and vice versa).
    conn.execute(
        "UPDATE curve_preference_models SET status = 'PUBLISHED' "
        "WHERE model_version = 'm-qual'"
    )
    conn.commit()
    published_refusal = validate_qualification_evidence(
        conn,
        model_version="m-qual",
        bundle=bundle,
        run_dir=run_dir,
        allowed_model_statuses=frozenset({"DRAFT"}),
        model_status_error="model_not_draft",
    )
    assert "model_not_draft" in published_refusal
    conn.close()
