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

    persisted = persist_capture(conn, payload, evidence_root)
    record_id = persisted.record_id
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
    record_id = persist_capture(conn, payload, tmp_path).record_id

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
    rec1 = persist_capture(conn, payload1, tmp_path).record_id
    rec2 = persist_capture(conn, payload2, tmp_path).record_id
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
    rec_id = persist_capture(conn, payload, tmp_path).record_id

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
    first_id = persist_capture(capture_conn, dict(capture_payload), tmp_path).record_id
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


def test_persist_capture_is_idempotent_for_identical_content(
    capture_conn, tmp_path, capture_payload
):
    """Task 1 Step 3: re-persisting identical content returns the same row
    without duplicating files."""
    first = persist_capture(capture_conn, dict(capture_payload), tmp_path)
    second = persist_capture(capture_conn, dict(capture_payload), tmp_path)
    assert first.record_id == second.record_id
    assert first.payload_path == second.payload_path
    assert first.payload_sha256 == second.payload_sha256
    count = capture_conn.execute(
        "SELECT COUNT(*) FROM candidate_analysis_records"
    ).fetchone()[0]
    assert count == 1


def test_persist_capture_uses_content_addressed_path(
    capture_conn, tmp_path, capture_payload
):
    """Task 1 Step 2: payload lives at
    evidence_root/run_id/capture_version/preexport_key/payload_sha256.json."""
    persisted = persist_capture(capture_conn, dict(capture_payload), tmp_path)
    expected_tail = Path(
        "run-capture-vtest/capture-v1"
    ) / capture_payload["preexport_key"] / f"{persisted.payload_sha256}.json"
    assert Path(persisted.payload_path) == (tmp_path / expected_tail).resolve()
    assert Path(persisted.payload_path).is_file()


def test_persist_capture_marks_row_needs_repair_when_file_corrupted(
    capture_conn, tmp_path, capture_payload
):
    """Task 1 Step 6: a DB row whose payload file is missing or corrupt must
    be flagged NEEDS_REPAIR; original evidence is never overwritten."""
    persisted = persist_capture(capture_conn, dict(capture_payload), tmp_path)
    Path(persisted.payload_path).write_bytes(b"tampered-not-original")

    again = persist_capture(capture_conn, dict(capture_payload), tmp_path)
    assert again.payload_sha256 == persisted.payload_sha256

    status = capture_conn.execute(
        "SELECT capture_status FROM candidate_analysis_records WHERE record_id = ?",
        (persisted.record_id,),
    ).fetchone()[0]
    assert status == "NEEDS_REPAIR"
    # The corrupted file was left alone (no silent evidence rewrite).
    assert Path(persisted.payload_path).read_bytes() == b"tampered-not-original"


def test_persist_capture_marks_row_needs_repair_when_file_missing(
    capture_conn, tmp_path, capture_payload
):
    persisted = persist_capture(capture_conn, dict(capture_payload), tmp_path)
    Path(persisted.payload_path).unlink()

    persist_capture(capture_conn, dict(capture_payload), tmp_path)
    status = capture_conn.execute(
        "SELECT capture_status FROM candidate_analysis_records WHERE record_id = ?",
        (persisted.record_id,),
    ).fetchone()[0]
    assert status == "NEEDS_REPAIR"


def test_failed_capture_without_key_gets_versioned_failure_identity(
    capture_conn, tmp_path, capture_payload
):
    """Task 1 Step 3: a FAILED capture without a usable preexport key is saved
    under its own content-addressed failure identity, never a fake key."""
    failure = {
        "run_id": "run-capture-vtest",
        "capture_version": "capture-v1",
        "capture_status": "FAILED",
        "failure_reason": "source_unreadable",
    }
    persisted = persist_capture(capture_conn, failure, tmp_path)
    row = capture_conn.execute(
        "SELECT preexport_key, capture_version, capture_status, payload_path "
        "FROM candidate_analysis_records WHERE record_id = ?",
        (persisted.record_id,),
    ).fetchone()
    assert row[2] == "FAILED"
    assert row[1].endswith("-failure")
    assert row[3].isascii()

    # Re-persisting the same failure is idempotent.
    again = persist_capture(capture_conn, dict(failure), tmp_path)
    assert again.record_id == persisted.record_id


def test_append_candidate_analysis_event_is_append_only(
    capture_conn, tmp_path, capture_payload
):
    """Task 1 Step 4: identical record/kind/content is idempotent, different
    content creates a NEW event, and history is never updated."""
    from app.services.candidate_capture import (
        append_candidate_analysis_event,
        load_candidate_analysis_events,
    )

    persisted = persist_capture(capture_conn, dict(capture_payload), tmp_path)

    payload_v1 = {"rank": 1, "selected": True}
    event_a1 = append_candidate_analysis_event(
        capture_conn, persisted.record_id, "selected", payload_v1, tmp_path
    )
    event_a2 = append_candidate_analysis_event(
        capture_conn, persisted.record_id, "selected", dict(payload_v1), tmp_path
    )
    assert event_a1 == event_a2

    payload_v2 = {"rank": 2, "selected": False}
    event_b = append_candidate_analysis_event(
        capture_conn, persisted.record_id, "selected", payload_v2, tmp_path
    )
    assert event_b != event_a1

    events = load_candidate_analysis_events(capture_conn, persisted.record_id)
    kinds = [e["event_kind"] for e in events]
    assert kinds == ["selected", "selected"]
    hashes = {e["payload_sha256"] for e in events}
    assert len(hashes) == 2

    with pytest.raises(ValueError, match="invalid candidate analysis event kind"):
        append_candidate_analysis_event(
            capture_conn, persisted.record_id, "dislike", {}, tmp_path
        )


def test_validate_capture_rejects_bool_baseline_and_anchor_outside_window():
    """Task 1 Step 5: strict window and numeric validation."""
    base = {
        "run_id": "run-strict",
        "preexport_key": "a" * 64,
        "capture_version": "capture-v1",
        "source_video_sha256": "b" * 64,
        "start_us": 1_000_000,
        "end_us": 4_000_000,
        "anchor_us": 2_500_000,
        "baseline_score": 0.5,
        "capture_status": "CAPTURED",
    }
    assert validate_capture(dict(base)) == []

    bool_baseline = dict(base, baseline_score=True)
    assert "baseline_missing" in validate_capture(bool_baseline)

    outside = dict(base, anchor_us=5_000_000)
    assert "anchor_out_of_window" in validate_capture(outside)

    bad_status = dict(base, capture_status="EXPORTED")
    assert "capture_status_invalid" in validate_capture(bad_status)

    bad_key = dict(base, preexport_key="0" * 63)
    assert "preexport_key_missing" in validate_capture(bad_key)

    bad_sha = dict(base, source_video_sha256="0" * 64 + "x")
    assert "source_identity_missing" in validate_capture(bad_sha)
