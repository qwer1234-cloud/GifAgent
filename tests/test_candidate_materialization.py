"""P1-3: Materialize run candidates into long-term candidate_gifs rows."""

import sqlite3


def test_materialize_run_candidate_is_idempotent():
    from app.services.preference_schema import apply_preference_schema
    from app.services.candidates import CandidateService

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_preference_schema(conn)
    service = CandidateService(conn)

    payload = {
        "run_candidate_id": "clip-001",
        "source_video_sha256": "sha256-video",
        "source_video_path": "D:/videos/sample.mp4",
        "start_sec": 12.0,
        "end_sec": 16.5,
        "artifact_path": "data/exports/sample.gif",
        "preview_path": "data/exports/sample.gif",
        "vlm_summary": {"emotion": "joy"},
        "tags": ["smile", "closeup"],
        "base_rag_similarity": 0.71,
        "final_score": 0.71,
    }

    first = service.materialize_run_candidate("run-1", payload)
    second = service.materialize_run_candidate("run-1", payload)

    assert first.candidate_id == second.candidate_id
    assert first.status == "candidate"
    assert conn.execute("SELECT COUNT(*) FROM candidate_gifs").fetchone()[0] == 1


def test_candidate_id_is_deterministic():
    """Same inputs produce the same candidate_id regardless of run_id."""
    from app.services.preference_schema import apply_preference_schema
    from app.services.candidates import CandidateService

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_preference_schema(conn)
    service = CandidateService(conn)

    payload = {
        "run_candidate_id": "clip-002",
        "source_video_sha256": "sha256-video",
        "source_video_path": "D:/videos/sample.mp4",
        "start_sec": 12.0,
        "end_sec": 16.5,
        "artifact_path": "data/exports/sample.gif",
        "preview_path": "data/exports/sample.gif",
        "vlm_summary": {"emotion": "joy"},
        "tags": ["smile"],
        "base_rag_similarity": 0.71,
        "final_score": 0.71,
    }

    first = service.materialize_run_candidate("run-A", payload)
    second = service.materialize_run_candidate("run-B", payload)

    # candidate_id is derived from video+snippet, not from run_id
    assert first.candidate_id == second.candidate_id
    # source_run_id records the actual run that materialized it
    assert first.source_run_id == "run-A"
    assert second.source_run_id == "run-A"  # second call is a no-op / return-existing


def test_different_clips_produce_different_ids():
    from app.services.preference_schema import apply_preference_schema
    from app.services.candidates import CandidateService

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_preference_schema(conn)
    service = CandidateService(conn)

    base = {
        "run_candidate_id": "clip-003",
        "source_video_sha256": "sha256-video",
        "source_video_path": "D:/videos/sample.mp4",
        "artifact_path": "data/exports/sample.gif",
        "preview_path": "data/exports/sample.gif",
        "vlm_summary": {"emotion": "joy"},
        "tags": ["smile"],
        "base_rag_similarity": 0.71,
        "final_score": 0.71,
    }

    a = service.materialize_run_candidate("run-1", {**base, "start_sec": 0.0, "end_sec": 5.0, "run_candidate_id": "clip-a"})
    b = service.materialize_run_candidate("run-1", {**base, "start_sec": 5.0, "end_sec": 10.0, "run_candidate_id": "clip-b"})

    assert a.candidate_id != b.candidate_id
    assert conn.execute("SELECT COUNT(*) FROM candidate_gifs").fetchone()[0] == 2


def test_materialize_stores_scenario_keys():
    import json
    from app.services.preference_schema import apply_preference_schema
    from app.services.candidates import CandidateService

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_preference_schema(conn)
    service = CandidateService(conn)

    payload = {
        "run_candidate_id": "clip-005",
        "source_video_sha256": "sha256-video",
        "source_video_path": "D:/videos/sample.mp4",
        "start_sec": 12.0,
        "end_sec": 16.5,
        "artifact_path": "data/exports/sample.gif",
        "preview_path": "data/exports/sample.gif",
        "vlm_summary": {"emotion": "joy"},
        "tags": ["smile", "closeup"],
        "base_rag_similarity": 0.71,
        "final_score": 0.71,
    }

    candidate = service.materialize_run_candidate("run-1", payload)

    row = conn.execute(
        "SELECT scenario_keys_json, tags_json FROM candidate_gifs WHERE candidate_id=?",
        (candidate.candidate_id,),
    ).fetchone()

    scenario_keys = json.loads(row["scenario_keys_json"])
    tags = json.loads(row["tags_json"])

    assert "emotion:joy" in scenario_keys
    assert "tag:smile" in scenario_keys
    assert "tag:closeup" in scenario_keys
    assert "smile" in tags


def test_scale_like_data_for_candidate_id():
    """Candidate IDs use sha256 and have expected prefix."""
    import hashlib
    from app.services.preference_schema import apply_preference_schema
    from app.services.candidates import CandidateService

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_preference_schema(conn)
    service = CandidateService(conn)

    payload = {
        "run_candidate_id": "clip-scale-1",
        "source_video_sha256": "sha256-video",
        "source_video_path": "D:/videos/sample.mp4",
        "start_sec": 12.0,
        "end_sec": 16.5,
        "artifact_path": "data/exports/sample.gif",
        "preview_path": "data/exports/sample.gif",
        "vlm_summary": {"emotion": "joy"},
        "tags": ["spam", "eggs"],
        "base_rag_similarity": 0.71,
        "final_score": 0.71,
    }

    candidate = service.materialize_run_candidate("run-scale-1", payload)

    assert candidate.candidate_id.startswith("cand_")
    assert len(candidate.candidate_id) == len("cand_") + 64  # sha256 hex
    assert all(c in "0123456789abcdef" for c in candidate.candidate_id[5:])


def test_materialize_with_minimal_payload():
    """Minimal payload with only required fields should still work."""
    from app.services.preference_schema import apply_preference_schema
    from app.services.candidates import CandidateService

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_preference_schema(conn)
    service = CandidateService(conn)

    payload = {
        "run_candidate_id": "minimal-1",
        "source_video_sha256": "sha256-video",
        "source_video_path": "D:/videos/sample.mp4",
        "start_sec": 0.0,
        "end_sec": 1.0,
    }

    candidate = service.materialize_run_candidate("run-minimal", payload)

    assert candidate.candidate_id.startswith("cand_")
    assert candidate.status == "candidate"
    assert conn.execute("SELECT COUNT(*) FROM candidate_gifs").fetchone()[0] == 1
