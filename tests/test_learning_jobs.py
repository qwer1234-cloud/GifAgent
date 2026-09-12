"""Tests for learning jobs eligibility, queueing, lease management, and recoverable execution."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.services.learning_jobs import (
    DEFAULT_LEARNING_POLICY,
    enqueue_learning,
    learning_eligibility,
    run_learning_once,
)
from app.services.preference_schema import apply_preference_schema


def test_no_feedback_is_not_a_training_trigger():
    assert "insufficient_new_feedback" in learning_eligibility(
        {"new_effective_candidates": 0, "new_source_groups": 0, "positive_candidates": 0, "negative_candidates": 0},
        {"min_new_effective_candidates": 100},
    )


def test_learning_eligibility_all_criteria():
    policy = {
        "min_new_effective_candidates": 100,
        "min_new_source_groups": 20,
        "min_positive_candidates": 20,
        "min_negative_candidates": 20,
        "min_interval_hours": 24,
    }

    # Deficient stats
    stats_low_sources = {
        "new_effective_candidates": 150,
        "new_source_groups": 10,
        "positive_candidates": 30,
        "negative_candidates": 30,
        "hours_since_last_job": 48,
    }
    reasons = learning_eligibility(stats_low_sources, policy)
    assert "insufficient_new_sources" in reasons

    # All criteria satisfied
    stats_ok = {
        "new_effective_candidates": 120,
        "new_source_groups": 25,
        "positive_candidates": 25,
        "negative_candidates": 25,
        "hours_since_last_job": 30,
    }
    assert learning_eligibility(stats_ok, policy) == []


def test_enqueue_learning_and_lease_execution():
    conn = sqlite3.connect(":memory:")
    apply_preference_schema(conn)

    job_id = enqueue_learning(
        conn,
        input_snapshot_sha256="input_sha_12345",
        policy_sha256="policy_sha_67890",
        job_type="curve_preference",
    )
    assert isinstance(job_id, str) and len(job_id) > 0

    # Second enqueue of identical input+policy returns existing job (idempotent)
    job_id2 = enqueue_learning(
        conn,
        input_snapshot_sha256="input_sha_12345",
        policy_sha256="policy_sha_67890",
        job_type="curve_preference",
    )
    assert job_id2 == job_id

    # Run once (integer epoch injection per Task 6 Step 1)
    res = run_learning_once(conn, now_epoch=1_800_000_000, owner="worker-1")
    assert res["status"] in ("SUCCEEDED", "RUNNING", "NEEDS_ATTENTION", "IDLE")


# ── Task 0 regressions (R1, R7): real stage execution and lease safety ──────


def test_missing_snapshot_never_completes_learning_job(tmp_path):
    """R1: a job whose frozen snapshot does not exist must never report success."""
    conn = sqlite3.connect(tmp_path / "learning.db")
    apply_preference_schema(conn)
    enqueue_learning(conn, "f" * 64, "e" * 64)
    result = run_learning_once(conn, now_epoch=1_800_000_000, owner="w1")
    assert result["status"] == "NEEDS_ATTENTION"
    assert result["error_code"] == "snapshot_not_found"
    assert conn.execute("SELECT COUNT(*) FROM curve_preference_models").fetchone()[0] == 0


def test_missing_snapshot_never_completes_learning_job_legacy_clock(tmp_path):
    """R1 assertion variant on the current ISO-clock signature.

    Today the worker claims the job and writes COMPLETE/SUCCEEDED without
    executing any stage; that empty success must become NEEDS_ATTENTION.
    """
    conn = sqlite3.connect(tmp_path / "learning.db")
    apply_preference_schema(conn)
    enqueue_learning(conn, "f" * 64, "e" * 64)
    result = run_learning_once(
        conn, now_utc="2026-09-12T00:00:00+00:00", owner="w1"
    )
    assert result["status"] == "NEEDS_ATTENTION"
    assert result["error_code"] == "snapshot_not_found"
    assert conn.execute("SELECT COUNT(*) FROM curve_preference_models").fetchone()[0] == 0


def test_claim_is_atomic_between_two_connections(tmp_path):
    """R7: an atomically claimed lease must never be handed to a second owner."""
    from app.services.learning_jobs import claim_learning_job

    db_path = tmp_path / "jobs.db"
    conn1 = sqlite3.connect(db_path)
    apply_preference_schema(conn1)
    conn2 = sqlite3.connect(db_path)
    enqueue_learning(conn1, "f" * 64, "e" * 64)

    first = claim_learning_job(
        conn1, owner="w1", now_epoch=1_800_000_000, lease_seconds=1800
    )
    second = claim_learning_job(
        conn2, owner="w2", now_epoch=1_800_000_001, lease_seconds=1800
    )
    assert first is not None
    assert second is None


def test_run_learning_once_respects_live_epoch_lease(tmp_path):
    """R7: a live integer-epoch lease must not be stealable by a second
    worker before it expires, and a claim must be one atomic CAS."""
    conn = sqlite3.connect(tmp_path / "learning.db")
    apply_preference_schema(conn)
    enqueue_learning(conn, "f" * 64, "e" * 64)
    conn.execute(
        "UPDATE learning_jobs SET status='RUNNING', owner='w1', "
        "lease_expires_epoch=1800000500"
    )
    conn.commit()
    result = run_learning_once(conn, now_epoch=1_800_000_100, owner="w2")
    assert result["status"] == "IDLE"

    # After expiry the same job becomes claimable again.
    conn2 = sqlite3.connect(tmp_path / "learning.db")
    reclaimed = run_learning_once(conn2, now_epoch=1_800_000_600, owner="w3")
    assert reclaimed["status"] in ("RUNNING", "NEEDS_ATTENTION", "RETRY_WAIT")
