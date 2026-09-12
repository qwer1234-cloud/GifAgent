"""Tests for candidate capture validation, persistence, and publication linking."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from app.services.candidate_capture import (
    link_publication,
    persist_capture,
    validate_capture,
)
from app.services.preference_schema import apply_preference_schema


def test_empty_capture_is_not_complete():
    reasons = validate_capture({})
    assert "source_identity_missing" in reasons
    assert "baseline_missing" in reasons
    assert "preexport_key_missing" in reasons


def test_validate_capture_complete_payload():
    payload = {
        "run_id": "run-101",
        "preexport_key": "a" * 64,
        "capture_version": "capture-v1",
        "source_video_sha256": "b" * 64,
        "start_us": 1_000_000,
        "end_us": 4_000_000,
        "anchor_us": 2_500_000,
        "anchor_source": "best_frame",
        "base_quality_score": 0.85,
        "baseline_score": 0.85,
        "selection_status": "selected",
        "capture_status": "CAPTURED",
    }
    reasons = validate_capture(payload)
    assert reasons == []


def test_persist_capture_and_link_publication(tmp_path: Path):
    conn = sqlite3.connect(":memory:")
    apply_preference_schema(conn)

    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()

    payload = {
        "run_id": "run-202",
        "preexport_key": "c" * 64,
        "capture_version": "capture-v1",
        "source_video_sha256": "d" * 64,
        "start_us": 2_000_000,
        "end_us": 5_000_000,
        "anchor_us": 3_500_000,
        "anchor_source": "window_midpoint",
        "base_quality_score": 0.90,
        "baseline_score": 0.90,
        "selection_status": "exported",
        "capture_status": "CAPTURED",
    }

    record_id = persist_capture(conn, payload, evidence_root)
    assert isinstance(record_id, str) and len(record_id) > 0

    # Verify candidate_analysis_records row
    row = conn.execute(
        "SELECT preexport_key, payload_sha256, payload_path FROM candidate_analysis_records WHERE record_id = ?",
        (record_id,),
    ).fetchone()
    assert row is not None
    assert row[0] == "c" * 64
    payload_file = Path(row[2])
    assert payload_file.exists()
    assert hashlib.sha256(payload_file.read_bytes()).hexdigest() == row[1]

    # Test publication link
    link_publication(conn, record_id, candidate_id="cand_123456", gif_sha256="e" * 64)
    pub_row = conn.execute(
        "SELECT candidate_id, gif_sha256 FROM candidate_analysis_publications WHERE record_id = ?",
        (record_id,),
    ).fetchone()
    assert pub_row is not None
    assert pub_row[0] == "cand_123456"
    assert pub_row[1] == "e" * 64


def test_duplicate_publication_link_is_idempotent(tmp_path: Path):
    conn = sqlite3.connect(":memory:")
    apply_preference_schema(conn)

    payload = {
        "run_id": "run-303",
        "preexport_key": "e" * 64,
        "capture_version": "capture-v1",
        "source_video_sha256": "f" * 64,
        "start_us": 1_000_000,
        "end_us": 4_000_000,
        "anchor_us": 2_500_000,
        "anchor_source": "best_frame",
        "baseline_score": 0.80,
        "capture_status": "CAPTURED",
    }
    record_id = persist_capture(conn, payload, tmp_path)

    # Calling link_publication multiple times with same data should be idempotent
    link_publication(conn, record_id, "cand_test", "1" * 64)
    link_publication(conn, record_id, "cand_test", "1" * 64)

    count = conn.execute(
        "SELECT COUNT(*) FROM candidate_analysis_publications WHERE record_id = ?",
        (record_id,),
    ).fetchone()[0]
    assert count == 1


def test_different_runs_can_link_same_candidate(tmp_path: Path):
    conn = sqlite3.connect(":memory:")
    apply_preference_schema(conn)

    payload1 = {
        "run_id": "run-1",
        "preexport_key": "1" * 64,
        "capture_version": "capture-v1",
        "source_video_sha256": "a" * 64,
        "start_us": 1_000_000,
        "end_us": 4_000_000,
        "anchor_us": 2_500_000,
        "anchor_source": "best_frame",
        "baseline_score": 0.80,
        "capture_status": "CAPTURED",
    }
    payload2 = {
        "run_id": "run-2",
        "preexport_key": "1" * 64,
        "capture_version": "capture-v1",
        "source_video_sha256": "a" * 64,
        "start_us": 1_000_000,
        "end_us": 4_000_000,
        "anchor_us": 2_500_000,
        "anchor_source": "best_frame",
        "baseline_score": 0.80,
        "capture_status": "CAPTURED",
    }
    rec1 = persist_capture(conn, payload1, tmp_path)
    rec2 = persist_capture(conn, payload2, tmp_path)
    assert rec1 != rec2

    # Both runs can link to the same candidate
    link_publication(conn, rec1, "cand_shared", "gif_sha")
    link_publication(conn, rec2, "cand_shared", "gif_sha")

    rows = conn.execute(
        "SELECT record_id FROM candidate_analysis_publications WHERE candidate_id = 'cand_shared'"
    ).fetchall()
    assert len(rows) == 2


def test_library_ingest_links_publication(tmp_path: Path):
    from app.services.library_ingest import ingest_published_clips

    conn = sqlite3.connect(":memory:")
    apply_preference_schema(conn)

    gif_file = tmp_path / "test.gif"
    gif_file.write_bytes(b"GIF89a-test-bytes")
    expected_gif_sha = hashlib.sha256(b"GIF89a-test-bytes").hexdigest()

    # Create an analysis record first
    payload = {
        "run_id": "run-library-test",
        "preexport_key": "9" * 64,
        "capture_version": "capture-v1",
        "source_video_sha256": "8" * 64,
        "start_us": 1_000_000,
        "end_us": 4_000_000,
        "anchor_us": 2_500_000,
        "anchor_source": "best_frame",
        "baseline_score": 0.75,
        "capture_status": "CAPTURED",
    }
    rec_id = persist_capture(conn, payload, tmp_path)

    clips = [
        {
            "clip_id": "clip_001",
            "start_ts": 1.0,
            "end_ts": 4.0,
            "formal_path": str(gif_file),
            "gif_worthiness": 0.75,
            "analysis_record_id": rec_id,
            "parent_source": {"source_file_sha256": "8" * 64},
        }
    ]

    res = ingest_published_clips(
        conn, video_path=str(tmp_path / "video.mp4"), clips=clips, run_id="run-library-test"
    )
    assert res["ingested"] == 1

    pub_row = conn.execute(
        "SELECT record_id, gif_sha256 FROM candidate_analysis_publications WHERE record_id = ?",
        (rec_id,),
    ).fetchone()
    assert pub_row is not None
    assert pub_row[0] == rec_id
    assert pub_row[1] == expected_gif_sha


# ── Task 0 regressions (R8): content-addressed, immutable capture payloads ──


@pytest.fixture()
def capture_conn():
    conn = sqlite3.connect(":memory:")
    apply_preference_schema(conn)
    yield conn


@pytest.fixture()
def capture_payload():
    return {
        "run_id": "run-capture-vtest",
        "preexport_key": "a" * 64,
        "capture_version": "capture-v1",
        "source_video_sha256": "b" * 64,
        "start_us": 1_000_000,
        "end_us": 4_000_000,
        "anchor_us": 2_500_000,
        "anchor_source": "best_frame",
        "baseline_score": 0.80,
        "capture_status": "CAPTURED",
    }


def test_capture_versions_do_not_overwrite_old_payload(
    capture_conn, tmp_path, capture_payload
):
    """R8: a new capture version must write its own content-addressed file;
    the old row's payload bytes can never change underneath it."""
    from app.services.candidate_capture import persist_capture as persist_typed

    payload = dict(capture_payload)
    first = persist_typed(capture_conn, payload, tmp_path)
    first_bytes = Path(first.payload_path).read_bytes()
    second = persist_typed(
        capture_conn, {**payload, "capture_version": "capture-v2"}, tmp_path
    )
    assert first.payload_path != second.payload_path
    assert Path(first.payload_path).read_bytes() == first_bytes


def test_capture_version_conflict_preserves_first_row_payload(
    capture_conn, tmp_path, capture_payload
):
    """R8 assertion variant on today's string record_id return: writing a
    second capture version must not rewrite the file the first row points at."""
    first_id = persist_capture(capture_conn, dict(capture_payload), tmp_path)
    row1 = capture_conn.execute(
        "SELECT payload_path, payload_sha256 FROM candidate_analysis_records "
        "WHERE record_id = ?",
        (first_id,),
    ).fetchone()

    persist_capture(
        capture_conn, {**capture_payload, "capture_version": "capture-v2"}, tmp_path
    )

    row1_after = capture_conn.execute(
        "SELECT payload_path, payload_sha256 FROM candidate_analysis_records "
        "WHERE record_id = ?",
        (first_id,),
    ).fetchone()
    assert row1_after[0] == row1[0]
    assert Path(row1[0]).is_file()
    assert hashlib.sha256(Path(row1[0]).read_bytes()).hexdigest() == row1[1]


def test_same_capture_identity_with_different_content_conflicts(
    capture_conn, tmp_path, capture_payload
):
    """R8: identical (run, preexport key, version) with different payload
    content must raise a conflict instead of silently updating evidence."""
    from app.services.candidate_capture import CaptureConflict

    persist_capture(capture_conn, dict(capture_payload), tmp_path)
    with pytest.raises(CaptureConflict):
        persist_capture(
            capture_conn, {**capture_payload, "baseline_score": 0.91}, tmp_path
        )

