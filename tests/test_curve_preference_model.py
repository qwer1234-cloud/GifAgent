"""Tests for the curve preference model training and gating script."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import numpy as np
import pytest

from scripts.train_curve_preference_model import (
    MIN_HOLDOUT_JUDGMENTS,
    decide_publication,
    load_rows,
    paired_bootstrap_delta_lcb95,
    source_split,
    within_source_pair_stats,
    pair_accuracy,
)
from scripts.continuity_spike_backend_real import source_media_dims

FEATURES = {
    "extension_mean": 0.5,
    "motion_smoothness": 0.7,
    "subject_area_ratio": 0.3,
    "torso_speed_p95": 0.2,
}


def _init_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE preference_events (
            event_id TEXT PRIMARY KEY,
            target_type TEXT NOT NULL,
            target_id TEXT NOT NULL,
            rating TEXT NOT NULL,
            source_video_sha256 TEXT,
            scenario_keys_json TEXT,
            corrected_tags_json TEXT,
            note TEXT,
            created_at TEXT NOT NULL,
            previous_status TEXT,
            undone_at TEXT,
            undone_reason TEXT,
            event_kind TEXT NOT NULL,
            supersedes_event_id TEXT
        );
        CREATE TABLE candidate_gifs (
            candidate_id TEXT PRIMARY KEY,
            artifact_path TEXT,
            source_video_sha256 TEXT,
            base_rag_similarity REAL,
            profile_score REAL,
            final_score REAL
        );
        CREATE TABLE curve_features (
            candidate_id TEXT NOT NULL,
            feature_version TEXT NOT NULL,
            availability TEXT NOT NULL,
            features_json TEXT,
            missing_reason TEXT,
            extractor_sha256 TEXT NOT NULL,
            artifact_sha256 TEXT NOT NULL,
            wall_clock_ms REAL,
            source_video_sha256 TEXT,
            width INTEGER,
            height INTEGER,
            frame_count INTEGER,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            PRIMARY KEY (candidate_id, feature_version)
        );
        """
    )
    return conn


def _insert_event(
    conn: sqlite3.Connection,
    event_id: str,
    target_id: str,
    rating: str,
    created_at: str,
    *,
    event_kind: str = "feedback",
    supersedes: str | None = None,
    undone_at: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO preference_events (
            event_id, target_type, target_id, rating, source_video_sha256,
            scenario_keys_json, created_at, event_kind, supersedes_event_id,
            undone_at
        ) VALUES (
            :event_id, 'candidate_gif', :target_id, :rating, 'src-1',
            '[]', :created_at, :event_kind, :supersedes, :undone_at
        )
        """,
        {
            "event_id": event_id,
            "target_id": target_id,
            "rating": rating,
            "created_at": created_at,
            "event_kind": event_kind,
            "supersedes": supersedes,
            "undone_at": undone_at,
        },
    )


def _insert_candidate(
    conn: sqlite3.Connection,
    candidate_id: str,
    *,
    source: str = "src-1",
    baseline: float | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO candidate_gifs (
            candidate_id, artifact_path, source_video_sha256, base_rag_similarity
        ) VALUES (:candidate_id, :artifact_path, :source, :baseline)
        """,
        {
            "candidate_id": candidate_id,
            "artifact_path": f"X:/media/{candidate_id}.gif",
            "source": source,
            "baseline": baseline,
        },
    )
    conn.execute(
        """
        INSERT INTO curve_features (
            candidate_id, feature_version, availability, features_json,
            missing_reason, extractor_sha256, artifact_sha256, wall_clock_ms,
            source_video_sha256, width, height, frame_count
        ) VALUES (
            :candidate_id, 'curve-v1', 'AVAILABLE', :features_json,
            NULL, 'extractor-1', 'artifact-sha-1', 900.0,
            :source, 720, 405, 24
        )
        """,
        {
            "candidate_id": candidate_id,
            "features_json": json.dumps(FEATURES),
            "source": source,
        },
    )


def test_correction_supersedes_earlier_rating(tmp_path):
    db_path = tmp_path / "library.db"
    conn = _init_db(db_path)
    _insert_candidate(conn, "cand-1")
    _insert_event(conn, "e1", "cand-1", "like", "2026-09-01T00:00:00")
    _insert_event(
        conn, "e2", "cand-1", "dislike", "2026-09-02T00:00:00", supersedes="e1",
        event_kind="correction",
    )
    conn.commit()
    conn.close()

    rows, _watermark, skipped = load_rows(db_path)

    assert skipped["no_effective_scoring_event"] == 0
    assert len(rows) == 1
    assert rows[0]["rating"] == "dislike"


def test_undone_event_no_longer_labels_the_candidate(tmp_path):
    db_path = tmp_path / "library.db"
    conn = _init_db(db_path)
    _insert_candidate(conn, "cand-1")
    _insert_event(
        conn, "e1", "cand-1", "like", "2026-09-01T00:00:00",
        undone_at="2026-09-03T00:00:00",
    )
    conn.commit()
    conn.close()

    rows, _watermark, skipped = load_rows(db_path)

    assert rows == []
    assert skipped["no_effective_scoring_event"] == 1


def test_later_like_replaces_favorite_as_latest_effective(tmp_path):
    db_path = tmp_path / "library.db"
    conn = _init_db(db_path)
    _insert_candidate(conn, "cand-1")
    _insert_event(conn, "e1", "cand-1", "favorite", "2026-09-01T00:00:00")
    _insert_event(conn, "e2", "cand-1", "like", "2026-09-02T00:00:00")
    conn.commit()
    conn.close()

    rows, _watermark, _skipped = load_rows(db_path)

    # Both are positive for the binary label, but the effective rating is
    # the latest non-superseded event, not a permanent favorite.
    assert rows[0]["rating"] == "like"


def test_latest_neutral_excludes_candidate_from_training(tmp_path):
    db_path = tmp_path / "library.db"
    conn = _init_db(db_path)
    _insert_candidate(conn, "cand-1")
    _insert_event(conn, "e1", "cand-1", "like", "2026-09-01T00:00:00")
    _insert_event(conn, "e2", "cand-1", "neutral", "2026-09-02T00:00:00")
    conn.commit()
    conn.close()

    rows, _watermark, skipped = load_rows(db_path)

    assert rows == []
    assert skipped["no_effective_scoring_event"] == 1


def test_source_split_is_deterministic_and_disjoint():
    sources = [hashlib_sha(i) for i in range(100)]

    first_train, first_holdout = source_split(sources)
    second_train, second_holdout = source_split(sources)

    assert first_train == second_train
    assert first_holdout == second_holdout
    assert not (first_train & first_holdout)
    assert first_train | first_holdout == set(sources)


def hashlib_sha(index: int) -> str:
    import hashlib

    return hashlib.sha256(str(index).encode("utf-8")).hexdigest()


def _rows_from_labels(labels: list[str], sources: list[str]) -> list[dict]:
    rows = []
    for index, rating in enumerate(labels):
        rows.append(
            {
                "candidate_id": f"cand-{index}",
                "features": FEATURES,
                "source": sources[index],
                "rating": rating,
                "baseline": None,
                "extractor": "e",
            }
        )
    return rows


def test_decide_publication_requires_baseline_universe():
    status, reasons = decide_publication(
        holdout_row_count=100,
        source_overlap=0,
        train_class_counts=(100, 10),
        holdout_class_counts=(30, 10),
        baseline_universe_rows=0,
        baseline_pair_count=0,
        paired_delta_lcb95=float("nan"),
    )

    assert status == "DRAFT"
    assert any(r.startswith("baseline_coverage_insufficient") for r in reasons)


def test_decide_publication_publishes_on_positive_paired_delta():
    status, reasons = decide_publication(
        holdout_row_count=MIN_HOLDOUT_JUDGMENTS + 10,
        source_overlap=0,
        train_class_counts=(100, 10),
        holdout_class_counts=(30, 10),
        baseline_universe_rows=40,
        baseline_pair_count=25,
        paired_delta_lcb95=0.08,
    )

    assert status == "PUBLISHED"
    assert reasons == []


def test_decide_publication_rejects_when_model_loses_to_baseline():
    status, reasons = decide_publication(
        holdout_row_count=MIN_HOLDOUT_JUDGMENTS + 10,
        source_overlap=0,
        train_class_counts=(100, 10),
        holdout_class_counts=(30, 10),
        baseline_universe_rows=40,
        baseline_pair_count=25,
        paired_delta_lcb95=-0.02,
    )

    assert status == "REJECTED"
    assert any("paired_delta_lcb95" in r for r in reasons)


def test_paired_bootstrap_separates_good_and_bad_models():
    sources = ["s1", "s1", "s2", "s2", "s3", "s3", "s4", "s4"]
    labels = ["like", "dislike", "like", "dislike", "like", "dislike", "like", "dislike"]
    rows = _rows_from_labels(labels, sources)
    pairs = within_source_pair_stats(rows)
    flat_pairs = [p for v in pairs.values() for p in v]
    assert len(flat_pairs) == 4

    good = np.array([1.0, 0.0, 1.0, 0.0, 1.0, 0.0, 1.0, 0.0])
    bad = np.array([0.0, 1.0, 0.0, 1.0, 0.0, 1.0, 0.0, 1.0])

    assert pair_accuracy(flat_pairs, good) == 1.0
    assert pair_accuracy(flat_pairs, bad) == 0.0
    assert paired_bootstrap_delta_lcb95(pairs, good, bad) > 0.5
    assert paired_bootstrap_delta_lcb95(pairs, bad, good) < -0.5


def test_gif_source_dims_differ_from_analysis_width(tmp_path):
    from PIL import Image

    gif_path = tmp_path / "clip.gif"
    Image.new("RGB", (720, 405), (10, 20, 30)).save(gif_path, save_all=True)

    dims = source_media_dims(gif_path)

    assert dims == (720, 405)
    assert dims[0] != 960  # analysis width must never leak into source dims
