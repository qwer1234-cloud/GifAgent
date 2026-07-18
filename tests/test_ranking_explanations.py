"""P3T5: Explainable ranking breakdown — tests for ScoreBreakdown, nearest
positive IDs, and fallback behaviour."""

import json
import sqlite3

import numpy as np
import pytest


# ---------------------------------------------------------------------------
# Fixtures (mirrored from test_preference_reranker.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def reranker_db():
    from app.services.preference_schema import apply_preference_schema

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_preference_schema(conn)
    return conn


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
# Tests: final score reconstructs from components
# ---------------------------------------------------------------------------


def test_final_score_is_consistent_with_components(published_profile):
    """With a published profile the final_score is in [0, 1] and all
    components are present."""
    from app.services.ranking_explanations import compute_ranking_explanation

    conn = published_profile["conn"]
    pv = published_profile["profile_version"]

    rng = np.random.default_rng(42)
    vec = rng.normal(0, 1, 768).astype(np.float32)
    vec = vec / np.linalg.norm(vec)

    result = compute_ranking_explanation(
        conn=conn,
        candidate_id="cand-like-0",
        candidate_vector=vec,
        base_rag_similarity=0.60,
        scenario_keys=["emotion:joy"],
        profile_version=None,
        enabled=True,
    )

    # All required fields are present
    assert result["base_quality"] == 0.60
    assert isinstance(result["final_score"], float)
    assert 0.0 <= result["final_score"] <= 1.0
    assert result["preference_profile_version"] == pv
    # Diversity and temporal adjustments are placeholders for now
    assert result["diversity_adjustment"] == 0.0
    assert result["temporal_coverage_adjustment"] == 0.0
    # The final_score should differ from base_quality when a profile is active
    assert result["final_score"] != result["base_quality"] or (
        result["positive_similarity"] is None
        and result["negative_penalty"] is None
    )


def test_positive_similarity_reflects_like_centroid_proximity(published_profile):
    """A candidate vector that exactly matches the liked centroid should
    produce a positive_similarity near 1.0."""
    from app.services.ranking_explanations import compute_ranking_explanation

    conn = published_profile["conn"]
    pv = published_profile["profile_version"]

    # Fetch the liked centroid
    row = conn.execute(
        """SELECT liked_centroid_blob FROM preference_profiles
           WHERE profile_version=? AND scope='global'""",
        (pv,),
    ).fetchone()
    liked = np.frombuffer(row["liked_centroid_blob"], dtype=np.float32)
    liked = liked / np.linalg.norm(liked)

    result = compute_ranking_explanation(
        conn=conn,
        candidate_id="cand-like-5",
        candidate_vector=liked,
        base_rag_similarity=0.50,
        scenario_keys=["emotion:joy"],
        profile_version=None,
        enabled=True,
    )

    assert result["positive_similarity"] is not None
    assert result["positive_similarity"] > 0.0
    assert result["final_score"] > result["base_quality"]


# ---------------------------------------------------------------------------
# Tests: negative similarity lowers the score
# ---------------------------------------------------------------------------


def test_negative_penalty_lowers_final_score(published_profile):
    """A candidate close to the disliked centroid should have a
    negative_penalty set and a final_score lower than base_quality."""
    from app.services.ranking_explanations import compute_ranking_explanation

    conn = published_profile["conn"]
    pv = published_profile["profile_version"]

    row = conn.execute(
        """SELECT disliked_centroid_blob FROM preference_profiles
           WHERE profile_version=? AND scope='global'""",
        (pv,),
    ).fetchone()
    disliked = np.frombuffer(row["disliked_centroid_blob"], dtype=np.float32)
    disliked = disliked / np.linalg.norm(disliked)

    result = compute_ranking_explanation(
        conn=conn,
        candidate_id="cand-dislike-0",
        candidate_vector=disliked,
        base_rag_similarity=0.80,
        scenario_keys=["emotion:sad"],
        profile_version=None,
        enabled=True,
    )

    assert result["negative_penalty"] is not None
    assert result["negative_penalty"] > 0.0
    assert result["final_score"] < result["base_quality"]


# ---------------------------------------------------------------------------
# Tests: nearest positive IDs
# ---------------------------------------------------------------------------


def test_nearest_positive_ids_contains_only_liked_favorite(published_profile):
    """Nearest positive IDs should contain only candidates that received
    'like' or 'favorite' feedback, and exclude the query candidate itself."""
    from app.services.ranking_explanations import compute_ranking_explanation

    conn = published_profile["conn"]

    rng = np.random.default_rng(77)
    vec = rng.normal(0, 1, 768).astype(np.float32)
    vec = vec / np.linalg.norm(vec)

    result = compute_ranking_explanation(
        conn=conn,
        candidate_id="cand-like-0",
        candidate_vector=vec,
        base_rag_similarity=0.50,
        scenario_keys=[],
        profile_version=None,
        enabled=True,
    )

    nearest = result["nearest_positive_ids"]
    assert len(nearest) <= 5

    for cid in nearest:
        assert cid.startswith("cand-like-"), (
            f"Expected liked/favorite candidate, got {cid}"
        )
        assert cid != "cand-like-0", "Should not include the candidate itself"


def test_nearest_positive_ids_empty_when_no_profile(reranker_db):
    """Without a published profile, nearest_positive_ids is empty."""
    from app.services.ranking_explanations import compute_ranking_explanation

    rng = np.random.default_rng(42)
    vec = rng.normal(0, 1, 768).astype(np.float32)
    vec = vec / np.linalg.norm(vec)

    result = compute_ranking_explanation(
        conn=reranker_db,
        candidate_id="cand-none",
        candidate_vector=vec,
        base_rag_similarity=0.50,
        scenario_keys=[],
        profile_version=None,
        enabled=True,
    )

    assert result["nearest_positive_ids"] == []


# ---------------------------------------------------------------------------
# Tests: fallback / availability errors
# ---------------------------------------------------------------------------


def test_missing_profile_returns_unchanged_base_score(reranker_db):
    """When no profile is published, final_score == base_quality with an
    inactive reason."""
    from app.services.ranking_explanations import compute_ranking_explanation

    rng = np.random.default_rng(42)
    vec = rng.normal(0, 1, 768).astype(np.float32)
    vec = vec / np.linalg.norm(vec)

    result = compute_ranking_explanation(
        conn=reranker_db,
        candidate_id="nonexistent",
        candidate_vector=vec,
        base_rag_similarity=0.55,
        scenario_keys=[],
        profile_version=None,
        enabled=True,
    )

    assert result["final_score"] == 0.55
    assert result["base_quality"] == 0.55
    assert result["positive_similarity"] is None
    assert result["negative_penalty"] is None
    assert len(result["inactive_reasons"]) > 0
    assert result["preference_profile_version"] is None
    assert result["nearest_positive_ids"] == []
    assert result["diversity_adjustment"] == 0.0
    assert result["temporal_coverage_adjustment"] == 0.0


def test_disabled_mode_returns_base_quality(reranker_db):
    """When enabled=False, final_score == base_quality with empty
    nearest_positive_ids."""
    from app.services.ranking_explanations import compute_ranking_explanation

    rng = np.random.default_rng(42)
    vec = rng.normal(0, 1, 768).astype(np.float32)
    vec = vec / np.linalg.norm(vec)

    result = compute_ranking_explanation(
        conn=reranker_db,
        candidate_id="nonexistent",
        candidate_vector=vec,
        base_rag_similarity=0.70,
        scenario_keys=[],
        profile_version=None,
        enabled=False,
    )

    assert result["final_score"] == 0.70
    assert result["base_quality"] == 0.70
    assert result["positive_similarity"] is None
    assert result["negative_penalty"] is None
    assert result["preference_profile_version"] is None
    assert result["nearest_positive_ids"] == []


def test_nonexistent_profile_version_returns_baseline(published_profile):
    """An explicit profile_version that does not exist yields unchanged
    base score with an inactive reason."""
    from app.services.ranking_explanations import compute_ranking_explanation

    conn = published_profile["conn"]

    rng = np.random.default_rng(42)
    vec = rng.normal(0, 1, 768).astype(np.float32)
    vec = vec / np.linalg.norm(vec)

    result = compute_ranking_explanation(
        conn=conn,
        candidate_id="cand-like-0",
        candidate_vector=vec,
        base_rag_similarity=0.45,
        scenario_keys=[],
        profile_version="profile_nonexistent",
        enabled=True,
    )

    assert result["final_score"] == 0.45
    assert result["positive_similarity"] is None
    assert result["negative_penalty"] is None
    assert result["preference_profile_version"] is None
    assert result["nearest_positive_ids"] == []


# ---------------------------------------------------------------------------
# Tests: interface compliance
# ---------------------------------------------------------------------------


def test_score_breakdown_has_all_required_fields(published_profile):
    """The returned ScoreBreakdown should contain every field defined
    in the interface."""
    from app.services.ranking_explanations import compute_ranking_explanation, ScoreBreakdown

    conn = published_profile["conn"]

    rng = np.random.default_rng(42)
    vec = rng.normal(0, 1, 768).astype(np.float32)
    vec = vec / np.linalg.norm(vec)

    result = compute_ranking_explanation(
        conn=conn,
        candidate_id="cand-like-0",
        candidate_vector=vec,
        base_rag_similarity=0.50,
        scenario_keys=[],
        profile_version=None,
        enabled=True,
    )

    expected_fields = set(ScoreBreakdown.__annotations__.keys())
    actual_fields = set(result.keys())

    missing = expected_fields - actual_fields
    extra = actual_fields - expected_fields
    assert not missing, f"Missing required fields: {missing}"
    assert not extra, f"Unexpected extra fields: {extra}"
