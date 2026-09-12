"""End-to-end learning pipeline tests: eligibility -> SNAPSHOT -> TRAIN ->
QUALIFY -> COMPLETE with the real stage services (Task 6)."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from app.services.learning_jobs import (
    collect_learning_stats,
    default_services,
    maybe_enqueue_learning,
    run_learning_once,
)
from app.services.preference_schema import apply_preference_schema

TINY_POLICY = {
    "min_new_effective_candidates": 1,
    "min_new_source_groups": 1,
    "min_positive_candidates": 1,
    "min_negative_candidates": 1,
    "min_interval_hours": 0,
}


def _seed_learning_db(db_path: Path) -> None:
    """Synthetic library DB: events, publications, capture payloads,
    source features, and vectors across three sources (Task 5 shapes)."""
    from app.services.candidate_capture import persist_capture
    from app.services.curve_source_features import CURVE_SOURCE_FEATURE_NAMES

    conn = sqlite3.connect(db_path)
    apply_preference_schema(conn)

    sources = ["a" * 64, "b" * 64, "c" * 64]
    for i in range(36):
        cid = f"cand-{i:02d}"
        source = sources[i % 3]
        rating = "like" if i % 2 == 0 else "dislike"
        conn.execute(
            "INSERT INTO candidate_gifs (candidate_id, source_run_id, "
            "source_run_candidate_id, source_video_sha256, source_video_path, "
            "start_sec, end_sec, status) VALUES (?, 'run', ?, ?, '/v.mp4', 0.0, 5.0, 'candidate')",
            (cid, f"rc-{cid}", source),
        )
        conn.execute(
            "INSERT INTO preference_events (event_id, target_type, target_id, "
            "rating, source_video_sha256, scenario_keys_json, created_at, event_kind) "
            "VALUES (?, 'candidate_gif', ?, ?, ?, '[]', ?, 'feedback')",
            (f"ev-{i:02d}", cid, rating, source, f"2026-09-01T{i // 24:02d}:{i % 24:02d}:00Z"),
        )
        payload = {
            "run_id": "run-e2e",
            "preexport_key": f"{i:064x}",
            "capture_version": "capture-v1",
            "source_video_sha256": source,
            "start_us": 1_000_000 + i,
            "end_us": 4_000_000 + i,
            "anchor_us": 2_500_000,
            "baseline_score": 0.5 + i / 100.0,
            "capture_status": "CAPTURED",
        }
        persisted = persist_capture(conn, payload, db_path.parent / "evidence")
        conn.execute(
            "INSERT INTO candidate_analysis_publications "
            "(publication_id, record_id, candidate_id, gif_sha256) VALUES (?, ?, ?, ?)",
            (f"pub-{i:02d}", persisted.record_id, cid, f"{i:064x}"),
        )
        features = {name: 0.3 + (i % 6) / 12.0 for name in CURVE_SOURCE_FEATURE_NAMES}
        conn.execute(
            "INSERT INTO curve_features (candidate_id, feature_version, "
            "availability, features_json, extractor_sha256, artifact_sha256, "
            "wall_clock_ms) VALUES (?, 'curve-source-v1', 'AVAILABLE', ?, 'ex', ?, 1.0)",
            (cid, json.dumps(features), f"{i:064x}"),
        )
        conn.execute(
            "INSERT INTO candidate_vectors (candidate_id, vector_type, "
            "embedding_model, embedding_dim, vector_blob, normalized, source_text_hash) "
            "VALUES (?, 'clip', 'nomic-embed-text:latest', 768, ?, 1, ?)",
            (cid, bytes([i]) * 8, f"texthash-{i}"),
        )
    conn.execute(
        "INSERT INTO preference_profile_current (slot, profile_version) "
        "VALUES ('current', 'profile-v1')"
    )
    conn.commit()
    conn.close()


def _services(db_path: Path, tmp_path: Path) -> dict:
    services = default_services(None)
    services["db_path"] = str(db_path)
    services["work_dir"] = str(tmp_path / "learning_runs")
    return services


def test_worker_advances_one_stage_per_run_until_complete(tmp_path):
    """Four runs observe SNAPSHOT -> TRAIN -> QUALIFY -> COMPLETE and exactly
    one DRAFT model; the synthetic holdout is too small, so the scientific
    outcome is NOT_QUALIFIED with the job still SUCCEEDED."""
    db_path = tmp_path / "library.db"
    _seed_learning_db(db_path)
    conn = sqlite3.connect(db_path)
    services = _services(db_path, tmp_path)

    stats = collect_learning_stats(conn, TINY_POLICY)
    assert stats["new_effective_candidates"] == 36
    assert stats["positive_candidates"] == 18
    assert stats["negative_candidates"] == 18

    enqueue = maybe_enqueue_learning(conn, TINY_POLICY)
    assert enqueue.status == "ENQUEUED"
    assert enqueue.job_id

    epoch = 1_800_000_000
    # Each stage run happens after the previous lease expires, mirroring a
    # worker that re-claims long-running jobs.
    run1 = run_learning_once(conn, now_epoch=epoch, owner="w1", services=services)
    assert run1["status"] == "RUNNING"
    assert run1["stage"] == "TRAIN"

    run2 = run_learning_once(
        conn, now_epoch=epoch + 1801, owner="w1", services=services
    )
    assert run2["status"] == "RUNNING"
    assert run2["stage"] == "QUALIFY"

    run3 = run_learning_once(
        conn, now_epoch=epoch + 3602, owner="w1", services=services
    )
    assert run3["status"] == "RUNNING"
    assert run3["stage"] == "COMPLETE"

    run4 = run_learning_once(
        conn, now_epoch=epoch + 5403, owner="w1", services=services
    )
    assert run4["status"] == "SUCCEEDED"

    drafts = conn.execute(
        "SELECT COUNT(*) FROM curve_preference_models WHERE status = 'DRAFT'"
    ).fetchone()[0]
    published = conn.execute(
        "SELECT COUNT(*) FROM curve_preference_models WHERE status = 'PUBLISHED'"
    ).fetchone()[0]
    assert drafts == 1
    assert published == 0

    job = conn.execute(
        "SELECT status, stage FROM learning_jobs WHERE job_id = ?",
        (enqueue.job_id,),
    ).fetchone()
    assert job[0] == "SUCCEEDED"
    assert job[1] == "COMPLETE"

    # The qualification outcome is recorded on the job package.
    package = json.loads(
        Path(
            conn.execute(
                "SELECT result_package_path FROM learning_jobs WHERE job_id = ?",
                (enqueue.job_id,),
            ).fetchone()[0]
        ).read_text(encoding="utf-8")
    )
    assert package["qualification"]["status"] == "NOT_QUALIFIED"
    assert "holdout_too_small" in package["qualification"]["reasons"]
    conn.close()


def test_ineligible_stats_never_enqueue():
    conn = sqlite3.connect(":memory:")
    apply_preference_schema(conn)
    result = maybe_enqueue_learning(conn, None)
    assert result.status == "INELIGIBLE"
    assert "insufficient_new_feedback" in result.reasons
    count = conn.execute("SELECT COUNT(*) FROM learning_jobs").fetchone()[0]
    assert count == 0


def test_worker_never_succeeds_without_a_frozen_snapshot(tmp_path):
    conn = sqlite3.connect(tmp_path / "learning.db")
    apply_preference_schema(conn)
    from app.services.learning_jobs import enqueue_learning

    enqueue_learning(conn, "f" * 64, "e" * 64)
    result = run_learning_once(
        conn, now_epoch=1_800_000_000, owner="w1",
        services=_services(tmp_path / "library.db", tmp_path),
    )
    assert result["status"] == "NEEDS_ATTENTION"
    assert result["error_code"] == "snapshot_not_found"
    models = conn.execute("SELECT COUNT(*) FROM curve_preference_models").fetchone()[0]
    assert models == 0


def test_cancelled_job_never_advances(tmp_path):
    db_path = tmp_path / "library.db"
    _seed_learning_db(db_path)
    conn = sqlite3.connect(db_path)
    services = _services(db_path, tmp_path)
    enqueue = maybe_enqueue_learning(conn, TINY_POLICY)
    assert enqueue.job_id

    conn.execute(
        "UPDATE learning_jobs SET status = 'CANCELLED' WHERE job_id = ?",
        (enqueue.job_id,),
    )
    conn.commit()
    result = run_learning_once(
        conn, now_epoch=1_800_000_000, owner="w1", services=services
    )
    assert result["status"] == "IDLE"
    stage = conn.execute(
        "SELECT stage FROM learning_jobs WHERE job_id = ?", (enqueue.job_id,)
    ).fetchone()[0]
    assert stage == "SNAPSHOT"  # untouched
    conn.close()


def test_recovered_job_rejects_drifted_snapshot_artifact(tmp_path):
    db_path = tmp_path / "library.db"
    _seed_learning_db(db_path)
    conn = sqlite3.connect(db_path)
    services = _services(db_path, tmp_path)
    enqueue = maybe_enqueue_learning(conn, TINY_POLICY)

    # Advance through SNAPSHOT, then tamper with the frozen manifest.
    run_learning_once(conn, now_epoch=1_800_000_000, owner="w1", services=services)
    package_path = conn.execute(
        "SELECT result_package_path FROM learning_jobs WHERE job_id = ?",
        (enqueue.job_id,),
    ).fetchone()[0]
    package = json.loads(Path(package_path).read_text(encoding="utf-8"))
    manifest_path = Path(package["snapshot"]["dir"]) / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["train_rows"][0]["baseline_score"] = 0.99
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )

    result = run_learning_once(
        conn, now_epoch=1_800_001_801, owner="w1", services=services
    )
    assert result["status"] == "NEEDS_ATTENTION"
    assert result["error_code"] == "snapshot_artifact_hash_mismatch"
    conn.close()
