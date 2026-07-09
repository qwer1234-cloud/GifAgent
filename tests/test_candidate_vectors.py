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
