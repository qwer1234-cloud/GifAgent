import json
import sqlite3

import numpy as np


def _conn() -> sqlite3.Connection:
    from app.services.preference_schema import apply_preference_schema

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_preference_schema(conn)
    return conn


def _insert_candidate(conn: sqlite3.Connection, candidate_id: str = "cand-1") -> None:
    _insert_candidates(conn, [candidate_id])


def _insert_candidates(
    conn: sqlite3.Connection, candidate_ids: list[str]
) -> list[str]:
    for candidate_id in candidate_ids:
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
                "data/exports/sample@@@001_12s-18s.gif",
                "data/exports/sample@@@001_12s-18s.gif",
                json.dumps({"emotion": "joy", "scene_type": "closeup"}),
                json.dumps(["smile", "warm"]),
                json.dumps(["emotion:joy", "tag:smile"]),
                "liked",
            ),
        )
    conn.commit()
    return candidate_ids


class _TrackingConn:
    """sqlite3 wrapper that counts commit/rollback calls."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self.commits = 0
        self.rollbacks = 0

    def execute(self, *args, **kwargs):
        return self._conn.execute(*args, **kwargs)

    def commit(self):
        self.commits += 1
        self._conn.commit()

    def rollback(self):
        self.rollbacks += 1
        self._conn.rollback()

    def close(self):
        self._conn.close()


def _embed_batch_like(vectors_per_text=768):
    def embed(texts):
        return [[0.1] * vectors_per_text for _ in texts]

    return embed


def test_batch_backfill_65_rows_uses_three_batch_calls_and_commits():
    from app.services.candidate_vectors import backfill_candidate_vectors

    raw = _conn()
    _insert_candidates(raw, [f"cand-{i:03d}" for i in range(1, 66)])
    conn = _TrackingConn(raw)

    call_sizes = []

    def embed(texts):
        call_sizes.append(len(texts))
        return [[0.1] * 768 for _ in texts]

    result = backfill_candidate_vectors(
        conn, batch_embed_fn=embed, batch_size=32
    )

    assert call_sizes == [32, 32, 1]
    assert conn.commits == 3
    assert conn.rollbacks == 0
    assert result["inserted"] == 65
    assert result["missing"] == 65
    assert result["batches"] == 3
    assert result["aborted"] is False
    assert result["remaining"] == 0
    assert (
        raw.execute("SELECT COUNT(*) FROM candidate_vectors").fetchone()[0]
        == 65
    )


def test_batch_backfill_aborts_on_second_batch_and_keeps_first_committed():
    from app.services.candidate_vectors import backfill_candidate_vectors

    raw = _conn()
    _insert_candidates(raw, [f"cand-{i:03d}" for i in range(1, 66)])
    conn = _TrackingConn(raw)

    call_sizes = []

    def embed(texts):
        call_sizes.append(len(texts))
        if len(call_sizes) == 2:
            raise RuntimeError("ollama unavailable")
        return [[0.1] * 768 for _ in texts]

    result = backfill_candidate_vectors(
        conn, batch_embed_fn=embed, batch_size=32
    )

    assert call_sizes == [32, 32]
    assert conn.commits == 1
    assert conn.rollbacks == 1
    assert result["aborted"] is True
    assert "ollama unavailable" in result["error"]
    assert result["inserted"] == 32
    assert result["missing"] == 65
    assert result["remaining"] == 33
    inserted_ids = {
        row["candidate_id"]
        for row in raw.execute(
            "SELECT candidate_id FROM candidate_vectors"
        ).fetchall()
    }
    assert inserted_ids == {f"cand-{i:03d}" for i in range(1, 33)}


def test_batch_backfill_reports_progress_from_zero_to_total():
    from app.services.candidate_vectors import backfill_candidate_vectors

    raw = _conn()
    _insert_candidates(raw, [f"cand-{i:03d}" for i in range(1, 66)])
    conn = _TrackingConn(raw)

    events = []

    def progress(completed, total):
        events.append((completed, total))

    result = backfill_candidate_vectors(
        conn,
        batch_embed_fn=_embed_batch_like(),
        batch_size=32,
        progress_cb=progress,
    )

    assert events == [(0, 65), (32, 65), (64, 65), (65, 65)]
    assert result["inserted"] == 65
    assert result["batches"] == 3
    assert conn.commits == 3


def test_batch_backfill_resume_skips_existing_vectors():
    from app.services.candidate_vectors import backfill_candidate_vectors

    raw = _conn()
    ids = [f"cand-{i:03d}" for i in range(1, 71)]
    _insert_candidates(raw, ids)
    for candidate_id in ids[:5]:
        raw.execute(
            """INSERT INTO candidate_vectors
               (candidate_id, vector_type, embedding_model, embedding_dim,
                vector_blob)
               VALUES (?,?,?,?,?)""",
            (
                candidate_id,
                "clip",
                "nomic-embed-text:latest",
                768,
                np.zeros(768, dtype=np.float32).tobytes(),
            ),
        )
    raw.commit()
    conn = _TrackingConn(raw)

    call_sizes = []

    def embed(texts):
        call_sizes.append(len(texts))
        return [[0.1] * 768 for _ in texts]

    result = backfill_candidate_vectors(
        conn, batch_embed_fn=embed, batch_size=32
    )

    assert result["skipped_existing"] == 5
    assert result["missing"] == 65
    assert result["inserted"] == 65
    assert call_sizes == [32, 32, 1]
    assert (
        raw.execute("SELECT COUNT(*) FROM candidate_vectors").fetchone()[0]
        == 70
    )

def test_backfill_candidate_vectors_inserts_missing_vector():
    from app.services.candidate_vectors import backfill_candidate_vectors

    conn = _conn()
    _insert_candidate(conn)
    seen_texts = []

    def embed(text: str):
        seen_texts.append(text)
        return [0.5] * 768

    result = backfill_candidate_vectors(conn, embed_fn=embed)

    assert result["inserted"] == 1
    assert result["missing"] == 1
    assert "joy" in seen_texts[0]
    assert "sample@@@001_12s-18s.gif" in seen_texts[0]

    row = conn.execute(
        "SELECT vector_type, embedding_model, embedding_dim, vector_blob "
        "FROM candidate_vectors WHERE candidate_id='cand-1'"
    ).fetchone()
    assert row["vector_type"] == "clip"
    assert row["embedding_model"] == "nomic-embed-text:latest"
    assert row["embedding_dim"] == 768
    vec = np.frombuffer(row["vector_blob"], dtype=np.float32)
    assert vec.shape == (768,)
    assert float(vec[0]) == 0.5


def test_backfill_candidate_vectors_skips_existing_vector():
    from app.services.candidate_vectors import backfill_candidate_vectors

    conn = _conn()
    _insert_candidate(conn)
    conn.execute(
        """INSERT INTO candidate_vectors
           (candidate_id, vector_type, embedding_model, embedding_dim, vector_blob)
           VALUES (?,?,?,?,?)""",
        ("cand-1", "clip", "nomic-embed-text:latest", 768, np.zeros(768, dtype=np.float32).tobytes()),
    )
    conn.commit()

    result = backfill_candidate_vectors(conn, embed_fn=lambda text: (_ for _ in ()).throw(AssertionError()))

    assert result["inserted"] == 0
    assert result["skipped_existing"] == 1


def test_backfill_candidate_vectors_dry_run_counts_without_embedding():
    from app.services.candidate_vectors import backfill_candidate_vectors

    conn = _conn()
    _insert_candidate(conn)

    result = backfill_candidate_vectors(
        conn,
        embed_fn=lambda text: (_ for _ in ()).throw(AssertionError()),
        dry_run=True,
    )

    assert result["missing"] == 1
    assert result["inserted"] == 0
    assert conn.execute("SELECT COUNT(*) FROM candidate_vectors").fetchone()[0] == 0


def test_backfill_candidate_vectors_can_scope_to_feedback_targets():
    from app.services.candidate_vectors import backfill_candidate_vectors

    conn = _conn()
    _insert_candidate(conn, "cand-liked")
    _insert_candidate(conn, "cand-unrated")
    conn.execute(
        """INSERT INTO preference_events
           (event_id, target_type, target_id, rating, source_video_sha256)
           VALUES (?,?,?,?,?)""",
        ("event-1", "candidate_gif", "cand-liked", "like", "video-sha"),
    )
    conn.commit()

    result = backfill_candidate_vectors(
        conn,
        embed_fn=lambda text: [0.25] * 768,
        only_feedback=True,
    )

    assert result["inserted"] == 1
    rows = conn.execute("SELECT candidate_id FROM candidate_vectors").fetchall()
    assert [row["candidate_id"] for row in rows] == ["cand-liked"]


def test_batch_backfill_structured_retry_exhaustion_commits_nothing_and_no_exclusions():
    from app.services.candidate_vectors import backfill_candidate_vectors
    from app.services.ollama_runtime import EmbeddingRuntimeError

    raw = _conn()
    _insert_candidates(raw, [f"cand-{i:03d}" for i in range(1, 33)])
    conn = _TrackingConn(raw)

    def embed(texts):
        raise EmbeddingRuntimeError(
            "Ollama embedding request failed after 3 attempts: unreachable",
            phase="embed",
            attempts=3,
            base_url="http://172.27.227.98:11434",
            retryable=True,
            cause=RuntimeError("unreachable"),
        )

    result = backfill_candidate_vectors(conn, batch_embed_fn=embed, batch_size=32)

    assert conn.commits == 0
    assert conn.rollbacks == 1
    assert result["aborted"] is True
    assert result["inserted"] == 0
    assert result["batches"] == 0
    assert result["phase"] == "embed"
    assert result["attempts"] == 3
    assert result["base_url"] == "http://172.27.227.98:11434"
    assert result["retryable"] is True
    assert "unreachable" in result["error"]
    assert len(result["errors"]) == 1
    entry = result["errors"][0]
    assert entry["phase"] == "embed"
    assert entry["retryable"] is True
    assert entry["first_candidate_id"] == "cand-001"
    assert (
        raw.execute("SELECT COUNT(*) FROM candidate_vector_exclusions")
        .fetchone()[0]
        == 0
    )
    assert raw.execute("SELECT COUNT(*) FROM candidate_vectors").fetchone()[0] == 0


def test_batch_backfill_durable_then_exhaustion_resumes():
    from app.services.candidate_vectors import backfill_candidate_vectors
    from app.services.ollama_runtime import EmbeddingRuntimeError

    raw = _conn()
    ids = [f"cand-{i:03d}" for i in range(1, 66)]
    _insert_candidates(raw, ids)
    conn = _TrackingConn(raw)
    call_sizes = []

    def embed(texts):
        call_sizes.append(len(texts))
        if len(call_sizes) == 2:
            raise EmbeddingRuntimeError(
                "Ollama embedding request failed after 3 attempts: unreachable",
                phase="embed",
                attempts=3,
                base_url="http://172.27.227.98:11434",
                retryable=True,
                cause=RuntimeError("unreachable"),
            )
        return [[0.1] * 768 for _ in texts]

    first = backfill_candidate_vectors(conn, batch_embed_fn=embed, batch_size=32)

    assert call_sizes == [32, 32]
    assert conn.commits == 1
    assert conn.rollbacks == 1
    assert first["inserted"] == 32
    assert first["aborted"] is True
    assert first["batches"] == 1
    assert first["phase"] == "embed"
    assert first["retryable"] is True

    conn2 = _TrackingConn(raw)
    second_calls = []

    def embed_ok(texts):
        second_calls.append(len(texts))
        return [[0.2] * 768 for _ in texts]

    second = backfill_candidate_vectors(
        conn2, batch_embed_fn=embed_ok, batch_size=32
    )

    assert second["skipped_existing"] == 32
    assert second["missing"] == 33
    assert second["inserted"] == 33
    assert second_calls == [32, 1]
    assert raw.execute("SELECT COUNT(*) FROM candidate_vectors").fetchone()[0] == 65
    assert (
        raw.execute("SELECT COUNT(*) FROM candidate_vector_exclusions")
        .fetchone()[0]
        == 0
    )


def test_legacy_backfill_does_not_exclude_transient_endpoint_failures():
    from app.services.candidate_vectors import backfill_missing_vectors
    from app.services.ollama_runtime import EmbeddingRuntimeError

    raw = _conn()
    _insert_candidates(raw, ["cand-a"])

    def embed(text):
        raise EmbeddingRuntimeError(
            "Ollama embedding request failed after 3 attempts: unreachable",
            phase="embed",
            attempts=3,
            base_url="http://172.27.227.98:11434",
            retryable=True,
            cause=RuntimeError("unreachable"),
        )

    report = backfill_missing_vectors(raw, embed)

    assert report["failed"] == 1
    assert (
        raw.execute("SELECT COUNT(*) FROM candidate_vector_exclusions")
        .fetchone()[0]
        == 0
    )
