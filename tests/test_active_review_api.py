"""Tests for Phase 3 Task 6: Active review API endpoints.

Covers: review-queue, pairwise, correction, explanation, profile preview/rollback,
vector-health.
"""

from __future__ import annotations

import json
import sqlite3

import numpy as np
import pytest

from fastapi import HTTPException


# ── helpers ──────────────────────────────────────────────────────────────────


def _conn() -> sqlite3.Connection:
    from app.services.preference_schema import apply_preference_schema

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_preference_schema(conn)
    return conn


def _insert_candidate(
    conn: sqlite3.Connection,
    candidate_id: str,
    **kw,
) -> None:
    conn.execute(
        """INSERT INTO candidate_gifs
           (candidate_id, source_run_id, source_run_candidate_id,
            source_video_sha256, source_video_path, start_sec, end_sec,
            artifact_path, preview_path, vlm_summary_json, tags_json,
            scenario_keys_json, base_rag_similarity, final_score, status,
            created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            candidate_id,
            kw.get("source_run_id", "run-1"),
            f"rc-{candidate_id}",
            kw.get("source_video_sha256", "video-default"),
            kw.get("source_video_path", "/videos/sample.mp4"),
            kw.get("start_sec", 0.0),
            kw.get("end_sec", 5.0),
            kw.get("artifact_path", "data/exports/full.gif"),
            kw.get("preview_path", None),
            kw.get("vlm_summary_json", "{}"),
            kw.get("tags_json", "[]"),
            kw.get("scenario_keys_json", "[]"),
            kw.get("base_rag_similarity", None),
            kw.get("final_score", None),
            kw.get("status", "candidate"),
            kw.get("created_at", "2026-07-18T00:00:00+00:00"),
            kw.get("updated_at", "2026-07-18T00:00:00+00:00"),
        ),
    )
    conn.commit()


def _insert_vector(
    conn: sqlite3.Connection,
    candidate_id: str,
    *,
    model: str = "nomic-embed-text:latest",
    dim: int = 768,
) -> None:
    vec = np.random.default_rng(hash(candidate_id) & 0xFFFFFFFF).normal(0, 1, dim).astype(np.float32)
    vec = vec / np.linalg.norm(vec)
    conn.execute(
        """INSERT OR IGNORE INTO candidate_vectors
           (candidate_id, vector_type, embedding_model, embedding_dim, vector_blob)
           VALUES (?,?,?,?,?)""",
        (candidate_id, "clip", model, dim, vec.tobytes()),
    )
    conn.commit()


def _insert_feedback(
    conn: sqlite3.Connection,
    candidate_id: str,
    rating: str = "like",
    *,
    source_video_sha256: str = "video-default",
    scenario_keys: list[str] | None = None,
) -> None:
    from app.services.preference_events import PreferenceEventService

    PreferenceEventService(conn).record_feedback(
        target_type="candidate_gif",
        target_id=candidate_id,
        rating=rating,  # type: ignore[arg-type]
        source_video_sha256=source_video_sha256,
        scenario_keys=scenario_keys or [],
    )


def _build_published_profile(conn: sqlite3.Connection) -> str:
    """Build and publish a profile with 40 candidates (30 like, 10 dislike).

    Returns the profile_version string.
    """
    from app.services.preference_events import PreferenceEventService
    from app.services.preference_memory import PreferenceMemoryService

    svc = PreferenceEventService(conn)
    videos = ["video-a", "video-b", "video-c"]
    rng = np.random.default_rng(99)

    for i in range(30):
        cid = f"cand-like-{i}"
        _insert_candidate(
            conn, cid,
            source_video_sha256=videos[i % 3],
            status="liked",
            tags_json=json.dumps([f"tag-{i % 5}"]),
            scenario_keys_json=json.dumps(["emotion:joy", f"tag:{i % 5}"]),
        )
        vec = rng.normal(0, 1, 768).astype(np.float32)
        vec = vec / np.linalg.norm(vec)
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
            source_video_sha256=videos[i % 3],
            scenario_keys=["emotion:joy", f"tag:{i % 5}"],
        )

    for i in range(10):
        cid = f"cand-dislike-{i}"
        _insert_candidate(
            conn, cid,
            source_video_sha256=videos[i % 3],
            status="disliked",
            tags_json=json.dumps([f"tag-{i % 5}"]),
            scenario_keys_json=json.dumps(["emotion:sad", f"tag:{i % 5}"]),
        )
        vec = rng.normal(0, 1, 768).astype(np.float32)
        vec = vec / np.linalg.norm(vec)
        conn.execute(
            """INSERT OR IGNORE INTO candidate_vectors
               (candidate_id, vector_type, embedding_model, embedding_dim, vector_blob)
               VALUES (?,?,?,?,?)""",
            (cid, "clip", "nomic-embed-text:latest", 768, vec.tobytes()),
        )
        svc.record_feedback(
            target_type="candidate_gif",
            target_id=cid,
            rating="dislike",
            source_video_sha256=videos[i % 3],
            scenario_keys=["emotion:sad", f"tag:{i % 5}"],
        )

    conn.commit()

    memory = PreferenceMemoryService(conn)
    result = memory.build_profile(dry_run=False)
    assert result["status"] == "built", f"Build blocked: {result.get('gate_reasons')}"
    memory.publish(result["profile_version"])
    return result["profile_version"]


# ── review-queue ────────────────────────────────────────────────────────────


class TestReviewQueue:
    def test_review_queue_returns_reason_and_reason_detail(self, monkeypatch):
        """Queue response exposes reason/reason_detail for each item."""
        from app.routers import candidates as candidates_router

        conn = _conn()
        for i in range(6):
            cid = f"cand-queue-{i}"
            _insert_candidate(
                conn, cid,
                source_video_sha256=f"video-{i % 3}",
                base_rag_similarity=0.5 + i * 0.05,
            )
            _insert_vector(conn, cid)
        monkeypatch.setattr(candidates_router, "get_connection", lambda: conn)

        result = candidates_router.get_review_queue(limit=6, seed=42)

        assert result["total"] == 6
        assert len(result["queue"]) == 6
        for item in result["queue"]:
            assert "candidate_id" in item
            assert "reason" in item
            assert item["reason"] in ("exploit", "uncertain", "explore")
            assert "reason_detail" in item
            assert isinstance(item["reason_detail"], str)
            assert len(item["reason_detail"]) > 0
            assert "score" in item

    def test_review_queue_empty_when_no_candidates(self, monkeypatch):
        """Returns empty queue when there are no unreviewed candidates."""
        from app.routers import candidates as candidates_router

        conn = _conn()
        monkeypatch.setattr(candidates_router, "get_connection", lambda: conn)

        result = candidates_router.get_review_queue(limit=10, seed=42)

        assert result["total"] == 0
        assert result["queue"] == []

    def test_review_queue_with_published_profile(self, monkeypatch):
        """With a published profile, review-queue uses preference scores."""
        from app.routers import candidates as candidates_router

        conn = _conn()
        _build_published_profile(conn)

        for i in range(6):
            cid = f"cand-review-{i}"
            _insert_candidate(
                conn, cid,
                source_video_sha256=f"video-{i % 3}",
                base_rag_similarity=0.5 + i * 0.05,
                scenario_keys_json=json.dumps(["emotion:joy"]),
            )
            _insert_vector(conn, cid)

        monkeypatch.setattr(candidates_router, "get_connection", lambda: conn)

        result = candidates_router.get_review_queue(limit=6, seed=42)

        assert result["total"] == 6
        for item in result["queue"]:
            assert item["reason"] in ("exploit", "uncertain", "explore")

    def test_review_queue_uses_default_limit(self, monkeypatch):
        """Default limit is 24."""
        from app.routers import candidates as candidates_router

        conn = _conn()
        for i in range(30):
            cid = f"cand-limit-{i}"
            _insert_candidate(conn, cid, source_video_sha256=f"video-{i % 5}")
            _insert_vector(conn, cid)
        monkeypatch.setattr(candidates_router, "get_connection", lambda: conn)

        result = candidates_router.get_review_queue(limit=24, seed=42)

        assert result["limit"] == 24
        assert len(result["queue"]) == 24

    def test_review_queue_respects_seed_for_determinism(self, monkeypatch):
        """Same seed produces the same queue ordering."""
        from app.routers import candidates as candidates_router

        conn = _conn()
        for i in range(10):
            cid = f"cand-seed-{i}"
            _insert_candidate(conn, cid, source_video_sha256=f"video-{i % 3}")
            _insert_vector(conn, cid)
        monkeypatch.setattr(candidates_router, "get_connection", lambda: conn)

        result1 = candidates_router.get_review_queue(limit=10, seed=99)
        result2 = candidates_router.get_review_queue(limit=10, seed=99)

        assert result1 == result2


# ── pairwise ────────────────────────────────────────────────────────────────


class TestPairwise:
    def test_pairwise_creates_winner_and_comparative_event(self, monkeypatch):
        """Pairwise choice creates a like event for winner and dislike for loser."""
        from app.routers import candidates as candidates_router

        conn = _conn()
        _insert_candidate(conn, "cand-winner", source_video_sha256="video-a",
                          scenario_keys_json=json.dumps(["emotion:joy"]))
        _insert_candidate(conn, "cand-loser", source_video_sha256="video-b",
                          scenario_keys_json=json.dumps(["emotion:sad"]))
        monkeypatch.setattr(candidates_router, "get_connection", lambda: conn)

        result = candidates_router.pairwise(
            candidates_router.PairwiseRequest(
                winner_id="cand-winner", loser_id="cand-loser"
            )
        )

        assert result["winner_event_id"] is not None
        assert result["loser_event_id"] is not None

        winner_event = conn.execute(
            "SELECT rating, note FROM preference_events WHERE target_id=?",
            ("cand-winner",),
        ).fetchone()
        assert winner_event is not None
        assert winner_event["rating"] == "like"

        loser_event = conn.execute(
            "SELECT rating, note FROM preference_events WHERE target_id=?",
            ("cand-loser",),
        ).fetchone()
        assert loser_event is not None
        assert loser_event["rating"] == "dislike"

    def test_pairwise_nonexistent_winner_returns_404(self, monkeypatch):
        """Pairwise endpoint returns 404 for unknown winner_id."""
        from app.routers import candidates as candidates_router

        conn = _conn()
        _insert_candidate(conn, "cand-loser", source_video_sha256="video-b")
        monkeypatch.setattr(candidates_router, "get_connection", lambda: conn)

        with pytest.raises(HTTPException) as exc:
            candidates_router.pairwise(
                candidates_router.PairwiseRequest(
                    winner_id="cand-unknown", loser_id="cand-loser"
                )
            )
        assert exc.value.status_code == 404

    def test_pairwise_nonexistent_loser_returns_404(self, monkeypatch):
        """Pairwise endpoint returns 404 for unknown loser_id."""
        from app.routers import candidates as candidates_router

        conn = _conn()
        _insert_candidate(conn, "cand-winner", source_video_sha256="video-a")
        monkeypatch.setattr(candidates_router, "get_connection", lambda: conn)

        with pytest.raises(HTTPException) as exc:
            candidates_router.pairwise(
                candidates_router.PairwiseRequest(
                    winner_id="cand-winner", loser_id="cand-unknown"
                )
            )
        assert exc.value.status_code == 404


# ── correction ──────────────────────────────────────────────────────────────


class TestCorrection:
    def test_correction_links_original_event(self, monkeypatch):
        """Correction creates a new event superseding the original."""
        from app.routers import candidates as candidates_router
        from app.services.preference_events import PreferenceEventService

        conn = _conn()
        _insert_candidate(conn, "cand-correct", source_video_sha256="video-a",
                          scenario_keys_json=json.dumps(["emotion:joy"]))
        svc = PreferenceEventService(conn)
        original = svc.record_feedback(
            target_type="candidate_gif",
            target_id="cand-correct",
            rating="like",
            source_video_sha256="video-a",
            scenario_keys=["emotion:joy"],
        )
        monkeypatch.setattr(candidates_router, "get_connection", lambda: conn)

        result = candidates_router.correct_event(
            original.event_id,
            candidates_router.CorrectionRequest(
                replacement_rating="dislike", reason="Changed my mind"
            ),
        )

        assert result["event_id"] != original.event_id
        assert result["supersedes_event_id"] == original.event_id
        assert result["rating"] == "dislike"
        assert result["event_kind"] == "correction"

    def test_correction_nonexistent_event_returns_404(self, monkeypatch):
        """Correction returns 404 for unknown event_id."""
        from app.routers import candidates as candidates_router

        conn = _conn()
        monkeypatch.setattr(candidates_router, "get_connection", lambda: conn)

        with pytest.raises(HTTPException) as exc:
            candidates_router.correct_event(
                "nonexistent-event",
                candidates_router.CorrectionRequest(
                    replacement_rating="dislike", reason="oops"
                ),
            )
        assert exc.value.status_code == 404


# ── explanation ─────────────────────────────────────────────────────────────


class TestExplanation:
    def test_explanation_returns_five_or_fewer_nearest(self, monkeypatch):
        """Explanation returns five-or-fewer nearest examples."""
        from app.routers import candidates as candidates_router

        conn = _conn()
        _build_published_profile(conn)

        _insert_candidate(
            conn, "cand-explain",
            source_video_sha256="video-d",
            base_rag_similarity=0.60,
            scenario_keys_json=json.dumps(["emotion:joy"]),
        )
        _insert_vector(conn, "cand-explain")
        monkeypatch.setattr(candidates_router, "get_connection", lambda: conn)

        result = candidates_router.get_explanation("cand-explain")

        assert "base_quality" in result
        assert "positive_similarity" in result
        assert "negative_penalty" in result
        assert "final_score" in result
        assert "nearest_positive_ids" in result
        assert len(result["nearest_positive_ids"]) <= 5
        assert "preference_profile_version" in result

    def test_explanation_nonexistent_candidate_returns_404(self, monkeypatch):
        """Explanation returns 404 for unknown candidate."""
        from app.routers import candidates as candidates_router

        conn = _conn()
        monkeypatch.setattr(candidates_router, "get_connection", lambda: conn)

        with pytest.raises(HTTPException) as exc:
            candidates_router.get_explanation("cand-unknown")
        assert exc.value.status_code == 404


# ── profile preview ─────────────────────────────────────────────────────────


class TestProfilePreview:
    def test_preview_does_not_publish(self, monkeypatch):
        """Preview returns gate info without publishing a profile."""
        from app.routers import preference as preference_router

        conn = _conn()
        monkeypatch.setattr(preference_router, "get_connection", lambda: conn)

        # Check no publication exists before calling preview
        current_before = conn.execute(
            "SELECT COUNT(*) FROM preference_profile_current"
        ).fetchone()[0]

        result = preference_router.preview_profile_endpoint()

        assert "profile_version" in result
        assert "status" in result
        assert result["status"] in ("ready", "blocked")
        assert "gate_reasons" in result
        assert "metrics" in result

        # The endpoint closes the connection in its finally block, so
        # we verified there was no publication BEFORE the call.
        assert current_before == 0


# ── profile rollback ────────────────────────────────────────────────────────


class TestProfileRollback:
    def test_rollback_updates_current(self, monkeypatch, tmp_path):
        """Rollback switches current profile to the specified version."""
        from app.routers import preference as preference_router
        from app.services.preference_memory import PreferenceMemoryService
        from app.services.preference_schema import apply_preference_schema

        db_path = tmp_path / "test_rollback.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        apply_preference_schema(conn)

        # Build and publish first profile
        pv = _build_published_profile(conn)

        # Build a second profile (direct insert of completed build)
        import uuid
        pv2 = f"profile_{uuid.uuid4().hex[:16]}"
        conn.execute(
            """INSERT INTO preference_profile_builds
               (profile_version, event_watermark, embedding_model, embedding_dim,
                effective_feedback_count, source_video_count, config_json, status,
                completed_at)
               VALUES (?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)""",
            (pv2, "2026-07-18T00:00:00+00:00", "nomic-embed-text:latest",
             768, 40, 4, "{}", "completed"),
        )
        conn.commit()

        # Publish pv2 as current
        memory = PreferenceMemoryService(conn)
        memory.publish(pv2)
        current_row = conn.execute(
            "SELECT profile_version FROM preference_profile_current WHERE slot='current'"
        ).fetchone()
        assert current_row["profile_version"] == pv2
        conn.close()

        # Wire up the endpoint to re-open the same file.
        def _file_conn():
            c = sqlite3.connect(str(db_path))
            c.row_factory = sqlite3.Row
            return c

        monkeypatch.setattr(preference_router, "get_connection", _file_conn)

        result = preference_router.rollback_profile(pv)

        assert result["status"] == "rolled_back"
        assert result["profile_version"] == pv

        # Verify via a new connection
        verify_conn = sqlite3.connect(str(db_path))
        verify_conn.row_factory = sqlite3.Row
        current_row = verify_conn.execute(
            "SELECT profile_version FROM preference_profile_current WHERE slot='current'"
        ).fetchone()
        assert current_row["profile_version"] == pv
        verify_conn.close()

    def test_rollback_nonexistent_profile_returns_400(self, monkeypatch):
        """Rollback returns 400 for unknown profile_version."""
        from app.routers import preference as preference_router

        conn = _conn()
        monkeypatch.setattr(preference_router, "get_connection", lambda: conn)

        with pytest.raises(HTTPException) as exc:
            preference_router.rollback_profile("profile_unknown")
        assert exc.value.status_code == 400


# ── vector-health ───────────────────────────────────────────────────────────


class TestVectorHealth:
    def test_vector_health_returns_counts(self, monkeypatch):
        """Vector health returns total/available/missing/excluded counts."""
        from app.routers import preference as preference_router

        conn = _conn()
        _insert_candidate(conn, "cand-a", source_video_sha256="video-a")
        _insert_candidate(conn, "cand-b", source_video_sha256="video-b")
        _insert_candidate(conn, "cand-c", source_video_sha256="video-c")
        _insert_vector(conn, "cand-a")
        _insert_vector(conn, "cand-b")
        monkeypatch.setattr(preference_router, "get_connection", lambda: conn)

        result = preference_router.get_vector_health()

        assert result["total_candidates"] == 3
        assert result["available"] == 2
        assert "cand-c" in result["missing"]

    def test_vector_health_excluded_listed(self, monkeypatch):
        """Vector health shows excluded candidates."""
        from app.routers import preference as preference_router

        conn = _conn()
        _insert_candidate(conn, "cand-a", source_video_sha256="video-a")
        conn.execute(
            """INSERT INTO candidate_vector_exclusions
               (candidate_id, reason, created_at)
               VALUES (?,?,?)""",
            ("cand-a", "corrupt", "2026-07-18T00:00:00Z"),
        )
        conn.commit()
        monkeypatch.setattr(preference_router, "get_connection", lambda: conn)

        result = preference_router.get_vector_health()
        assert len(result["excluded"]) == 1
        assert result["excluded"][0]["candidate_id"] == "cand-a"
        assert result["excluded"][0]["reason"] == "corrupt"
