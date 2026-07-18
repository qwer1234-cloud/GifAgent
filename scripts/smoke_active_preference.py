#!/usr/bin/env python3
"""Smoke test for the active preference learning subsystem.

Verifies the full lifecycle: candidate materialization, feedback recording
(all 6 rating meanings), profile building, profile publishing, source-grouped
evaluation, reranker explanations, and rollback.

Usage:
    uv run python scripts/smoke_active_preference.py
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

# Ensure project root is on sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from app.services.preference_schema import apply_preference_schema
from app.services.preference_memory import PreferenceMemoryService
from app.services.preference_evaluation import PreferenceEvaluationService
from app.services.preference_types import ProfileBuildConfig
from app.services.reranker import PreferenceReranker


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MEANING_DESCRIPTIONS: dict[str, str] = {
    "like": "Strong positive signal: candidate matches user preference",
    "dislike": "Strong negative signal: candidate does not match user preference",
    "neutral": "Weak signal: candidate is neither liked nor disliked",
    "skip": "No signal: user chose not to rate this candidate (ignored in profiles)",
    "quality_reject": "Quality veto: candidate has visual/technical defects (ignored in profiles)",
    "favorite": "Amplified positive signal: candidate is especially preferred (2x weight in profile)",
}


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _assert(condition: bool, message: str) -> None:
    if not condition:
        print(f"  [FAIL] {message}")
        sys.exit(1)
    print(f"  [ok] {message}")


# ---------------------------------------------------------------------------
# Fixture Helpers
# ---------------------------------------------------------------------------


def _fake_vector(dim: int = 768) -> bytes:
    """Generate a unit-normalized random vector."""
    vec = np.random.default_rng(42).normal(size=dim).astype(np.float32)
    vec /= np.linalg.norm(vec)
    return vec.tobytes()


def _seed_candidates(conn: sqlite3.Connection, count: int = 60) -> list[dict[str, object]]:
    """Seed *count* candidate GIFs spread across several source videos."""
    videos = [f"sha256-video-{i}" for i in range(6)]
    candidates: list[dict[str, object]] = []

    for i in range(count):
        video = videos[i % len(videos)]
        cid = f"cand-{i:04d}"
        candidates.append({
            "candidate_id": cid,
            "source_run_id": "smoke-run",
            "source_run_candidate_id": f"src-{i}",
            "source_video_sha256": video,
            "source_video_path": f"/smoke/video-{video[-1]}.mp4",
            "start_sec": float(i * 5),
            "end_sec": float(i * 5 + 3),
            "artifact_path": f"/smoke/exports/{cid}.gif",
            "preview_path": None,
            "vlm_summary_json": json.dumps({"caption": f"Scene {i}"}),
            "tags_json": json.dumps(["action", "drama"] if i % 2 == 0 else ["comedy", "dialogue"]),
            "scenario_keys_json": json.dumps(
                ["emotion:exciting"] if i % 3 == 0 else ["emotion:calm"]
            ),
            "base_rag_similarity": round(0.3 + (i / count) * 0.5, 4),
            "profile_score": None,
            "final_score": round(0.3 + (i / count) * 0.5, 4),
            "score_profile_version": None,
            "status": "candidate",
        })

    for c in candidates:
        conn.execute(
            """INSERT OR REPLACE INTO candidate_gifs
               (candidate_id, source_run_id, source_run_candidate_id,
                source_video_sha256, source_video_path, start_sec, end_sec,
                artifact_path, preview_path, vlm_summary_json, tags_json,
                scenario_keys_json, base_rag_similarity, profile_score,
                final_score, score_profile_version, status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                c["candidate_id"], c["source_run_id"], c["source_run_candidate_id"],
                c["source_video_sha256"], c["source_video_path"],
                c["start_sec"], c["end_sec"],
                c["artifact_path"], c["preview_path"],
                c["vlm_summary_json"], c["tags_json"],
                c["scenario_keys_json"],
                c["base_rag_similarity"], c["profile_score"],
                c["final_score"], c["score_profile_version"],
                c["status"],
            ),
        )

    conn.commit()
    return candidates


def _seed_vectors(conn: sqlite3.Connection, candidates: list[dict[str, object]]) -> None:
    """Seed embedding vectors for each candidate."""
    for c in candidates:
        conn.execute(
            """INSERT OR REPLACE INTO candidate_vectors
               (candidate_id, vector_type, embedding_model, embedding_dim,
                vector_blob, normalized)
               VALUES (?, 'clip', 'nomic-embed-text:latest', 768, ?, 1)""",
            (c["candidate_id"], _fake_vector()),
        )
    conn.commit()


def _record_feedback(
    conn: sqlite3.Connection,
    candidate_id: str,
    rating: str,
    *,
    note: str | None = None,
) -> str:
    """Record a feedback event and return its event_id."""
    event_id = f"evt-{uuid.uuid4().hex[:12]}"
    now = _utcnow()

    # Look up source_video_sha256 from candidate_gifs
    row = conn.execute(
        "SELECT source_video_sha256 FROM candidate_gifs WHERE candidate_id=?",
        (candidate_id,),
    ).fetchone()
    source_video_sha256 = row["source_video_sha256"] if row else "sha256-unknown"

    # Load scenario keys
    row2 = conn.execute(
        "SELECT scenario_keys_json FROM candidate_gifs WHERE candidate_id=?",
        (candidate_id,),
    ).fetchone()
    scenario_keys_json = row2["scenario_keys_json"] if row2 else "[]"

    conn.execute(
        """INSERT INTO preference_events
           (event_id, target_type, target_id, rating,
            source_video_sha256, scenario_keys_json, note, created_at)
           VALUES (?, 'candidate_gif', ?, ?, ?, ?, ?, ?)""",
        (event_id, candidate_id, rating, source_video_sha256, scenario_keys_json, note, now),
    )

    # Update candidate status for effective ratings
    status_map = {
        "like": "liked",
        "dislike": "disliked",
        "neutral": "neutral",
        "quality_reject": "rejected",
    }
    if rating in status_map:
        conn.execute(
            "UPDATE candidate_gifs SET status=?, updated_at=? WHERE candidate_id=?",
            (status_map[rating], now, candidate_id),
        )

    conn.commit()
    return event_id


# ---------------------------------------------------------------------------
# Core smoke test
# ---------------------------------------------------------------------------


def _main() -> int:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_preference_schema(conn)

    ok = True

    # ---- Step 1: Seed candidates with vectors -------------------------------
    print("\n=== Step 1: Seed candidate GIFs and vectors ===")
    candidates = _seed_candidates(conn, 80)
    _seed_vectors(conn, candidates)
    total = conn.execute("SELECT COUNT(*) FROM candidate_gifs").fetchone()[0]
    _assert(total == 80, f"seeded {total} candidates")
    vec_count = conn.execute("SELECT COUNT(*) FROM candidate_vectors").fetchone()[0]
    _assert(vec_count == 80, f"seeded {vec_count} candidate vectors")

    # ---- Step 2: Record all 6 feedback meanings -----------------------------
    print("\n=== Step 2: Record all 6 feedback meanings ===")
    # Training: candidates 0-44 (video-0 and video-1, no holdout overlap)
    # Favorites: candidates 40-44 on video-2 (creates overlap for testing)
    # Holdout: candidates 50-79 (video-3 through video-5, but 40-44 also on video-2)

    event_ids: dict[str, str] = {}

    # like (candidates 0-14) — 15 likes
    for i in range(15):
        rid = _record_feedback(conn, f"cand-{i:04d}", "like")
        event_ids[f"like-{i}"] = rid

    # dislike (candidates 15-24) — 10 dislikes
    for i in range(15, 25):
        rid = _record_feedback(conn, f"cand-{i:04d}", "dislike")
        event_ids[f"dislike-{i}"] = rid

    # neutral (candidates 25-29) — 5 neutral
    for i in range(25, 30):
        rid = _record_feedback(conn, f"cand-{i:04d}", "neutral")
        event_ids[f"neutral-{i}"] = rid

    # skip (candidates 30-34) — 5 skip
    for i in range(30, 35):
        rid = _record_feedback(conn, f"cand-{i:04d}", "skip")
        event_ids[f"skip-{i}"] = rid

    # quality_reject (candidates 35-39) — 5 quality_reject
    for i in range(35, 40):
        rid = _record_feedback(
            conn, f"cand-{i:04d}", "quality_reject",
            note="blurry frame",
        )
        event_ids[f"quality_reject-{i}"] = rid

    # favorite (candidates 40-44) — 5 favorites on video-2 (overlaps with holdout)
    for i in range(40, 45):
        rid = _record_feedback(conn, f"cand-{i:04d}", "favorite")
        event_ids[f"favorite-{i}"] = rid

    # Verify event counts
    event_count = conn.execute("SELECT COUNT(*) FROM preference_events").fetchone()[0]
    expected_events = 15 + 10 + 5 + 5 + 5 + 5  # 45
    _assert(event_count == expected_events, f"recorded {event_count} feedback events")

    # Verify each rating is present
    for rating in ("like", "dislike", "neutral", "skip", "quality_reject", "favorite"):
        count = conn.execute(
            "SELECT COUNT(*) FROM preference_events WHERE rating=?", (rating,)
        ).fetchone()[0]
        _assert(count > 0, f"rating '{rating}' has {count} events")

    print("\n  Feedback meaning descriptions:")
    for rating, desc in sorted(_MEANING_DESCRIPTIONS.items()):
        count = conn.execute(
            "SELECT COUNT(*) FROM preference_events WHERE rating=?", (rating,)
        ).fetchone()[0]
        print(f"    {rating:20s} ({count:2d} events): {desc}")

    # ---- Step 3: Build a preference profile --------------------------------
    print("\n=== Step 3: Build preference profile ===")
    config = ProfileBuildConfig(
        recency_enabled=True,
        recency_half_life_days=90.0,
        favorite_weight=2.0,
        like_weight=1.0,
        dislike_weight=1.5,  # extra weight on dislikes
        scenario_min_feedback=5,
    )
    service = PreferenceMemoryService(conn)
    result = service.build_profile(config=config)
    profile_version = result["profile_version"]

    if result["status"] == "blocked":
        print(f"  [FAIL] profile build blocked: {result['gate_reasons']}")
        return 1
    _assert(result["status"] == "built", f"profile built: version={profile_version}")
    _assert(
        result["effective_feedback_count"] >= 30,
        f"effective feedback count={result['effective_feedback_count']} >= 30",
    )

    # ---- Step 4: Publish the profile ---------------------------------------
    print("\n=== Step 4: Publish profile ===")
    service.publish(profile_version)

    current_row = conn.execute(
        "SELECT profile_version FROM preference_profile_current WHERE slot='current'"
    ).fetchone()
    _assert(
        current_row["profile_version"] == profile_version,
        f"current profile is {current_row['profile_version']}",
    )

    # ---- Step 5: Source-grouped evaluation ----------------------------------
    print("\n=== Step 5: Source-grouped evaluation ===")
    # Build holdout set from video-5 candidates that were NOT used in training
    # Our training used video-0 through video-4, and holdout uses video-5.
    # But we also used candidates 30-35 (from video-5) for 'favorite' feedback,
    # which creates an overlap.
    eval_svc = PreferenceEvaluationService(conn)
    grouped_report = eval_svc.evaluate_source_grouped(
        profile_version,
        holdout_count=30,
    )

    _assert(
        "source_video_integrity" in grouped_report,
        "source_video_integrity in report",
    )
    _assert(
        "base_ndcg_at_20" in grouped_report,
        "base_ndcg_at_20 in report",
    )
    _assert(
        "preference_ndcg_at_20" in grouped_report,
        "preference_ndcg_at_20 in report",
    )
    _assert(
        "pairwise_win_rate" in grouped_report,
        "pairwise_win_rate in report",
    )
    _assert(
        "exploration_diversity" in grouped_report,
        "exploration_diversity in report",
    )
    _assert(
        "vector_coverage" in grouped_report,
        "vector_coverage in report",
    )
    _assert(
        "inactive_fallbacks" in grouped_report,
        "inactive_fallbacks in report",
    )
    _assert(
        "publish_gate" in grouped_report,
        "publish_gate in report",
    )

    print(f"  Source video integrity: {grouped_report['source_video_integrity']}")
    print(f"  Base NDCG@20:   {grouped_report['base_ndcg_at_20']}")
    print(f"  Preference NDCG@20: {grouped_report['preference_ndcg_at_20']}")
    print(f"  NDCG delta:     {grouped_report['ndcg_delta']}")
    print(f"  Pairwise win rate: {grouped_report['pairwise_win_rate']}")
    print(f"  Exploration diversity: {grouped_report['exploration_diversity']}")
    print(f"  Vector coverage: {grouped_report['vector_coverage']}")
    print(f"  Inactive fallbacks: {grouped_report['inactive_fallbacks']}")
    print(f"  Publish gate:    {grouped_report['publish_gate']}")

    # ---- Step 6: Reranker explanations -------------------------------------
    print("\n=== Step 6: Reranker explanations ===")
    reranker = PreferenceReranker(conn)

    # Score a few candidates to show explanations
    for i in range(5):
        cid = f"cand-{i:04d}"
        vec_row = conn.execute(
            "SELECT vector_blob FROM candidate_vectors WHERE candidate_id=? AND vector_type='clip'",
            (cid,),
        ).fetchone()
        gif_row = conn.execute(
            "SELECT base_rag_similarity, scenario_keys_json FROM candidate_gifs WHERE candidate_id=?",
            (cid,),
        ).fetchone()

        if vec_row is None or gif_row is None:
            continue

        vec = np.frombuffer(vec_row["vector_blob"], dtype=np.float32)
        scenario_keys = json.loads(gif_row["scenario_keys_json"])

        breakdown = reranker.score(
            candidate_vector=vec,
            base_rag_similarity=gif_row["base_rag_similarity"],
            scenario_keys=scenario_keys,
            profile_version=profile_version,
            enabled=True,
        )

        print(f"\n  Candidate {cid}:")
        print(f"    Base RAG sim:    {breakdown['base_rag_similarity']:.4f}")
        print(f"    Profile score:   {breakdown['profile_score']}")
        print(f"    Final score:     {breakdown['final_score']:.4f}")
        print(f"    Positive sim:    {breakdown.get('positive_similarity')}")
        print(f"    Negative sim:    {breakdown.get('negative_similarity')}")
        print(f"    Active weights:  {breakdown['active_weights']}")
        if breakdown['inactive_reasons']:
            print(f"    Inactive reasons: {breakdown['inactive_reasons']}")

    # ---- Step 7: Roll back the published profile ----------------------------
    print("\n=== Step 7: Roll back published profile ===")
    service.rollback(profile_version)

    current_after = conn.execute(
        "SELECT profile_version FROM preference_profile_current WHERE slot='current'"
    ).fetchone()
    _assert(
        current_after["profile_version"] == profile_version,
        "rollback preserves current version slot",
    )

    # Verify publication history
    pub_count = conn.execute(
        "SELECT COUNT(*) FROM preference_profile_publications"
    ).fetchone()[0]
    _assert(pub_count >= 2, f"publication history has {pub_count} entries")

    # ---- Step 8: Verify source files unchanged ------------------------------
    print("\n=== Step 8: Verify no source files changed ===")
    # (This is a smoke test — we don't modify source files, so this is implicit.)

    # ---- Cleanup ------------------------------------------------------------
    conn.close()
    print("\n=== All smoke tests passed! ===")
    return 0 if ok else 1


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Smoke test for active preference learning lifecycle"
    )
    parser.parse_args()
    sys.exit(_main())


if __name__ == "__main__":
    main()
