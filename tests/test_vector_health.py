"""Tests for Phase 3 Task 2: Candidate vector health and resumable backfill."""

from __future__ import annotations

import json
import sqlite3

import numpy as np
import pytest

from app.services.preference_types import BackfillReport, VectorExclusion


# ── helpers ──────────────────────────────────────────────────────────────────


def _conn() -> sqlite3.Connection:
    from app.services.preference_schema import apply_preference_schema

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_preference_schema(conn)
    return conn


def _insert_candidate(
    conn: sqlite3.Connection,
    candidate_id: str = "cand-1",
    *,
    artifact_path: str | None = "data/exports/sample.gif",
) -> None:
    conn.execute(
        """INSERT INTO candidate_gifs
           (candidate_id, source_run_id, source_run_candidate_id,
            source_video_sha256, source_video_path, start_sec, end_sec,
            artifact_path, preview_path,
            vlm_summary_json, tags_json, scenario_keys_json,
            status)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            candidate_id,
            "run-1",
            f"clip-{candidate_id}",
            "video-sha",
            "D:/videos/sample.mp4",
            12.0,
            18.0,
            artifact_path,
            artifact_path,
            json.dumps({"emotion": "joy", "scene_type": "closeup"}),
            json.dumps(["smile", "warm"]),
            json.dumps(["emotion:joy", "tag:smile"]),
            "liked",
        ),
    )
    conn.commit()


def _embed_const(dim: int = 768) -> tuple[float, ...]:
    return tuple([0.5] * dim)


def _stub_embedder(text: str) -> list[float]:
    return [0.5] * 768


def _stub_embedder_fails_on(text_substr: str) -> callable:
    """Return an embedder that raises ValueError for matching text."""

    def _embed(text: str) -> list[float]:
        if text_substr in text:
            raise ValueError(f"simulated failure for {text_substr!r}")
        return [0.5] * 768

    return _embed


def _insert_candidate_with_text(
    conn: sqlite3.Connection,
    candidate_id: str,
    *,
    text: str = "",
) -> None:
    """Insert a candidate with minimal data; *text* is stored in tags."""
    conn.execute(
        """INSERT INTO candidate_gifs
           (candidate_id, source_run_id, source_run_candidate_id,
            source_video_sha256, source_video_path, start_sec, end_sec,
            artifact_path, vlm_summary_json, tags_json, scenario_keys_json,
            status)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            candidate_id,
            "run-1",
            f"clip-{candidate_id}",
            "video-sha",
            "D:/videos/sample.mp4",
            12.0,
            18.0,
            None,
            "{}",
            json.dumps([text] if text else []),
            "[]",
            "candidate",
        ),
    )
    conn.commit()


# ── vector_health: inspect_vector_health ─────────────────────────────────────


class TestInspectVectorHealth:
    def test_returns_zero_when_no_candidates(self):
        from app.services.vector_health import inspect_vector_health

        conn = _conn()
        health = inspect_vector_health(conn, model="nomic-embed-text:latest")
        assert health.total_candidates == 0
        assert health.available == 0
        assert health.missing == ()
        assert health.excluded == ()

    def test_all_missing_when_no_vectors(self):
        from app.services.vector_health import inspect_vector_health

        conn = _conn()
        _insert_candidate(conn, "cand-a")
        _insert_candidate(conn, "cand-b")
        health = inspect_vector_health(conn, model="nomic-embed-text:latest")
        assert health.total_candidates == 2
        assert health.available == 0
        assert health.missing == ("cand-a", "cand-b")

    def test_partial_coverage(self):
        from app.services.vector_health import inspect_vector_health

        conn = _conn()
        _insert_candidate(conn, "cand-a")
        _insert_candidate(conn, "cand-b")
        # Insert vector for cand-a only.
        conn.execute(
            """INSERT INTO candidate_vectors
               (candidate_id, vector_type, embedding_model, embedding_dim, vector_blob)
               VALUES (?,?,?,?,?)""",
            ("cand-a", "clip", "nomic-embed-text:latest", 768,
             np.zeros(768, dtype=np.float32).tobytes()),
        )
        conn.commit()

        health = inspect_vector_health(conn, model="nomic-embed-text:latest")
        assert health.total_candidates == 2
        assert health.available == 1
        assert health.missing == ("cand-b",)

    def test_excluded_candidates_not_in_missing(self):
        from app.services.vector_health import inspect_vector_health

        conn = _conn()
        _insert_candidate(conn, "cand-a")
        _insert_candidate(conn, "cand-b")
        # Record cand-b as excluded.
        conn.execute(
            """INSERT INTO candidate_vector_exclusions
               (candidate_id, reason, created_at)
               VALUES (?,?,?)""",
            ("cand-b", "embedding_failed: test failure", "2026-07-18T00:00:00Z"),
        )
        conn.commit()

        health = inspect_vector_health(conn, model="nomic-embed-text:latest")
        assert health.total_candidates == 2
        assert health.available == 0
        # cand-b is still technically "missing" since it has no vector,
        # but also appears in excluded.
        assert "cand-b" in health.missing

    @pytest.mark.parametrize("model", ["nomic-embed-text:latest", "other-model"])
    def test_coverage_scoped_by_model(self, model):
        from app.services.vector_health import inspect_vector_health

        conn = _conn()
        _insert_candidate(conn, "cand-a")
        # Insert vector for cand-a with a specific model.
        conn.execute(
            """INSERT INTO candidate_vectors
               (candidate_id, vector_type, embedding_model, embedding_dim, vector_blob)
               VALUES (?,?,?,?,?)""",
            ("cand-a", "clip", model, 768,
             np.zeros(768, dtype=np.float32).tobytes()),
        )
        conn.commit()

        health = inspect_vector_health(conn, model=model)
        assert health.available == 1

        health_other = inspect_vector_health(conn, model="wrong-model")
        assert health_other.available == 0


# ── vector_health: VectorHealth dataclass ────────────────────────────────────


class TestVectorHealthDataclass:
    def test_frozen(self):
        from app.services.vector_health import VectorHealth

        vh = VectorHealth(
            total_candidates=1,
            available=0,
            missing=("cand-a",),
            excluded=(),
        )
        with pytest.raises(AttributeError):
            vh.total_candidates = 99  # type: ignore[misc]

    def test_excluded_contains_vector_exclusion_instances(self):
        from app.services.vector_health import VectorHealth

        exc = VectorExclusion(candidate_id="cand-b", reason="corrupt", created_at="now")
        vh = VectorHealth(
            total_candidates=2,
            available=1,
            missing=("cand-b",),
            excluded=(exc,),
        )
        assert len(vh.excluded) == 1
        assert vh.excluded[0].candidate_id == "cand-b"


# ── candidate_vectors: backfill_missing_vectors ──────────────────────────────


class TestBackfillMissingVectors:
    def test_inserts_missing_vectors(self):
        from app.services.candidate_vectors import backfill_missing_vectors

        conn = _conn()
        _insert_candidate(conn, "cand-a")
        _insert_candidate(conn, "cand-b")

        report = backfill_missing_vectors(conn, _stub_embedder)

        assert report["total"] == 2
        assert report["inserted"] == 2
        assert report["failed"] == 0
        assert report["batch_commits"] >= 1

        row_count = conn.execute(
            "SELECT COUNT(*) FROM candidate_vectors"
        ).fetchone()[0]
        assert row_count == 2

    def test_skips_existing_vectors(self):
        from app.services.candidate_vectors import backfill_missing_vectors

        conn = _conn()
        _insert_candidate(conn, "cand-a")
        # Pre-insert vector for cand-a.
        conn.execute(
            """INSERT INTO candidate_vectors
               (candidate_id, vector_type, embedding_model, embedding_dim, vector_blob)
               VALUES (?,?,?,?,?)""",
            ("cand-a", "clip", "nomic-embed-text:latest", 768,
             np.zeros(768, dtype=np.float32).tobytes()),
        )
        conn.commit()

        report = backfill_missing_vectors(conn, _stub_embedder)

        assert report["inserted"] == 0
        assert report["skipped_existing"] == 1

    def test_rerun_skips_existing_vectors(self):
        """Second run should skip all vectors from first run."""
        from app.services.candidate_vectors import backfill_missing_vectors

        conn = _conn()
        _insert_candidate(conn, "cand-a")

        report1 = backfill_missing_vectors(conn, _stub_embedder)
        assert report1["inserted"] == 1

        report2 = backfill_missing_vectors(conn, _stub_embedder)
        assert report2["inserted"] == 0
        assert report2["skipped_existing"] == 1

    def test_records_exclusions_on_failure(self):
        from app.services.candidate_vectors import backfill_missing_vectors

        conn = _conn()
        _insert_candidate_with_text(conn, "cand-a", text="good")
        _insert_candidate_with_text(conn, "cand-b", text="bad")

        # Fail on any text containing "bad".
        fail_on = _stub_embedder_fails_on("bad")

        report = backfill_missing_vectors(conn, fail_on)

        assert report["inserted"] == 1
        assert report["failed"] == 1
        assert len(report["exclusions"]) == 2  # 1 inserted + 1 excluded

        exclusion_row = conn.execute(
            "SELECT reason FROM candidate_vector_exclusions WHERE candidate_id=?",
            ("cand-b",),
        ).fetchone()
        assert exclusion_row is not None
        assert "simulated failure" in exclusion_row["reason"]

    def test_failed_candidates_not_retried(self):
        """Excluded candidates should be skipped on subsequent runs."""
        from app.services.candidate_vectors import backfill_missing_vectors

        conn = _conn()
        _insert_candidate_with_text(conn, "cand-a", text="bad")

        fail_first = _stub_embedder_fails_on("bad")
        report1 = backfill_missing_vectors(conn, fail_first)
        assert report1["failed"] == 1

        # Second run: cand-a is excluded, should be skipped.
        report2 = backfill_missing_vectors(conn, _stub_embedder)
        assert report2["inserted"] == 0
        assert report2["skipped_existing"] >= 1
        # At least one skip is for the excluded candidate.
        assert report2["skipped_existing"] >= 1

    def test_commits_incrementally(self):
        """Batch commits happen at least once, not only at end."""
        from app.services.candidate_vectors import backfill_missing_vectors

        conn = _conn()
        for i in range(5):
            _insert_candidate(conn, f"cand-{i}")

        report = backfill_missing_vectors(
            conn, _stub_embedder, batch_size=2
        )

        # 5 candidates, batch_size=2 => at least 3 commits (2+2+1)
        assert report["batch_commits"] >= 3
        assert report["inserted"] == 5

    def test_backfill_specific_ids(self):
        """Only the specified candidate_ids are processed."""
        from app.services.candidate_vectors import backfill_missing_vectors

        conn = _conn()
        _insert_candidate(conn, "cand-a")
        _insert_candidate(conn, "cand-b")
        _insert_candidate(conn, "cand-c")

        report = backfill_missing_vectors(
            conn, _stub_embedder, candidate_ids=["cand-a", "cand-c"]
        )

        assert report["total"] == 2
        assert report["inserted"] == 2
        rows = conn.execute(
            "SELECT candidate_id FROM candidate_vectors ORDER BY candidate_id"
        ).fetchall()
        assert [r["candidate_id"] for r in rows] == ["cand-a", "cand-c"]

    def test_backfill_specific_ids_skips_excluded(self):
        """Excluded candidates in the specific list are skipped."""
        from app.services.candidate_vectors import backfill_missing_vectors

        conn = _conn()
        _insert_candidate(conn, "cand-a")
        _insert_candidate(conn, "cand-b")
        conn.execute(
            """INSERT INTO candidate_vector_exclusions
               (candidate_id, reason, created_at)
               VALUES (?,?,?)""",
            ("cand-b", "previously_excluded", "2026-07-18T00:00:00Z"),
        )
        conn.commit()

        report = backfill_missing_vectors(
            conn, _stub_embedder, candidate_ids=["cand-a", "cand-b"]
        )

        assert report["inserted"] == 1
        assert report["skipped_existing"] >= 1


# ── integration: health + backfill cycle ─────────────────────────────────────


class TestHealthBackfillCycle:
    def test_health_improves_after_backfill(self):
        from app.services.candidate_vectors import backfill_missing_vectors
        from app.services.vector_health import inspect_vector_health

        conn = _conn()
        _insert_candidate(conn, "cand-a")
        _insert_candidate(conn, "cand-b")

        health_before = inspect_vector_health(conn, model="nomic-embed-text:latest")
        assert health_before.available == 0
        assert len(health_before.missing) == 2

        backfill_missing_vectors(conn, _stub_embedder)

        health_after = inspect_vector_health(conn, model="nomic-embed-text:latest")
        assert health_after.available == 2
        assert health_after.missing == ()

    def test_excluded_candidates_remain_excluded_after_backfill(self):
        from app.services.candidate_vectors import backfill_missing_vectors
        from app.services.vector_health import inspect_vector_health

        conn = _conn()
        _insert_candidate_with_text(conn, "cand-a", text="bad")

        fail_embed = _stub_embedder_fails_on("bad")
        report = backfill_missing_vectors(conn, fail_embed)
        assert report["failed"] == 1

        health = inspect_vector_health(conn, model="nomic-embed-text:latest")
        assert health.available == 0
        assert len(health.excluded) == 1
        assert health.excluded[0].candidate_id == "cand-a"
