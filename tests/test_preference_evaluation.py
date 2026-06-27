"""P1-7: Holdout evaluation tests."""

import json
import sqlite3
from pathlib import Path

import pytest


@pytest.fixture
def eval_db():
    """In-memory SQLite DB with a completed profile build."""
    from app.services.preference_schema import apply_preference_schema

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_preference_schema(conn)

    conn.execute(
        """INSERT INTO preference_profile_builds
           (profile_version, event_watermark, embedding_model, embedding_dim,
            effective_feedback_count, source_video_count, config_json,
            status, gate_reasons_json)
           VALUES ('profile-test-v1', '2026-01-01', 'nomic-embed-text:latest', 768,
                   30, 3, '{}', 'completed', '[]')"""
    )
    conn.commit()
    return conn


@pytest.fixture
def holdout_file(tmp_path):
    """40 holdout judgments (25 like, 15 dislike)."""
    p = tmp_path / "holdout.jsonl"
    lines = []
    for i in range(40):
        lines.append(
            json.dumps(
                {
                    "candidate_id": f"cand-holdout-{i}",
                    "rating": "like" if i < 25 else "dislike",
                    "judged_at": "2026-01-02T00:00:00Z",
                }
            )
        )
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Gate tests
# ---------------------------------------------------------------------------


def test_evaluation_blocks_insufficient_holdout(eval_db):
    """Fewer than 30 holdout judgments blocks publish."""
    from app.services.preference_evaluation import PreferenceEvaluationService

    svc = PreferenceEvaluationService(eval_db)
    report = svc.evaluate("profile-test-v1", holdout_count=12)

    assert report["can_publish"] is False
    assert any("holdout_judgment" in r for r in report["gate_reasons"])


def test_evaluation_with_sufficient_holdout(eval_db, holdout_file):
    """40 holdout judgments produces a full evaluation report."""
    from app.services.preference_evaluation import PreferenceEvaluationService

    svc = PreferenceEvaluationService(eval_db)
    report = svc.evaluate("profile-test-v1", holdout_path=holdout_file)

    assert "can_publish" in report
    assert "like_at_20" in report
    assert "dislike_at_20" in report
    assert "ndcg_at_20" in report
    assert "gate_reasons" in report
    assert isinstance(report["gate_reasons"], list)
    assert isinstance(report["can_publish"], bool)
    assert isinstance(report["like_at_20"], float)
    assert isinstance(report["dislike_at_20"], float)
    assert isinstance(report["ndcg_at_20"], float)


def test_evaluation_blocks_source_video_overlap(eval_db, holdout_file):
    """Source-video overlap between training and holdout blocks publish."""
    from app.services.preference_evaluation import PreferenceEvaluationService

    # Insert training events (within the profile's event_watermark)
    eval_db.execute(
        """INSERT INTO preference_events
           (event_id, target_type, target_id, rating, source_video_sha256,
            scenario_keys_json, created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (
            "evt-train-1",
            "candidate_gif",
            "cand-train-1",
            "like",
            "sha256-overlap-aaa",
            "[]",
            "2025-12-15T00:00:00Z",
        ),
    )
    eval_db.execute(
        """INSERT INTO preference_events
           (event_id, target_type, target_id, rating, source_video_sha256,
            scenario_keys_json, created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (
            "evt-train-2",
            "candidate_gif",
            "cand-train-2",
            "dislike",
            "sha256-overlap-bbb",
            "[]",
            "2025-12-20T00:00:00Z",
        ),
    )

    # Insert a holdout candidate that shares a training source video
    eval_db.execute(
        """INSERT OR REPLACE INTO candidate_gifs
           (candidate_id, source_run_id, source_run_candidate_id,
            source_video_sha256, source_video_path, start_sec, end_sec)
           VALUES (?,?,?,?,?,?,?)""",
        (
            "cand-holdout-0",
            "run-test",
            "src-cand-0",
            "sha256-overlap-aaa",
            "/videos/test.mp4",
            0.0,
            5.0,
        ),
    )

    eval_db.commit()

    svc = PreferenceEvaluationService(eval_db)
    report = svc.evaluate("profile-test-v1", holdout_path=holdout_file)

    assert isinstance(report["gate_reasons"], list)
    assert any("overlap" in r.lower() for r in report["gate_reasons"])
    assert report["can_publish"] is False


def test_metrics_with_no_overlap_and_sufficient_holdout(eval_db, holdout_file):
    """When gates pass, metrics are computed properly."""
    from app.services.preference_evaluation import PreferenceEvaluationService

    # Insert training events with distinct (non-overlapping) source videos
    eval_db.execute(
        """INSERT INTO preference_events
           (event_id, target_type, target_id, rating, source_video_sha256,
            scenario_keys_json, created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (
            "evt-train-1",
            "candidate_gif",
            "cand-train-1",
            "like",
            "sha256-train-only",
            "[]",
            "2025-12-15T00:00:00Z",
        ),
    )

    # Insert holdout candidates with different source videos
    for i in range(40):
        eval_db.execute(
            """INSERT OR REPLACE INTO candidate_gifs
               (candidate_id, source_run_id, source_run_candidate_id,
                source_video_sha256, source_video_path, start_sec, end_sec,
                final_score)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                f"cand-holdout-{i}",
                "run-test",
                f"src-cand-{i}",
                f"sha256-holdout-{i % 5}",
                f"/videos/test-{i % 5}.mp4",
                0.0,
                5.0,
                # Give liked candidates higher scores so they rank in top 20
                1.0 - i * 0.01,
            ),
        )

    eval_db.commit()

    svc = PreferenceEvaluationService(eval_db)
    report = svc.evaluate("profile-test-v1", holdout_path=holdout_file)

    assert report["can_publish"] is True
    assert report["gate_reasons"] == []
    assert 0.0 <= report["like_at_20"] <= 1.0
    assert 0.0 <= report["dislike_at_20"] <= 1.0
    assert 0.0 <= report["ndcg_at_20"] <= 1.0


def test_evaluate_raises_for_unknown_build(eval_db):
    """Non-existent profile_version raises ValueError."""
    from app.services.preference_evaluation import PreferenceEvaluationService

    svc = PreferenceEvaluationService(eval_db)
    with pytest.raises(ValueError, match="Build not found"):
        svc.evaluate("nonexistent-profile", holdout_count=50)
