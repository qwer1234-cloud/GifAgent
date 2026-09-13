"""Tests for holdout source video isolation and build exclusion."""

import json
import sqlite3
from pathlib import Path
import numpy as np
import pytest

from app.services.preference_schema import apply_preference_schema
from app.services.preference_events import PreferenceEventService
from app.services.preference_memory import (
    PreferenceMemoryService,
    evaluate_profile_gates,
    preview_profile,
)
from app.services.preference_evaluation import PreferenceEvaluationService
from app.services.preference_types import ProfileBuildConfig


@pytest.fixture
def multi_video_db(tmp_path):
    """Create a database with 6 distinct videos and feedback events."""
    db_path = tmp_path / "test_split.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    apply_preference_schema(conn)

    def _no_embed(_text: str) -> list[float]:
        raise RuntimeError("multi_video_db supplies vectors explicitly")

    svc = PreferenceEventService(conn, embed_fn=_no_embed)
    rng = np.random.default_rng(42)

    # 6 videos: vid_0 to vid_5
    # Each video has 10 likes, 4 dislikes (total 14 * 6 = 84 events)
    # This allows 3 videos in train (42 events: 30 like, 12 dislike) and 3 in holdout (42 events)
    videos = [f"video_sha_{i}" for i in range(6)]
    for v_idx, vid in enumerate(videos):
        # 10 likes (including 2 favorites)
        for i in range(10):
            cand_id = f"cand_{v_idx}_like_{i}"
            rating = "favorite" if i < 2 else "like"
            conn.execute(
                """INSERT INTO candidate_gifs
                   (candidate_id, source_run_id, source_run_candidate_id,
                    source_video_sha256, source_video_path, start_sec, end_sec,
                    status, tags_json, scenario_keys_json, final_score)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    cand_id,
                    "run-1",
                    f"rc_{cand_id}",
                    vid,
                    f"/v/{vid}.mp4",
                    0.0,
                    5.0,
                    "liked",
                    json.dumps(["cat", "cute"]),
                    json.dumps(["scene:cat"]),
                    0.8,
                ),
            )
            svc.record_feedback(
                target_type="candidate_gif",
                target_id=cand_id,
                rating=rating,
                source_video_sha256=vid,
                scenario_keys=["scene:cat"],
            )
            vec = rng.random(768).astype(np.float32)
            conn.execute(
                """INSERT OR REPLACE INTO candidate_vectors
                   (candidate_id, vector_type, embedding_model, embedding_dim, vector_blob)
                   VALUES (?,?,?,?,?)""",
                (cand_id, "clip", "nomic-embed-text:latest", 768, vec.tobytes()),
            )

        # 4 dislikes
        for i in range(4):
            cand_id = f"cand_{v_idx}_dislike_{i}"
            conn.execute(
                """INSERT INTO candidate_gifs
                   (candidate_id, source_run_id, source_run_candidate_id,
                    source_video_sha256, source_video_path, start_sec, end_sec,
                    status, tags_json, scenario_keys_json, final_score)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    cand_id,
                    "run-1",
                    f"rc_{cand_id}",
                    vid,
                    f"/v/{vid}.mp4",
                    0.0,
                    5.0,
                    "disliked",
                    json.dumps(["dark"]),
                    json.dumps(["scene:dark"]),
                    0.2,
                ),
            )
            svc.record_feedback(
                target_type="candidate_gif",
                target_id=cand_id,
                rating="dislike",
                source_video_sha256=vid,
                scenario_keys=["scene:dark"],
            )
            vec = rng.random(768).astype(np.float32)
            conn.execute(
                """INSERT OR REPLACE INTO candidate_vectors
                   (candidate_id, vector_type, embedding_model, embedding_dim, vector_blob)
                   VALUES (?,?,?,?,?)""",
                (cand_id, "clip", "nomic-embed-text:latest", 768, vec.tobytes()),
            )

    conn.commit()
    return conn, str(db_path)


def test_evaluate_profile_gates_exclude(multi_video_db):
    conn, _ = multi_video_db
    excluded = {"video_sha_3", "video_sha_4", "video_sha_5"}

    # Without exclude: 84 events across 6 videos
    all_snap = evaluate_profile_gates(conn)
    assert all_snap.effective_count == 84
    assert all_snap.source_video_count == 6
    assert len(all_snap.gate_reasons) == 0

    # With exclude: 42 events across 3 videos
    snap = evaluate_profile_gates(conn, exclude_source_videos=excluded)
    assert snap.effective_count == 42
    assert snap.like_count == 30
    assert snap.dislike_count == 12
    assert snap.source_video_count == 3
    assert snap.max_single_video_share <= 0.40
    assert snap.candidate_vector_count == 42
    assert len(snap.gate_reasons) == 0


def test_build_profile_with_exclude_and_holdout_evaluate(multi_video_db, tmp_path):
    conn, db_path = multi_video_db
    holdout_videos = {"video_sha_3", "video_sha_4", "video_sha_5"}

    # Count events before
    event_count_before = conn.execute("SELECT count(*) FROM preference_events").fetchone()[0]

    # 1. Build profile with exclude_source_videos
    svc = PreferenceMemoryService(conn)
    build_res = svc.build_profile(dry_run=False, exclude_source_videos=holdout_videos)
    assert build_res["status"] == "built"
    profile_ver = build_res["profile_version"]

    # Verify event count unmodified
    event_count_after = conn.execute("SELECT count(*) FROM preference_events").fetchone()[0]
    assert event_count_before == event_count_after

    # 2. Create holdout JSONL using candidates from holdout_videos
    holdout_file = tmp_path / "holdout.jsonl"
    holdout_lines = []
    for v_idx in [3, 4, 5]:
        for i in range(10):
            holdout_lines.append(
                json.dumps(
                    {
                        "candidate_id": f"cand_{v_idx}_like_{i}",
                        "rating": "like",
                        "judged_at": "2026-09-02T12:00:00Z",
                    }
                )
            )
        for i in range(4):
            holdout_lines.append(
                json.dumps(
                    {
                        "candidate_id": f"cand_{v_idx}_dislike_{i}",
                        "rating": "dislike",
                        "judged_at": "2026-09-02T12:00:00Z",
                    }
                )
            )
    holdout_file.write_text("\n".join(holdout_lines) + "\n", encoding="utf-8")

    # 3. Evaluate excluded build -> should pass overlap gate and publish gate
    eval_svc = PreferenceEvaluationService(conn)
    eval_res = eval_svc.evaluate(profile_ver, holdout_path=holdout_file)
    assert eval_res["can_publish"] is True
    assert not any("source_video_overlap" in r for r in eval_res["gate_reasons"])

    # 4. If we build without excluding holdout videos, evaluating should FAIL overlap gate
    unexcluded_res = svc.build_profile(dry_run=False, exclude_source_videos=None)
    assert unexcluded_res["status"] == "built"
    unexcluded_ver = unexcluded_res["profile_version"]

    eval_unexcluded = eval_svc.evaluate(unexcluded_ver, holdout_path=holdout_file)
    assert eval_unexcluded["can_publish"] is False
    assert any("source_video_overlap" in r for r in eval_unexcluded["gate_reasons"])

    # 5. Test evaluate_source_grouped on excluded build
    grouped_res = eval_svc.evaluate_source_grouped(profile_ver, holdout_path=holdout_file)
    assert grouped_res["publish_gate"]["can_publish"] is True
    assert grouped_res["source_video_integrity"]["integrity_ok"] is True
    assert grouped_res["source_video_integrity"]["overlap_violations"] == 0


def test_split_source_videos_script_logic(multi_video_db, tmp_path):
    from scripts.preference_holdout_split import split_source_videos

    conn, db_path = multi_video_db
    res = split_source_videos(conn, seed=42)

    assert len(res["holdout_videos"]) >= 3
    assert len(res["train_videos"]) >= 3
    assert set(res["holdout_videos"]).isdisjoint(set(res["train_videos"]))

    metrics = res["metrics"]
    assert metrics["holdout"]["judgment_count"] >= 30
    assert metrics["holdout"]["max_single_video_share"] <= 0.40
    assert metrics["train"]["effective_feedback_count"] >= 30
    assert metrics["train"]["like_count"] >= 15
    assert metrics["train"]["dislike_count"] >= 10
    assert metrics["train"]["max_single_video_share"] <= 0.40


def test_preview_profile_with_exclude(multi_video_db):
    conn, _ = multi_video_db
    config = ProfileBuildConfig()
    holdout_videos = ["video_sha_3", "video_sha_4", "video_sha_5"]

    preview = preview_profile(conn, config, exclude_source_videos=holdout_videos)
    assert preview.status == "ready"
    assert preview.metrics["effective_feedback_count"] == 42.0
    assert preview.metrics["source_video_count"] == 3.0
    assert len(preview.gate_reasons) == 0
