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
