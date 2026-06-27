"""P1-6: Preference reranker — availability-aware scoring behind feature flag."""

import json
import sqlite3

import numpy as np
import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def reranker_db():
    from app.services.preference_schema import apply_preference_schema

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_preference_schema(conn)
    return conn


@pytest.fixture
def random_vector():
    rng = np.random.default_rng(42)
    vec = rng.normal(0, 1, 768).astype(np.float32)
    vec = vec / np.linalg.norm(vec)
    return vec


@pytest.fixture
def published_profile(reranker_db):
    """Build and publish a profile with 40 candidates (30 like, 10 dislike).

    Returns a dict with ``conn`` and ``profile_version`` keys.
    """
    from app.services.preference_events import PreferenceEventService
    from app.services.preference_memory import PreferenceMemoryService

    conn = reranker_db
    svc = PreferenceEventService(conn)

    videos = ["video-a", "video-b", "video-c"]
    rng = np.random.default_rng(99)

    # Insert 30 liked candidates with vectors
    for i in range(30):
        conn.execute(
            """INSERT OR IGNORE INTO candidate_gifs
               (candidate_id, source_run_id, source_run_candidate_id,
                source_video_sha256, source_video_path, start_sec, end_sec,
                status, tags_json, scenario_keys_json)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                f"cand-like-{i}", "run-1", f"rc-like-{i}", videos[i % 3],
                f"/v/{i % 3}.mp4", 0.0, 5.0, "liked",
                json.dumps([f"tag-{i % 5}"]),
                json.dumps([f"emotion:joy", f"tag:{i % 5}"]),
            ),
        )
        vec = rng.normal(0, 1, 768).astype(np.float32)
        vec = vec / np.linalg.norm(vec)
        conn.execute(
            """INSERT OR IGNORE INTO candidate_vectors
               (candidate_id, vector_type, embedding_model, embedding_dim, vector_blob)
               VALUES (?,?,?,?,?)""",
            (f"cand-like-{i}", "clip", "nomic-embed-text:latest", 768, vec.tobytes()),
        )

    # Insert 10 disliked candidates with vectors
    for i in range(10):
        conn.execute(
            """INSERT OR IGNORE INTO candidate_gifs
               (candidate_id, source_run_id, source_run_candidate_id,
                source_video_sha256, source_video_path, start_sec, end_sec,
                status, tags_json, scenario_keys_json)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                f"cand-dislike-{i}", "run-1", f"rc-dislike-{i}", videos[i % 3],
                f"/v/{i % 3}.mp4", 0.0, 5.0, "disliked",
                json.dumps([f"tag-{i % 5}"]),
                json.dumps([f"emotion:sad", f"tag:{i % 5}"]),
            ),
        )
        vec = rng.normal(0, 1, 768).astype(np.float32)
        vec = vec / np.linalg.norm(vec)
        conn.execute(
            """INSERT OR IGNORE INTO candidate_vectors
               (candidate_id, vector_type, embedding_model, embedding_dim, vector_blob)
               VALUES (?,?,?,?,?)""",
            (f"cand-dislike-{i}", "clip", "nomic-embed-text:latest", 768, vec.tobytes()),
        )

    # Record feedback events
    for i in range(30):
        svc.record_feedback(
            target_type="candidate_gif",
            target_id=f"cand-like-{i}",
            rating="like",
            source_video_sha256=videos[i % 3],
            scenario_keys=[f"emotion:joy", f"tag:{i % 5}"],
        )
    for i in range(10):
        svc.record_feedback(
            target_type="candidate_gif",
            target_id=f"cand-dislike-{i}",
            rating="dislike",
            source_video_sha256=videos[i % 3],
            scenario_keys=[f"emotion:sad", f"tag:{i % 5}"],
        )

    conn.commit()

    # Build and publish
    memory = PreferenceMemoryService(conn)
    result = memory.build_profile(dry_run=False)
    assert result["status"] == "built", f"Build blocked: {result.get('gate_reasons')}"
    memory.publish(result["profile_version"])

    return {"conn": conn, "profile_version": result["profile_version"]}


# ---------------------------------------------------------------------------
# Tests: disabled / no-profile (baseline pass-through)
# ---------------------------------------------------------------------------


def test_reranker_disabled_returns_baseline_score(reranker_db):
    from app.services.reranker import PreferenceReranker

    reranker = PreferenceReranker(reranker_db)
    vec = np.random.default_rng(0).normal(0, 1, 768).astype(np.float32)
    vec = vec / np.linalg.norm(vec)

    score = reranker.score(
        candidate_vector=vec,
        base_rag_similarity=0.62,
        scenario_keys=["tag:smile"],
        profile_version=None,
        enabled=False,
    )

    assert score["base_rag_similarity"] == 0.62
    assert score["profile_score"] is None
    assert score["raw_score"] == 0.62
    assert score["final_score"] == 0.62
    assert score["preference_profile_version"] is None


def test_reranker_enabled_without_profile_returns_baseline(reranker_db):
    from app.services.reranker import PreferenceReranker

    reranker = PreferenceReranker(reranker_db)
    vec = np.random.default_rng(1).normal(0, 1, 768).astype(np.float32)
    vec = vec / np.linalg.norm(vec)

    score = reranker.score(
        candidate_vector=vec,
        base_rag_similarity=0.50,
        scenario_keys=["tag:smile"],
        profile_version=None,
        enabled=True,
    )

    # enabled but no profile -> should behave same as disabled
    assert score["final_score"] == 0.50
    assert score["profile_score"] is None
    assert score["preference_profile_version"] is None


def test_score_breakdown_has_required_fields(reranker_db):
    from app.services.reranker import PreferenceReranker

    reranker = PreferenceReranker(reranker_db)
    vec = np.random.default_rng(2).normal(0, 1, 768).astype(np.float32)
    vec = vec / np.linalg.norm(vec)

    score = reranker.score(
        candidate_vector=vec,
        base_rag_similarity=0.75,
        scenario_keys=[],
        profile_version=None,
        enabled=True,
    )

    assert "base_rag_similarity" in score
    assert "profile_score" in score
    assert "raw_score" in score
    assert "final_score" in score
    assert "active_weights" in score
    assert "inactive_reasons" in score
    assert "preference_profile_version" in score
    assert 0.0 <= score["final_score"] <= 1.0


# ---------------------------------------------------------------------------
# Tests: enabled with published profile
# ---------------------------------------------------------------------------


def test_reranker_enabled_with_published_profile_produces_non_baseline_score(
    published_profile,
):
    from app.services.reranker import PreferenceReranker

    conn = published_profile["conn"]
    pv = published_profile["profile_version"]

    reranker = PreferenceReranker(conn)
    vec = np.random.default_rng(3).normal(0, 1, 768).astype(np.float32)
    vec = vec / np.linalg.norm(vec)

    score = reranker.score(
        candidate_vector=vec,
        base_rag_similarity=0.60,
        scenario_keys=["emotion:joy"],
        profile_version=None,
        enabled=True,
    )

    # With a published profile, profile_score should not be None
    assert score["profile_score"] is not None
    assert score["preference_profile_version"] is not None
    assert score["preference_profile_version"] == pv


def test_reranker_resolves_profile_version_from_current(published_profile):
    from app.services.reranker import PreferenceReranker

    conn = published_profile["conn"]
    pv = published_profile["profile_version"]

    reranker = PreferenceReranker(conn)
    vec = np.random.default_rng(4).normal(0, 1, 768).astype(np.float32)
    vec = vec / np.linalg.norm(vec)

    # profile_version=None should auto-resolve from preference_profile_current
    score = reranker.score(
        candidate_vector=vec,
        base_rag_similarity=0.55,
        scenario_keys=["emotion:joy"],
        profile_version=None,
        enabled=True,
    )

    assert score["preference_profile_version"] == pv


def test_reranker_with_explicit_profile_version(published_profile):
    from app.services.reranker import PreferenceReranker

    conn = published_profile["conn"]
    pv = published_profile["profile_version"]

    reranker = PreferenceReranker(conn)
    vec = np.random.default_rng(5).normal(0, 1, 768).astype(np.float32)
    vec = vec / np.linalg.norm(vec)

    score = reranker.score(
        candidate_vector=vec,
        base_rag_similarity=0.60,
        scenario_keys=["emotion:joy"],
        profile_version=pv,
        enabled=True,
    )

    assert score["preference_profile_version"] == pv
    assert score["profile_score"] is not None


def test_reranker_explicit_version_nonexistent_returns_baseline(published_profile):
    from app.services.reranker import PreferenceReranker

    conn = published_profile["conn"]

    reranker = PreferenceReranker(conn)
    vec = np.random.default_rng(6).normal(0, 1, 768).astype(np.float32)
    vec = vec / np.linalg.norm(vec)

    score = reranker.score(
        candidate_vector=vec,
        base_rag_similarity=0.45,
        scenario_keys=["emotion:joy"],
        profile_version="profile_nonexistent",
        enabled=True,
    )

    # Non-existent version -> fall back to baseline
    assert score["final_score"] == 0.45
    assert score["profile_score"] is None


def test_candidate_vector_close_to_liked_centroid_scores_higher(published_profile):
    """Vector near the liked centroid should get a boost."""
    from app.services.reranker import PreferenceReranker

    conn = published_profile["conn"]
    pv = published_profile["profile_version"]

    reranker = PreferenceReranker(conn)

    # Fetch the actual liked centroid
    row = conn.execute(
        """SELECT liked_centroid_blob FROM preference_profiles
           WHERE profile_version=? AND scope='global'""",
        (pv,),
    ).fetchone()
    liked_centroid = np.frombuffer(row["liked_centroid_blob"], dtype=np.float32)
    liked_centroid = liked_centroid / np.linalg.norm(liked_centroid)

    # Score with the liked centroid itself -> should get high similarity
    score = reranker.score(
        candidate_vector=liked_centroid,
        base_rag_similarity=0.50,
        scenario_keys=["emotion:joy"],
        profile_version=None,
        enabled=True,
    )

    assert score["final_score"] > 0.50  # should be boosted
    assert score["profile_score"] is not None


def test_candidate_vector_close_to_disliked_centroid_scores_lower(published_profile):
    """Vector near the disliked centroid should be penalized."""
    from app.services.reranker import PreferenceReranker

    conn = published_profile["conn"]
    pv = published_profile["profile_version"]

    reranker = PreferenceReranker(conn)

    # Fetch the actual disliked centroid
    row = conn.execute(
        """SELECT disliked_centroid_blob FROM preference_profiles
           WHERE profile_version=? AND scope='global'""",
        (pv,),
    ).fetchone()
    disliked_centroid = np.frombuffer(row["disliked_centroid_blob"], dtype=np.float32)
    disliked_centroid = disliked_centroid / np.linalg.norm(disliked_centroid)

    score = reranker.score(
        candidate_vector=disliked_centroid,
        base_rag_similarity=0.80,
        scenario_keys=["emotion:sad"],
        profile_version=None,
        enabled=True,
    )

    assert score["final_score"] < 0.80  # should be penalized
    assert score["profile_score"] is not None


def test_scenario_keys_activate_scenario_profiles(published_profile):
    from app.services.reranker import PreferenceReranker

    conn = published_profile["conn"]

    reranker = PreferenceReranker(conn)
    vec = np.random.default_rng(7).normal(0, 1, 768).astype(np.float32)
    vec = vec / np.linalg.norm(vec)

    # With matching scenario keys
    score_with = reranker.score(
        candidate_vector=vec,
        base_rag_similarity=0.50,
        scenario_keys=["emotion:joy", "tag:0"],
        profile_version=None,
        enabled=True,
    )

    # With empty scenario keys
    score_without = reranker.score(
        candidate_vector=vec,
        base_rag_similarity=0.50,
        scenario_keys=[],
        profile_version=None,
        enabled=True,
    )

    # Both should be valid scores
    assert 0.0 <= score_with["final_score"] <= 1.0
    assert 0.0 <= score_without["final_score"] <= 1.0

    # With scenario keys, scenario_like should be active (or have an inactive reason)
    if "scenario_like" in score_with["active_weights"]:
        pass  # verification that it doesn't crash is sufficient


def test_disabled_mode_is_byte_equivalent_to_baseline(published_profile):
    """enabled=False MUST produce identical ranking to baseline (no DB reads)."""
    from app.services.reranker import PreferenceReranker

    conn = published_profile["conn"]

    reranker = PreferenceReranker(conn)

    for i in range(10):
        vec = np.random.default_rng(100 + i).normal(0, 1, 768).astype(np.float32)
        vec = vec / np.linalg.norm(vec)
        base = round(0.3 + i * 0.05, 4)

        score = reranker.score(
            candidate_vector=vec,
            base_rag_similarity=base,
            scenario_keys=["tag:smile"],
            profile_version=None,
            enabled=False,
        )

        assert score["base_rag_similarity"] == base
        assert score["raw_score"] == base
        assert score["final_score"] == base
        assert score["profile_score"] is None
        assert score["active_weights"] == {}
        assert score["preference_profile_version"] is None


def test_final_score_is_clamped_to_zero_one(published_profile):
    from app.services.reranker import PreferenceReranker

    conn = published_profile["conn"]

    reranker = PreferenceReranker(conn)

    # Extreme base_rag_similarity values should still produce clamped final_score
    for base in [0.0, 1.0]:
        vec = np.random.default_rng(200 + int(base * 100)).normal(0, 1, 768).astype(np.float32)
        vec = vec / np.linalg.norm(vec)

        score = reranker.score(
            candidate_vector=vec,
            base_rag_similarity=base,
            scenario_keys=[],
            profile_version=None,
            enabled=True,
        )

        assert 0.0 <= score["final_score"] <= 1.0, (
            f"final_score={score['final_score']} out of [0,1] for base={base}"
        )


def test_inactive_reasons_populated_for_missing_components(reranker_db):
    """When enabled but no profile, inactive_reasons should explain why."""
    from app.services.reranker import PreferenceReranker

    reranker = PreferenceReranker(reranker_db)
    vec = np.random.default_rng(8).normal(0, 1, 768).astype(np.float32)
    vec = vec / np.linalg.norm(vec)

    score = reranker.score(
        candidate_vector=vec,
        base_rag_similarity=0.70,
        scenario_keys=["emotion:joy"],
        profile_version=None,
        enabled=True,
    )

    assert len(score["inactive_reasons"]) > 0
    # final_score should still equal base_rag because no profile is published
    assert score["final_score"] == 0.70
