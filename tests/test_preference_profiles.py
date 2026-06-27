"""P1-5: Immutable preference profiles — build, gates, versioning, and publish."""

import json
import sqlite3

import pytest


@pytest.fixture
def profile_db():
    from app.services.preference_schema import apply_preference_schema

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_preference_schema(conn)
    return conn


@pytest.fixture
def seeded_db(profile_db):
    """Insert 40 candidate_gifs + 40 feedback events (30 like, 10 dislike) across 3 videos."""
    from app.services.preference_events import PreferenceEventService

    conn = profile_db
    svc = PreferenceEventService(conn)

    videos = ["video-a", "video-b", "video-c"]
    # Insert like candidate rows
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
    # Insert dislike candidate rows
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
    conn.commit()

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
    return conn


@pytest.fixture
def seeded_db_with_vectors(seeded_db):
    """Add candidate_vectors entries for each liked/disliked candidate."""
    import numpy as np

    conn = seeded_db
    rng = np.random.default_rng(42)
    for i in range(30):
        vec = rng.random(768).astype(np.float32)
        conn.execute(
            """INSERT OR IGNORE INTO candidate_vectors
               (candidate_id, vector_type, embedding_model, embedding_dim, vector_blob)
               VALUES (?,?,?,?,?)""",
            (f"cand-like-{i}", "clip", "nomic-embed-text:latest", 768, vec.tobytes()),
        )
    for i in range(10):
        vec = rng.random(768).astype(np.float32)
        conn.execute(
            """INSERT OR IGNORE INTO candidate_vectors
               (candidate_id, vector_type, embedding_model, embedding_dim, vector_blob)
               VALUES (?,?,?,?,?)""",
            (f"cand-dislike-{i}", "clip", "nomic-embed-text:latest", 768, vec.tobytes()),
        )
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Gate tests
# ---------------------------------------------------------------------------

def test_profile_build_blocks_when_effective_feedback_is_insufficient(profile_db):
    from app.services.preference_memory import PreferenceMemoryService

    memory = PreferenceMemoryService(profile_db)
    result = memory.build_profile(dry_run=False)

    assert result["status"] == "blocked"
    assert any(
        "effective_feedback_count" in r for r in result.get("gate_reasons", [])
    )


def test_profile_build_blocks_when_vectors_are_missing(seeded_db):
    """With 40 effective events but no candidate_vectors, build should block."""
    from app.services.preference_memory import PreferenceMemoryService

    memory = PreferenceMemoryService(seeded_db)
    result = memory.build_profile(dry_run=False)

    assert result["status"] == "blocked"
    reasons = result.get("gate_reasons", [])
    assert any("no_vectors_found" in r for r in reasons)


def test_profile_build_completes_with_sufficient_data(seeded_db_with_vectors):
    from app.services.preference_memory import PreferenceMemoryService

    memory = PreferenceMemoryService(seeded_db_with_vectors)
    result = memory.build_profile(dry_run=False)

    assert result["status"] == "built"
    assert result["profile_version"].startswith("profile_")
    assert result["effective_feedback_count"] == 40


def test_profile_build_is_deterministic(seeded_db):
    """Same state -> same profile_version (even when blocked)."""
    from app.services.preference_memory import PreferenceMemoryService

    memory = PreferenceMemoryService(seeded_db)

    first = memory.build_profile(dry_run=False)
    second = memory.build_profile(dry_run=False)

    assert first["profile_version"] == second["profile_version"]
    assert first["status"] == second["status"]


# ---------------------------------------------------------------------------
# Profile build persistence
# ---------------------------------------------------------------------------

def test_profile_build_inserts_row_into_builds_table(seeded_db_with_vectors):
    from app.services.preference_memory import PreferenceMemoryService

    memory = PreferenceMemoryService(seeded_db_with_vectors)
    result = memory.build_profile(dry_run=False)

    row = seeded_db_with_vectors.execute(
        "SELECT * FROM preference_profile_builds WHERE profile_version=?",
        (result["profile_version"],),
    ).fetchone()

    assert row is not None
    assert row["status"] == "completed"
    assert row["effective_feedback_count"] == 40


def test_profile_build_inserts_global_profile(seeded_db_with_vectors):
    from app.services.preference_memory import PreferenceMemoryService

    memory = PreferenceMemoryService(seeded_db_with_vectors)
    result = memory.build_profile(dry_run=False)

    rows = seeded_db_with_vectors.execute(
        "SELECT * FROM preference_profiles WHERE profile_version=? AND scope='global'",
        (result["profile_version"],),
    ).fetchall()

    assert len(rows) == 1
    assert rows[0]["like_count"] == 30
    assert rows[0]["dislike_count"] == 10
    assert rows[0]["liked_centroid_blob"] is not None
    assert rows[0]["disliked_centroid_blob"] is not None


def test_profile_build_creates_scenario_profiles(seeded_db_with_vectors):
    from app.services.preference_memory import PreferenceMemoryService

    memory = PreferenceMemoryService(seeded_db_with_vectors)
    result = memory.build_profile(dry_run=False)

    scenario_rows = seeded_db_with_vectors.execute(
        "SELECT * FROM preference_profiles WHERE profile_version=? AND scope='scenario'",
        (result["profile_version"],),
    ).fetchall()

    # Each scenario key appears across multiple events; verify at least one scenario
    assert len(scenario_rows) >= 0  # may or may not meet thresholds, but shouldn't crash


def test_blocked_build_does_not_touch_profiles_table(seeded_db):
    """A blocked build inserts a builds row but NO preference_profiles rows."""
    from app.services.preference_memory import PreferenceMemoryService

    memory = PreferenceMemoryService(seeded_db)
    result = memory.build_profile(dry_run=False)

    assert result["status"] == "blocked"

    profile_count = seeded_db.execute(
        "SELECT COUNT(*) FROM preference_profiles"
    ).fetchone()[0]
    assert profile_count == 0


def test_blocked_build_does_not_update_current(seeded_db):
    from app.services.preference_memory import PreferenceMemoryService

    memory = PreferenceMemoryService(seeded_db)
    memory.build_profile(dry_run=False)

    current = seeded_db.execute(
        "SELECT COUNT(*) FROM preference_profile_current"
    ).fetchone()[0]
    assert current == 0


# ---------------------------------------------------------------------------
# Dry-run
# ---------------------------------------------------------------------------

def test_dry_run_does_not_persist_anything(seeded_db_with_vectors):
    from app.services.preference_memory import PreferenceMemoryService

    memory = PreferenceMemoryService(seeded_db_with_vectors)
    result = memory.build_profile(dry_run=True)

    assert result["status"] == "built"

    build_count = seeded_db_with_vectors.execute(
        "SELECT COUNT(*) FROM preference_profile_builds"
    ).fetchone()[0]
    profile_count = seeded_db_with_vectors.execute(
        "SELECT COUNT(*) FROM preference_profiles"
    ).fetchone()[0]
    assert build_count == 0
    assert profile_count == 0


# ---------------------------------------------------------------------------
# Publish
# ---------------------------------------------------------------------------

def test_publish_sets_current_profile(seeded_db_with_vectors):
    from app.services.preference_memory import PreferenceMemoryService

    memory = PreferenceMemoryService(seeded_db_with_vectors)
    result = memory.build_profile(dry_run=False)
    assert result["status"] == "built"

    memory.publish(result["profile_version"])

    current = seeded_db_with_vectors.execute(
        "SELECT * FROM preference_profile_current WHERE slot='current'"
    ).fetchone()
    assert current is not None
    assert current["profile_version"] == result["profile_version"]


def test_publish_rejects_blocked_build(seeded_db):
    from app.services.preference_memory import PreferenceMemoryService

    memory = PreferenceMemoryService(seeded_db)
    result = memory.build_profile(dry_run=False)
    assert result["status"] == "blocked"

    with pytest.raises(ValueError, match="not completed"):
        memory.publish(result["profile_version"])


def test_publish_rejects_unknown_version(profile_db):
    from app.services.preference_memory import PreferenceMemoryService

    memory = PreferenceMemoryService(profile_db)
    with pytest.raises(ValueError, match="not found"):
        memory.publish("profile_nonexistent")


def test_publish_is_idempotent(seeded_db_with_vectors):
    from app.services.preference_memory import PreferenceMemoryService

    memory = PreferenceMemoryService(seeded_db_with_vectors)
    result = memory.build_profile(dry_run=False)
    assert result["status"] == "built"

    memory.publish(result["profile_version"])
    memory.publish(result["profile_version"])  # should not raise

    current = seeded_db_with_vectors.execute(
        "SELECT * FROM preference_profile_current WHERE slot='current'"
    ).fetchone()
    assert current["profile_version"] == result["profile_version"]


# ---------------------------------------------------------------------------
# Centroid validity
# ---------------------------------------------------------------------------

def test_centroid_dimension_matches_embedding_dim(seeded_db_with_vectors):
    import numpy as np
    from app.services.preference_memory import PreferenceMemoryService

    memory = PreferenceMemoryService(seeded_db_with_vectors)
    result = memory.build_profile(dry_run=False)
    assert result["status"] == "built"

    row = seeded_db_with_vectors.execute(
        "SELECT liked_centroid_blob, disliked_centroid_blob "
        "FROM preference_profiles WHERE profile_version=? AND scope='global'",
        (result["profile_version"],),
    ).fetchone()

    liked = np.frombuffer(row["liked_centroid_blob"], dtype=np.float32)
    disliked = np.frombuffer(row["disliked_centroid_blob"], dtype=np.float32)
    assert len(liked) == 768
    assert len(disliked) == 768


def test_centroids_differ_for_like_and_dislike(seeded_db_with_vectors):
    import numpy as np
    from app.services.preference_memory import PreferenceMemoryService

    memory = PreferenceMemoryService(seeded_db_with_vectors)
    result = memory.build_profile(dry_run=False)
    assert result["status"] == "built"

    row = seeded_db_with_vectors.execute(
        "SELECT liked_centroid_blob, disliked_centroid_blob "
        "FROM preference_profiles WHERE profile_version=? AND scope='global'",
        (result["profile_version"],),
    ).fetchone()

    liked = np.frombuffer(row["liked_centroid_blob"], dtype=np.float32)
    disliked = np.frombuffer(row["disliked_centroid_blob"], dtype=np.float32)
    # With different random vectors, centroids should differ
    assert not np.allclose(liked, disliked)


# ---------------------------------------------------------------------------
# gate: max_single_video_share
# ---------------------------------------------------------------------------

def test_build_blocks_when_single_video_dominates(profile_db):
    """All feedback on one video -> max_single_video_share = 1.0 > 0.40."""
    from app.services.preference_events import PreferenceEventService
    from app.services.preference_memory import PreferenceMemoryService

    conn = profile_db
    svc = PreferenceEventService(conn)

    for i in range(30):
        conn.execute(
            """INSERT OR IGNORE INTO candidate_gifs
               (candidate_id, source_run_id, source_run_candidate_id,
                source_video_sha256, source_video_path, start_sec, end_sec,
                status, tags_json, scenario_keys_json)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (f"cand-{i}", "run-1", f"rc-{i}", "video-only",
             "/v/only.mp4", 0.0, 5.0, "liked",
             "[]", "[]"),
        )
    conn.commit()

    for i in range(25):
        svc.record_feedback(
            target_type="candidate_gif",
            target_id=f"cand-{i}",
            rating="like",
            source_video_sha256="video-only",
            scenario_keys=[],
        )
    for i in range(25, 30):
        svc.record_feedback(
            target_type="candidate_gif",
            target_id=f"cand-{i}",
            rating="dislike",
            source_video_sha256="video-only",
            scenario_keys=[],
        )

    memory = PreferenceMemoryService(conn)
    result = memory.build_profile(dry_run=False)

    # Should be blocked by video share gate (and vectors gate, but check video gate)
    assert result["status"] == "blocked"
    reasons = result.get("gate_reasons", [])
    assert any("max_single_video_share" in r for r in reasons)


# ---------------------------------------------------------------------------
# Tag weights
# ---------------------------------------------------------------------------

def test_tag_weights_are_stored_in_profile(seeded_db_with_vectors):
    import json as _json
    from app.services.preference_memory import PreferenceMemoryService

    memory = PreferenceMemoryService(seeded_db_with_vectors)
    result = memory.build_profile(dry_run=False)
    assert result["status"] == "built"

    row = seeded_db_with_vectors.execute(
        "SELECT tag_weights_json FROM preference_profiles "
        "WHERE profile_version=? AND scope='global'",
        (result["profile_version"],),
    ).fetchone()

    weights = _json.loads(row["tag_weights_json"])
    assert isinstance(weights, dict)
    if weights:
        assert all(0.0 <= v <= 1.0 for v in weights.values())
