"""Tests for candidate vector ingestion on candidate materialization and enrichment."""

import json
import sqlite3
import numpy as np
import pytest

from app.services.preference_schema import apply_preference_schema
from app.services.candidates import CandidateService
from app.services.candidate_vectors import (
    upsert_candidate_vector,
    build_candidate_embedding_text,
    semantic_embedding_text,
    EMBEDDING_TEXT_SCHEMA_VERSION,
)
from app.services.embedding import EmbeddingServiceUnavailable


@pytest.fixture
def clean_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_preference_schema(conn)
    return conn


def mock_embed_fn(text: str) -> list[float]:
    """Deterministic 768-d unit vector from text hash."""
    rng = np.random.default_rng(abs(hash(text)) % (2**32))
    vec = rng.random(768).astype(np.float32)
    norm = np.linalg.norm(vec)
    return (vec / norm).tolist()


def test_materialize_run_candidate_auto_ingests_vector(clean_db):
    svc = CandidateService(clean_db, embed_fn=mock_embed_fn)

    run_cand = {
        "run_candidate_id": "rc_001",
        "source_video_sha256": "video_abc123",
        "source_video_path": "/path/video.mp4",
        "start_sec": 1.0,
        "end_sec": 4.5,
        "artifact_path": "/path/export.gif",
        "preview_path": "/path/preview.mp4",
        "vlm_summary": {
            "caption": "A cute cat jumping on a sofa",
            "emotion": "joy",
            "scene_type": "animal",
        },
        "tags": ["cat", "cute", "sofa"],
        "base_rag_similarity": 0.85,
        "final_score": 0.90,
    }

    mat = svc.materialize_run_candidate("run-2026-01", run_cand)
    assert mat.candidate_id.startswith("cand_")

    # Verify candidate_gifs row
    cg_row = clean_db.execute(
        "SELECT * FROM candidate_gifs WHERE candidate_id=?", (mat.candidate_id,)
    ).fetchone()
    assert cg_row is not None
    assert cg_row["status"] == "candidate"

    # Verify candidate_vectors row
    vec_row = clean_db.execute(
        "SELECT * FROM candidate_vectors WHERE candidate_id=?", (mat.candidate_id,)
    ).fetchone()
    assert vec_row is not None
    assert vec_row["vector_type"] == "clip"
    assert vec_row["embedding_model"] == "nomic-embed-text:latest"
    assert vec_row["embedding_dim"] == 768
    assert vec_row["text_schema_version"] == EMBEDDING_TEXT_SCHEMA_VERSION
    assert vec_row["source_text_hash"] is not None
    assert len(vec_row["vector_blob"]) == 768 * 4  # 768 float32 bytes


def test_materialize_idempotent_no_reembed_if_current(clean_db):
    call_count = 0

    def counting_embed_fn(text: str) -> list[float]:
        nonlocal call_count
        call_count += 1
        return mock_embed_fn(text)

    svc = CandidateService(clean_db, embed_fn=counting_embed_fn)
    run_cand = {
        "run_candidate_id": "rc_002",
        "source_video_sha256": "video_abc123",
        "source_video_path": "/path/video.mp4",
        "start_sec": 1.0,
        "end_sec": 4.5,
        "vlm_summary": {"caption": "Dancing bird", "emotion": "funny"},
        "tags": ["bird"],
    }

    # First materialize
    mat1 = svc.materialize_run_candidate("run-1", run_cand)
    assert call_count == 1

    # Second materialize with same candidate
    mat2 = svc.materialize_run_candidate("run-1", run_cand)
    assert mat1.candidate_id == mat2.candidate_id
    # Should not re-embed since candidate already exists and is current
    assert call_count == 1


def test_materialize_graceful_on_embedding_failure(clean_db):
    def failing_embed_fn(text: str) -> list[float]:
        raise EmbeddingServiceUnavailable("Ollama connection refused")

    svc = CandidateService(clean_db, embed_fn=failing_embed_fn)
    run_cand = {
        "run_candidate_id": "rc_003",
        "source_video_sha256": "video_fail",
        "source_video_path": "/path/video.mp4",
        "start_sec": 2.0,
        "end_sec": 5.0,
        "vlm_summary": {"caption": "A sunset scene"},
        "tags": ["sunset"],
    }

    # Materialization must NOT raise exception
    mat = svc.materialize_run_candidate("run-1", run_cand)
    assert mat.candidate_id is not None

    # candidate_gifs row must exist
    cg_row = clean_db.execute(
        "SELECT * FROM candidate_gifs WHERE candidate_id=?", (mat.candidate_id,)
    ).fetchone()
    assert cg_row is not None

    # candidate_vectors row should not exist, but exclusion recorded
    vec_row = clean_db.execute(
        "SELECT * FROM candidate_vectors WHERE candidate_id=?", (mat.candidate_id,)
    ).fetchone()
    assert vec_row is None

    ex_row = clean_db.execute(
        "SELECT * FROM candidate_vector_exclusions WHERE candidate_id=?", (mat.candidate_id,)
    ).fetchone()
    assert ex_row is not None
    assert "ingest_embed_failed" in ex_row["reason"]


def test_upsert_candidate_vector_standalone(clean_db):
    # Insert a candidate manually with empty summary
    clean_db.execute(
        """INSERT INTO candidate_gifs
           (candidate_id, source_run_id, source_run_candidate_id,
            source_video_sha256, source_video_path, start_sec, end_sec,
            vlm_summary_json, tags_json, scenario_keys_json)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            "cand_manual_001",
            "run-1",
            "rc_001",
            "vid_123",
            "/v/1.mp4",
            0.0,
            3.0,
            json.dumps({"caption": "Smiling person"}),
            json.dumps(["smile"]),
            json.dumps(["emotion:joy"]),
        ),
    )
    clean_db.commit()

    ok = upsert_candidate_vector(
        clean_db, candidate_id="cand_manual_001", embed_fn=mock_embed_fn
    )
    assert ok is True

    vec = clean_db.execute(
        "SELECT * FROM candidate_vectors WHERE candidate_id='cand_manual_001'"
    ).fetchone()
    assert vec is not None
    assert vec["embedding_dim"] == 768


def test_materialize_empty_text_does_not_embed(clean_db):
    calls: list[str] = []

    def tracking_embed(text: str) -> list[float]:
        calls.append(text)
        return mock_embed_fn(text)

    svc = CandidateService(clean_db, embed_fn=tracking_embed)
    mat = svc.materialize_run_candidate(
        "run-empty",
        {
            "run_candidate_id": "rc_empty",
            "source_video_sha256": "video_empty",
            "source_video_path": "/path/video.mp4",
            "start_sec": 0.0,
            "end_sec": 3.0,
            "vlm_summary": {},
            "tags": [],
        },
    )
    assert calls == []
    vec = clean_db.execute(
        "SELECT * FROM candidate_vectors WHERE candidate_id=?", (mat.candidate_id,)
    ).fetchone()
    assert vec is None
    row = clean_db.execute(
        "SELECT vlm_summary_json, tags_json, scenario_keys_json FROM candidate_gifs WHERE candidate_id=?",
        (mat.candidate_id,),
    ).fetchone()
    assert semantic_embedding_text(row) == ""


def test_upsert_reembeds_when_source_text_hash_changes(clean_db):
    calls: list[str] = []

    def tracking_embed(text: str) -> list[float]:
        calls.append(text)
        return mock_embed_fn(text)

    clean_db.execute(
        """INSERT INTO candidate_gifs
           (candidate_id, source_run_id, source_run_candidate_id,
            source_video_sha256, source_video_path, start_sec, end_sec,
            vlm_summary_json, tags_json, scenario_keys_json)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            "cand_hash_001",
            "run-1",
            "rc_001",
            "vid_123",
            "/v/1.mp4",
            0.0,
            3.0,
            json.dumps({"caption": "first caption"}),
            json.dumps(["smile"]),
            json.dumps([]),
        ),
    )
    clean_db.commit()
    assert upsert_candidate_vector(
        clean_db, candidate_id="cand_hash_001", embed_fn=tracking_embed
    )
    first_hash = clean_db.execute(
        "SELECT source_text_hash FROM candidate_vectors WHERE candidate_id='cand_hash_001'"
    ).fetchone()["source_text_hash"]
    clean_db.execute(
        "UPDATE candidate_gifs SET vlm_summary_json=? WHERE candidate_id='cand_hash_001'",
        (json.dumps({"caption": "second caption"}),),
    )
    clean_db.commit()
    assert upsert_candidate_vector(
        clean_db, candidate_id="cand_hash_001", embed_fn=tracking_embed
    )
    second_hash = clean_db.execute(
        "SELECT source_text_hash FROM candidate_vectors WHERE candidate_id='cand_hash_001'"
    ).fetchone()["source_text_hash"]
    assert len(calls) == 2
    assert first_hash != second_hash


def test_rematerialize_fills_empty_folder_row_and_embeds(clean_db):
    calls: list[str] = []

    def tracking_embed(text: str) -> list[float]:
        calls.append(text)
        return mock_embed_fn(text)

    svc = CandidateService(clean_db, embed_fn=tracking_embed)
    empty_payload = {
        "run_candidate_id": "rc_fill",
        "source_video_sha256": "video_fill",
        "source_video_path": "/path/video.mp4",
        "start_sec": 1.0,
        "end_sec": 4.0,
        "artifact_path": "/exports/clip.gif",
        "vlm_summary": {},
        "tags": [],
    }
    first = svc.materialize_run_candidate("run-1", empty_payload)
    assert calls == []
    filled = {
        **empty_payload,
        "vlm_summary": {"caption": "a person waving", "emotion": "joy"},
        "tags": ["wave"],
    }
    second = svc.materialize_run_candidate("run-2", filled)
    assert first.candidate_id == second.candidate_id
    assert len(calls) == 1
    row = clean_db.execute(
        "SELECT vlm_summary_json FROM candidate_gifs WHERE candidate_id=?",
        (first.candidate_id,),
    ).fetchone()
    assert "waving" in row["vlm_summary_json"]
    vec = clean_db.execute(
        "SELECT * FROM candidate_vectors WHERE candidate_id=?",
        (first.candidate_id,),
    ).fetchone()
    assert vec is not None


def test_record_feedback_upserts_vector_when_text_exists(clean_db):
    from app.services.preference_events import PreferenceEventService

    calls: list[str] = []

    def tracking_embed(text: str) -> list[float]:
        calls.append(text)
        return mock_embed_fn(text)

    clean_db.execute(
        """INSERT INTO candidate_gifs
           (candidate_id, source_run_id, source_run_candidate_id,
            source_video_sha256, source_video_path, start_sec, end_sec,
            vlm_summary_json, tags_json, scenario_keys_json, status)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "cand_fb_001",
            "run-1",
            "rc_001",
            "vid_fb",
            "/v/1.mp4",
            0.0,
            3.0,
            json.dumps({"caption": "smiling closeup"}),
            json.dumps(["smile"]),
            json.dumps(["tag:smile"]),
            "candidate",
        ),
    )
    clean_db.commit()
    svc = PreferenceEventService(clean_db, embed_fn=tracking_embed)
    event = svc.record_feedback(
        target_type="candidate_gif",
        target_id="cand_fb_001",
        rating="like",
        source_video_sha256="vid_fb",
        scenario_keys=["tag:smile"],
    )
    assert event.event_id
    assert len(calls) == 1
    vec = clean_db.execute(
        "SELECT * FROM candidate_vectors WHERE candidate_id='cand_fb_001'"
    ).fetchone()
    assert vec is not None


def test_record_feedback_empty_text_does_not_embed_and_keeps_event(clean_db):
    from app.services.preference_events import PreferenceEventService

    calls: list[str] = []

    def tracking_embed(text: str) -> list[float]:
        calls.append(text)
        return mock_embed_fn(text)

    clean_db.execute(
        """INSERT INTO candidate_gifs
           (candidate_id, source_run_id, source_run_candidate_id,
            source_video_sha256, source_video_path, start_sec, end_sec,
            vlm_summary_json, tags_json, scenario_keys_json, status)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "cand_fb_empty",
            "run-1",
            "rc_001",
            "vid_fb",
            "/v/1.mp4",
            0.0,
            3.0,
            "{}",
            "[]",
            "[]",
            "candidate",
        ),
    )
    clean_db.commit()
    svc = PreferenceEventService(clean_db, embed_fn=tracking_embed)
    svc.record_feedback(
        target_type="candidate_gif",
        target_id="cand_fb_empty",
        rating="like",
        source_video_sha256="vid_fb",
        scenario_keys=[],
    )
    assert calls == []
    assert clean_db.execute("SELECT COUNT(*) FROM preference_events").fetchone()[0] == 1
    assert (
        clean_db.execute(
            "SELECT COUNT(*) FROM candidate_vectors WHERE candidate_id='cand_fb_empty'"
        ).fetchone()[0]
        == 0
    )


def test_record_feedback_embed_failure_does_not_block_like(clean_db):
    from app.services.preference_events import PreferenceEventService

    def failing_embed(text: str) -> list[float]:
        raise EmbeddingServiceUnavailable("Ollama down")

    clean_db.execute(
        """INSERT INTO candidate_gifs
           (candidate_id, source_run_id, source_run_candidate_id,
            source_video_sha256, source_video_path, start_sec, end_sec,
            vlm_summary_json, tags_json, scenario_keys_json, status)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "cand_fb_fail",
            "run-1",
            "rc_001",
            "vid_fb",
            "/v/1.mp4",
            0.0,
            3.0,
            json.dumps({"caption": "a sunset"}),
            json.dumps([]),
            json.dumps([]),
            "candidate",
        ),
    )
    clean_db.commit()
    svc = PreferenceEventService(clean_db, embed_fn=failing_embed)
    svc.record_feedback(
        target_type="candidate_gif",
        target_id="cand_fb_fail",
        rating="like",
        source_video_sha256="vid_fb",
        scenario_keys=[],
    )
    status = clean_db.execute(
        "SELECT status FROM candidate_gifs WHERE candidate_id='cand_fb_fail'"
    ).fetchone()["status"]
    assert status == "liked"
    assert clean_db.execute("SELECT COUNT(*) FROM preference_events").fetchone()[0] == 1
