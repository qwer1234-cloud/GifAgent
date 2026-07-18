"""Phase 3 Task 4: Immutable profiles with recency, weights, scenario controls."""

from __future__ import annotations

import json
import sqlite3

import numpy as np
import pytest

from app.services.preference_types import ProfileBuildConfig, ProfilePreview


# ---------------------------------------------------------------------------
# Shared fixtures  (mirror test_preference_profiles.py for isolation)
# ---------------------------------------------------------------------------


@pytest.fixture
def profile_db():
    from app.services.preference_schema import apply_preference_schema

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_preference_schema(conn)
    return conn


@pytest.fixture
def seeded_db(profile_db):
    """40 candidates + events (30 like, 10 dislike) across 3 videos."""
    from app.services.preference_events import PreferenceEventService

    conn = profile_db
    svc = PreferenceEventService(conn)

    videos = ["video-a", "video-b", "video-c"]
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
    """Add candidate_vectors for each liked/disliked candidate."""
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
# Test: positive and negative centroids stay separate
# ---------------------------------------------------------------------------


def test_v2_centroids_remain_separate(seeded_db_with_vectors):
    """Even with recency and weights, positive/negative centroids differ."""
    from app.services.preference_memory import PreferenceMemoryService

    memory = PreferenceMemoryService(seeded_db_with_vectors)
    config = ProfileBuildConfig(recency_enabled=False, favorite_weight=3.0)
    result = memory.build_profile(dry_run=False, config=config)

    assert result["status"] == "built"

    row = seeded_db_with_vectors.execute(
        """SELECT liked_centroid_blob, disliked_centroid_blob
           FROM preference_profiles
           WHERE profile_version=? AND scope='global'""",
        (result["profile_version"],),
    ).fetchone()
    assert row is not None
    liked = np.frombuffer(row["liked_centroid_blob"], dtype=np.float32)
    disliked = np.frombuffer(row["disliked_centroid_blob"], dtype=np.float32)
    assert not np.allclose(liked, disliked)


# ---------------------------------------------------------------------------
# Test: favorite receives configured weight
# ---------------------------------------------------------------------------


def test_favorite_receives_configured_weight(profile_db, monkeypatch):
    """When favorite_weight=2.0, a favorite contributes twice as much as a
    like to the positive centroid."""
    monkeypatch.setattr(
        "app.services.preference_memory.MIN_EFFECTIVE_FEEDBACK", 3
    )
    monkeypatch.setattr("app.services.preference_memory.MIN_LIKE_COUNT", 1)
    monkeypatch.setattr("app.services.preference_memory.MIN_DISLIKE_COUNT", 0)
    monkeypatch.setattr("app.services.preference_memory.MIN_SOURCE_VIDEOS", 1)
    monkeypatch.setattr(
        "app.services.preference_memory.MAX_SINGLE_VIDEO_SHARE", 1.0
    )

    from app.services.preference_memory import PreferenceMemoryService

    conn = profile_db

    # Insert 3 candidates with known 2-D vectors.
    # cand-a, cand-b:  "like" rating, vector = (1, 0)
    # cand-c:          "favorite" rating, vector = (0, 1)
    candidates = [
        ("cand-a", [1.0, 0.0], "like"),
        ("cand-b", [1.0, 0.0], "like"),
        ("cand-c", [0.0, 1.0], "favorite"),
    ]
    for cid, vec, rating in candidates:
        conn.execute(
            """INSERT INTO candidate_gifs
               (candidate_id, source_run_id, source_run_candidate_id,
                source_video_sha256, source_video_path, start_sec, end_sec,
                status, tags_json, scenario_keys_json)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (cid, "run-1", cid, "vid-1", "/v/1.mp4", 0.0, 5.0, "liked",
             "[]", "[]"),
        )
        vec_bytes = np.array(vec, dtype=np.float32).tobytes()
        conn.execute(
            """INSERT INTO candidate_vectors
               (candidate_id, vector_type, embedding_model, embedding_dim, vector_blob)
               VALUES (?,?,?,?,?)""",
            (cid, "clip", "nomic-embed-text:latest", 2, vec_bytes),
        )
        conn.execute(
            """INSERT INTO preference_events
               (event_id, target_type, target_id, rating,
                source_video_sha256, created_at, event_kind)
               VALUES (?,?,?,?,?,?,?)""",
            (f"evt-{cid}", "candidate_gif", cid, rating,
             "vid-1", "2026-07-18T12:00:00", "feedback"),
        )
    conn.commit()

    # Build with recency disabled (so all weights = 1.0 * rating_weight).
    config = ProfileBuildConfig(
        recency_enabled=False,
        favorite_weight=2.0,
        like_weight=1.0,
        dislike_weight=1.0,
    )
    memory = PreferenceMemoryService(conn)
    result = memory.build_profile(
        dry_run=False, config=config,
        embedding_model="nomic-embed-text:latest", embedding_dim=2,
    )
    assert result["status"] == "built"

    row = conn.execute(
        """SELECT liked_centroid_blob FROM preference_profiles
           WHERE profile_version=? AND scope='global'""",
        (result["profile_version"],),
    ).fetchone()
    assert row is not None

    centroid = np.frombuffer(row["liked_centroid_blob"], dtype=np.float32)
    # Expected: cand-a (1,0) * 1 + cand-b (1,0) * 1 + cand-c (0,1) * 2
    #           = (2, 2) / 4 = (0.5, 0.5)
    assert centroid.shape == (2,)
    assert np.allclose(centroid, [0.5, 0.5], atol=1e-6)


# ---------------------------------------------------------------------------
# Test: recency can be disabled
# ---------------------------------------------------------------------------


def test_recency_can_be_disabled(profile_db, monkeypatch):
    """When recency_enabled=False, equally-aged and unequally-aged events
    both contribute with weight 1.0."""
    monkeypatch.setattr(
        "app.services.preference_memory.MIN_EFFECTIVE_FEEDBACK", 2
    )
    monkeypatch.setattr("app.services.preference_memory.MIN_LIKE_COUNT", 1)
    monkeypatch.setattr("app.services.preference_memory.MIN_DISLIKE_COUNT", 0)
    monkeypatch.setattr("app.services.preference_memory.MIN_SOURCE_VIDEOS", 1)
    monkeypatch.setattr(
        "app.services.preference_memory.MAX_SINGLE_VIDEO_SHARE", 1.0
    )

    from app.services.preference_memory import PreferenceMemoryService

    conn = profile_db

    # Two candidates with orthogonal vectors.
    candidates = [
        ("cand-old", [1.0, 0.0], "2026-01-01T12:00:00"),
        ("cand-new", [0.0, 1.0], "2026-07-18T12:00:00"),
    ]
    for cid, vec, ts in candidates:
        conn.execute(
            """INSERT INTO candidate_gifs
               (candidate_id, source_run_id, source_run_candidate_id,
                source_video_sha256, source_video_path, start_sec, end_sec,
                status, tags_json, scenario_keys_json)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (cid, "run-1", cid, "vid-1", "/v/1.mp4", 0.0, 5.0, "liked",
             "[]", "[]"),
        )
        vec_bytes = np.array(vec, dtype=np.float32).tobytes()
        conn.execute(
            """INSERT INTO candidate_vectors
               (candidate_id, vector_type, embedding_model, embedding_dim, vector_blob)
               VALUES (?,?,?,?,?)""",
            (cid, "clip", "nomic-embed-text:latest", 2, vec_bytes),
        )
        conn.execute(
            """INSERT INTO preference_events
               (event_id, target_type, target_id, rating,
                source_video_sha256, created_at, event_kind)
               VALUES (?,?,?,?,?,?,?)""",
            (f"evt-{cid}", "candidate_gif", cid, "like",
             "vid-1", ts, "feedback"),
        )
    conn.commit()

    # Disable recency — both events get weight 1.0 * 1.0 = 1.0
    config = ProfileBuildConfig(
        recency_enabled=False,
        like_weight=1.0,
    )
    memory = PreferenceMemoryService(conn)
    result = memory.build_profile(
        dry_run=False, config=config,
        embedding_model="nomic-embed-text:latest", embedding_dim=2,
    )
    assert result["status"] == "built"

    row = conn.execute(
        """SELECT liked_centroid_blob FROM preference_profiles
           WHERE profile_version=? AND scope='global'""",
        (result["profile_version"],),
    ).fetchone()
    assert row is not None

    centroid = np.frombuffer(row["liked_centroid_blob"], dtype=np.float32)
    # Equal weight -> simple mean of (1,0) and (0,1) = (0.5, 0.5)
    assert np.allclose(centroid, [0.5, 0.5], atol=1e-6)


# ---------------------------------------------------------------------------
# Test: half-life weights are exact
# ---------------------------------------------------------------------------


def test_half_life_weights_are_exact(profile_db, monkeypatch):
    """With recency enabled, the weight formula 0.5^(age/half_life) is
    applied correctly."""
    monkeypatch.setattr(
        "app.services.preference_memory.MIN_EFFECTIVE_FEEDBACK", 2
    )
    monkeypatch.setattr("app.services.preference_memory.MIN_LIKE_COUNT", 1)
    monkeypatch.setattr("app.services.preference_memory.MIN_DISLIKE_COUNT", 0)
    monkeypatch.setattr("app.services.preference_memory.MIN_SOURCE_VIDEOS", 1)
    monkeypatch.setattr(
        "app.services.preference_memory.MAX_SINGLE_VIDEO_SHARE", 1.0
    )

    from app.services.preference_memory import PreferenceMemoryService

    conn = profile_db

    # cand-recent: age=0 days from watermark (weight = 1.0)
    # cand-old:    age=1 day   from watermark (weight = 0.5^(1/1) = 0.5)
    watermark = "2026-07-18T12:00:00"
    one_day_ago = "2026-07-17T12:00:00"

    candidates = [
        ("cand-recent", [1.0, 0.0], watermark),
        ("cand-old", [0.0, 1.0], one_day_ago),
    ]
    for cid, vec, ts in candidates:
        conn.execute(
            """INSERT INTO candidate_gifs
               (candidate_id, source_run_id, source_run_candidate_id,
                source_video_sha256, source_video_path, start_sec, end_sec,
                status, tags_json, scenario_keys_json)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (cid, "run-1", cid, "vid-1", "/v/1.mp4", 0.0, 5.0, "liked",
             "[]", "[]"),
        )
        vec_bytes = np.array(vec, dtype=np.float32).tobytes()
        conn.execute(
            """INSERT INTO candidate_vectors
               (candidate_id, vector_type, embedding_model, embedding_dim, vector_blob)
               VALUES (?,?,?,?,?)""",
            (cid, "clip", "nomic-embed-text:latest", 2, vec_bytes),
        )
        conn.execute(
            """INSERT INTO preference_events
               (event_id, target_type, target_id, rating,
                source_video_sha256, created_at, event_kind)
               VALUES (?,?,?,?,?,?,?)""",
            (f"evt-{cid}", "candidate_gif", cid, "like",
             "vid-1", ts, "feedback"),
        )
    conn.commit()

    config = ProfileBuildConfig(
        recency_enabled=True,
        recency_half_life_days=1.0,
        like_weight=1.0,
    )
    memory = PreferenceMemoryService(conn)
    result = memory.build_profile(
        dry_run=False, config=config,
        embedding_model="nomic-embed-text:latest", embedding_dim=2,
    )
    assert result["status"] == "built"

    row = conn.execute(
        """SELECT liked_centroid_blob FROM preference_profiles
           WHERE profile_version=? AND scope='global'""",
        (result["profile_version"],),
    ).fetchone()
    assert row is not None

    centroid = np.frombuffer(row["liked_centroid_blob"], dtype=np.float32)
    # Weighted mean: (1.0 * (1,0) + 0.5 * (0,1)) / 1.5 = (0.666..., 0.333...)
    assert np.allclose(centroid, [2.0 / 3.0, 1.0 / 3.0], atol=1e-6)


# ---------------------------------------------------------------------------
# Test: scenario profiles honor minimum feedback
# ---------------------------------------------------------------------------


def test_scenario_min_feedback_honored(profile_db, monkeypatch):
    """scenario_min_feedback=8: only scenarios with 8+ events get profiles."""
    monkeypatch.setattr(
        "app.services.preference_memory.MIN_EFFECTIVE_FEEDBACK", 10
    )
    monkeypatch.setattr(
        "app.services.preference_memory.MIN_LIKE_COUNT", 1
    )
    monkeypatch.setattr(
        "app.services.preference_memory.MIN_DISLIKE_COUNT", 0
    )
    monkeypatch.setattr(
        "app.services.preference_memory.MIN_SOURCE_VIDEOS", 1
    )
    monkeypatch.setattr(
        "app.services.preference_memory.MAX_SINGLE_VIDEO_SHARE", 1.0
    )

    from app.services.preference_events import PreferenceEventService
    from app.services.preference_memory import PreferenceMemoryService

    conn = profile_db
    svc = PreferenceEventService(conn)

    # Insert 10 candidates and events.
    # scenario-key-A:  6 events (below min_feedback=8)
    # scenario-key-B:  4 events (below min_feedback=8)
    # scenario-key-C:  8 events (at threshold = 8)
    # scenario-key-D: 10 events (above min_feedback=8)
    scenarios = {"scn-a": 6, "scn-b": 4, "scn-c": 8, "scn-d": 10}
    idx = 0
    for scn_key, count in scenarios.items():
        for j in range(count):
            cid = f"cand-{scn_key}-{j}"
            conn.execute(
                """INSERT OR IGNORE INTO candidate_gifs
                   (candidate_id, source_run_id, source_run_candidate_id,
                    source_video_sha256, source_video_path, start_sec, end_sec,
                    status, tags_json, scenario_keys_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (cid, "run-1", cid, f"vid-{idx}",
                 f"/v/{idx}.mp4", 0.0, 5.0, "liked", "[]", "[]"),
            )
            vec = np.random.default_rng(idx).random(768).astype(np.float32)
            conn.execute(
                """INSERT OR IGNORE INTO candidate_vectors
                   (candidate_id, vector_type, embedding_model, embedding_dim, vector_blob)
                   VALUES (?,?,?,?,?)""",
                (cid, "clip", "nomic-embed-text:latest", 768, vec.tobytes()),
            )
            svc.record_feedback(
                target_type="candidate_gif",
                target_id=cid,
                rating="like",
                source_video_sha256=f"vid-{idx}",
                scenario_keys=[scn_key],
            )
            idx += 1
    conn.commit()

    config = ProfileBuildConfig(
        recency_enabled=False,
        scenario_min_feedback=8,
    )
    memory = PreferenceMemoryService(conn)
    result = memory.build_profile(dry_run=False, config=config)
    assert result["status"] == "built"

    scenario_rows = conn.execute(
        """SELECT scenario_key, like_count, dislike_count
           FROM preference_profiles
           WHERE profile_version=? AND scope='scenario'
           ORDER BY scenario_key""",
        (result["profile_version"],),
    ).fetchall()

    scenario_keys_found = [r["scenario_key"] for r in scenario_rows]
    # scn-b (4) and scn-a (6) should be excluded; scn-c (8) and scn-d (10) included
    assert "scn-a" not in scenario_keys_found
    assert "scn-b" not in scenario_keys_found
    assert "scn-c" in scenario_keys_found
    assert "scn-d" in scenario_keys_found


# ---------------------------------------------------------------------------
# Test: builds are immutable (deterministic version based on state + config)
# ---------------------------------------------------------------------------


def test_build_is_immutable(seeded_db_with_vectors):
    """Same state + same config => same profile version and identical data.

    This verifies that builds are deterministic and immutable — the system
    never produces different output for the same input."""
    from app.services.preference_memory import PreferenceMemoryService

    config = ProfileBuildConfig()
    memory = PreferenceMemoryService(seeded_db_with_vectors)

    r1 = memory.build_profile(dry_run=False, config=config)
    r2 = memory.build_profile(dry_run=False, config=config)

    assert r1["profile_version"] == r2["profile_version"]
    assert r1["status"] == r2["status"]
    assert r1["effective_feedback_count"] == r2["effective_feedback_count"]


def test_different_config_produces_different_version(seeded_db_with_vectors):
    """Different config => different profile version hash."""
    from app.services.preference_memory import PreferenceMemoryService

    memory = PreferenceMemoryService(seeded_db_with_vectors)

    r1 = memory.build_profile(
        dry_run=False, config=ProfileBuildConfig(favorite_weight=2.0)
    )
    r2 = memory.build_profile(
        dry_run=False, config=ProfileBuildConfig(favorite_weight=3.0)
    )

    assert r1["profile_version"] != r2["profile_version"]


# ---------------------------------------------------------------------------
# Test: preview_profile computes gates and metrics without writing
# ---------------------------------------------------------------------------


def test_preview_profile_returns_preview_without_writing(seeded_db_with_vectors):
    """preview_profile computes gates/metrics, writes nothing to DB."""
    from app.services.preference_memory import PreferenceMemoryService, preview_profile

    memory = PreferenceMemoryService(seeded_db_with_vectors)
    config = ProfileBuildConfig()

    preview = preview_profile(seeded_db_with_vectors, config)

    assert isinstance(preview, ProfilePreview)
    assert preview.status in ("ready", "blocked")
    assert "effective_feedback_count" in preview.metrics
    assert "like_count" in preview.metrics

    # Nothing should have been written to the DB
    build_count = seeded_db_with_vectors.execute(
        "SELECT COUNT(*) FROM preference_profile_builds"
    ).fetchone()[0]
    profile_count = seeded_db_with_vectors.execute(
        "SELECT COUNT(*) FROM preference_profiles"
    ).fetchone()[0]
    assert build_count == 0
    assert profile_count == 0


def test_preview_profile_reflects_config(seeded_db):
    """A config with extreme recency should affect metrics differently."""
    from app.services.preference_memory import PreferenceMemoryService, preview_profile

    memory = PreferenceMemoryService(seeded_db)

    preview = preview_profile(
        seeded_db, ProfileBuildConfig(recency_enabled=False)
    )
    assert preview.profile_version.startswith("profile_")

    # A different config yields a different version string
    preview2 = preview_profile(
        seeded_db, ProfileBuildConfig(recency_enabled=True)
    )
    assert preview.profile_version != preview2.profile_version


# ---------------------------------------------------------------------------
# Test: rollback
# ---------------------------------------------------------------------------


def test_rollback_changes_current_while_preserving_history(
    seeded_db_with_vectors,
):
    """rollback() updates the current slot but preserves all publications."""
    from app.services.preference_memory import PreferenceMemoryService
    from app.services.preference_types import ProfileBuildConfig

    conn = seeded_db_with_vectors
    memory = PreferenceMemoryService(conn)

    # Build + publish version A
    config_a = ProfileBuildConfig(favorite_weight=2.0)
    r_a = memory.build_profile(dry_run=False, config=config_a)
    assert r_a["status"] == "built"
    memory.publish(r_a["profile_version"])

    # Build + publish version B (different config -> different version)
    config_b = ProfileBuildConfig(favorite_weight=3.0)
    r_b = memory.build_profile(dry_run=False, config=config_b)
    assert r_b["status"] == "built"
    assert r_b["profile_version"] != r_a["profile_version"]
    memory.publish(r_b["profile_version"])

    # Verify current is B
    current = conn.execute(
        "SELECT * FROM preference_profile_current WHERE slot='current'"
    ).fetchone()
    assert current["profile_version"] == r_b["profile_version"]

    # Rollback to A
    memory.rollback(r_a["profile_version"])

    # Current points to A again
    current = conn.execute(
        "SELECT * FROM preference_profile_current WHERE slot='current'"
    ).fetchone()
    assert current["profile_version"] == r_a["profile_version"]

    # 3 publications: publish A, publish B, rollback to A
    pubs = conn.execute(
        "SELECT COUNT(*) FROM preference_profile_publications"
    ).fetchone()[0]
    assert pubs == 3

    pub_rows = conn.execute(
        "SELECT profile_version, previous_profile_version "
        "FROM preference_profile_publications ORDER BY publication_id"
    ).fetchall()
    assert pub_rows[0]["profile_version"] == r_a["profile_version"]
    assert pub_rows[0]["previous_profile_version"] is None
    assert pub_rows[1]["profile_version"] == r_b["profile_version"]
    assert pub_rows[1]["previous_profile_version"] == r_a["profile_version"]
    assert pub_rows[2]["profile_version"] == r_a["profile_version"]
    assert pub_rows[2]["previous_profile_version"] == r_b["profile_version"]


def test_rollback_raises_for_nonexistent_version(profile_db):
    """Rollback to a version that doesn't exist raises ValueError."""
    from app.services.preference_memory import PreferenceMemoryService

    memory = PreferenceMemoryService(profile_db)
    with pytest.raises(ValueError, match="not found"):
        memory.rollback("profile_nonexistent")


def test_rollback_raises_for_not_completed(seeded_db):
    """Rollback to a blocked version raises ValueError."""
    from app.services.preference_memory import PreferenceMemoryService

    memory = PreferenceMemoryService(seeded_db)
    result = memory.build_profile(dry_run=False)
    assert result["status"] == "blocked"

    with pytest.raises(ValueError, match="not completed"):
        memory.rollback(result["profile_version"])


# ---------------------------------------------------------------------------
# Test: publish writes to publications table
# ---------------------------------------------------------------------------


def test_publish_writes_publication_row(seeded_db_with_vectors):
    """publish() now writes into preference_profile_publications."""
    from app.services.preference_memory import PreferenceMemoryService

    memory = PreferenceMemoryService(seeded_db_with_vectors)
    result = memory.build_profile(dry_run=False)
    assert result["status"] == "built"

    memory.publish(result["profile_version"])

    pub = seeded_db_with_vectors.execute(
        "SELECT * FROM preference_profile_publications"
    ).fetchone()
    assert pub is not None
    assert pub["profile_version"] == result["profile_version"]
    assert pub["previous_profile_version"] is None
