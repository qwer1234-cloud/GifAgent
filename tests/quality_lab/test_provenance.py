from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app.quality_lab.provenance import (
    provenance_for_candidate,
    snapshot_experiment_config,
)
from app.services.provenance import Provenance, hash_prompt_text, provenance_to_json

# ===================================================================
# Helpers
# ===================================================================


def _apply_both_schemas(conn: sqlite3.Connection) -> None:
    """Create all tables needed by ``provenance_for_candidate``."""
    from app.services.preference_schema import apply_preference_schema
    from app.task_engine.schema import apply_task_schema

    apply_preference_schema(conn)
    apply_task_schema(conn)


def _insert_candidate(
    conn: sqlite3.Connection,
    candidate_id: str,
    source_run_id: str,
    *,
    artifact_id: str | None = None,
    provenance_json: str | None = None,
    source_video_sha256: str = "sha256_aaa",
    start_sec: float = 0.0,
    end_sec: float = 10.0,
) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO candidate_gifs
           (candidate_id, source_run_id, source_run_candidate_id,
            source_video_sha256, source_video_path,
            start_sec, end_sec,
            artifact_id, provenance_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            candidate_id,
            source_run_id,
            f"cid_{candidate_id}",
            source_video_sha256,
            f"/videos/{source_video_sha256}.mp4",
            start_sec,
            end_sec,
            artifact_id,
            provenance_json,
        ),
    )


def _insert_task_job(
    conn: sqlite3.Connection,
    job_id: str,
    config_json: str,
) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO task_jobs
           (job_id, directory, directory_key, config_json, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, 'succeeded', '2026-01-01T00:00:00', '2026-01-01T00:00:00')""",
        (job_id, f"/jobs/{job_id}", f"/jobs/{job_id}", config_json),
    )


def _insert_task_video(
    conn: sqlite3.Connection,
    video_id: str,
    job_id: str,
    *,
    fingerprint: str = "fp_default",
) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO task_videos
           (video_id, job_id, path, fingerprint, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, 'succeeded', '2026-01-01T00:00:00', '2026-01-01T00:00:00')""",
        (video_id, job_id, f"/videos/{video_id}.mp4", fingerprint),
    )


def _insert_task_stage(
    conn: sqlite3.Connection,
    stage_id: str,
    video_id: str,
    *,
    stage_name: str = "vlm",
    clip_id: str | None = None,
) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO task_stages
           (stage_id, video_id, stage_name, clip_id, input_key, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, 'input_key', 'succeeded', '2026-01-01T00:00:00', '2026-01-01T00:00:00')""",
        (stage_id, video_id, stage_name, clip_id),
    )


def _insert_task_artifact(
    conn: sqlite3.Connection,
    artifact_id: str,
    job_id: str,
    video_id: str,
    *,
    stage_name: str = "vlm",
    clip_id: str | None = None,
) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO task_artifacts
           (artifact_id, job_id, video_id, stage_name, clip_id, path, sha256, size_bytes, provenance_json, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '2026-01-01T00:00:00')""",
        (
            artifact_id,
            job_id,
            video_id,
            stage_name,
            clip_id,
            f"/artifacts/{artifact_id}.gif",
            "sha256_artifact",
            1024,
            '{}',
        ),
    )


# ===================================================================
# Tests for hash_prompt_text (app/services/provenance.py)
# ===================================================================


class TestHashPromptText:
    def test_returns_hex_string(self) -> None:
        h = hash_prompt_text("hello world")
        assert isinstance(h, str)
        assert len(h) == 64  # SHA-256 hex

    def test_deterministic(self) -> None:
        assert hash_prompt_text("hello") == hash_prompt_text("hello")

    def test_different_text_different_hash(self) -> None:
        assert hash_prompt_text("hello") != hash_prompt_text("world")

    def test_empty_string(self) -> None:
        h = hash_prompt_text("")
        assert isinstance(h, str)
        assert len(h) == 64


# ===================================================================
# Tests for snapshot_experiment_config (app/quality_lab/provenance.py)
# ===================================================================


class TestSnapshotExperimentConfig:
    """``snapshot_experiment_config`` produces deterministic config IDs
    independent of Python dict ordering."""

    PROVENANCE = Provenance(
        git_commit="abc123def456",
        config_hash="cfg_hash_001",
        model_versions={"vlm.model": "claude-3-opus"},
        prompt_hashes={"vlm": "ph_001"},
        stage_versions={"vlm": "1.2.3"},
    )

    CONFIG_A = {
        "vlm": {"model": "claude-3-opus", "temperature": 0.7},
        "refine": {"model": "gpt-4", "max_tokens": 1024},
    }
    # Same content, different key ordering
    CONFIG_A_REORDERED = {
        "refine": {"max_tokens": 1024, "model": "gpt-4"},
        "vlm": {"temperature": 0.7, "model": "claude-3-opus"},
    }
    CONFIG_B = {
        "vlm": {"model": "claude-3-opus", "temperature": 0.5},
        "refine": {"model": "gpt-4", "max_tokens": 1024},
    }

    def test_returns_experiment_config(self) -> None:
        ec = snapshot_experiment_config(self.CONFIG_A, self.PROVENANCE)
        assert ec.config_id
        assert ec.config_json
        assert ec.provenance_json

    def test_config_id_from_canonical_hash(self) -> None:
        """config_id must be the canonical hash of config."""
        from app.task_engine.fingerprints import canonical_hash

        ec = snapshot_experiment_config(self.CONFIG_A, self.PROVENANCE)
        assert ec.config_id == canonical_hash(self.CONFIG_A)

    def test_same_config_yields_same_config_id(self) -> None:
        a = snapshot_experiment_config(self.CONFIG_A, self.PROVENANCE)
        b = snapshot_experiment_config(self.CONFIG_A, self.PROVENANCE)
        assert a.config_id == b.config_id
        assert a.config_json == b.config_json

    def test_dict_ordering_does_not_change_config_id(self) -> None:
        """Dictionary key reordering must produce identical results."""
        a = snapshot_experiment_config(self.CONFIG_A, self.PROVENANCE)
        b = snapshot_experiment_config(self.CONFIG_A_REORDERED, self.PROVENANCE)
        assert a.config_id == b.config_id
        assert a.config_json == b.config_json

    def test_different_config_yields_different_id(self) -> None:
        a = snapshot_experiment_config(self.CONFIG_A, self.PROVENANCE)
        c = snapshot_experiment_config(self.CONFIG_B, self.PROVENANCE)
        assert a.config_id != c.config_id

    def test_provenance_json_round_trip(self) -> None:
        ec = snapshot_experiment_config(self.CONFIG_A, self.PROVENANCE)
        parsed = json.loads(ec.provenance_json)
        assert parsed["git_commit"] == "abc123def456"
        assert parsed["config_hash"] == "cfg_hash_001"
        assert parsed["model_versions"]["vlm.model"] == "claude-3-opus"
        assert parsed["prompt_hashes"]["vlm"] == "ph_001"
        assert parsed["stage_versions"]["vlm"] == "1.2.3"

    def test_config_json_round_trip(self) -> None:
        ec = snapshot_experiment_config(self.CONFIG_A, self.PROVENANCE)
        parsed = json.loads(ec.config_json)
        assert parsed["vlm"]["model"] == "claude-3-opus"


# ===================================================================
# Tests for provenance_for_candidate (app/quality_lab/provenance.py)
# ===================================================================


class TestProvenanceForCandidate:
    """Tests the full provenance resolution chain from candidate_gifs
    through task_jobs, task_videos, task_stages, and task_artifacts."""

    SAMPLE_PROVENANCE_JSON = json.dumps(
        {
            "git_commit": "deadbeef01234567",
            "config_hash": "cfg_hash_002",
            "model_versions": {
                "vlm.model": "claude-3-opus-20240229",
                "refine.model": "gpt-4-turbo",
            },
            "prompt_hashes": {
                "vlm": "abc123def456",
                "refine": "789012345678",
            },
            "stage_versions": {
                "vlm": "1.2.3",
                "refine": "2.0.0",
                "synthesize": "1.0.0",
            },
        },
        sort_keys=True,
    )

    @pytest.fixture
    def conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        _apply_both_schemas(c)
        return c

    def seed_full_chain(self, conn: sqlite3.Connection) -> None:
        """Insert a complete chain: job -> video -> stage -> artifact + candidate."""
        _insert_task_job(conn, "job_001", '{"vlm": {"model": "claude-3-opus"}}')
        _insert_task_video(
            conn, "vid_001", "job_001", fingerprint="fp_video_abc"
        )
        _insert_task_stage(
            conn, "stage_001", "vid_001", stage_name="vlm"
        )
        _insert_task_stage(
            conn, "stage_002", "vid_001", stage_name="refine", clip_id="clip_01"
        )
        _insert_task_artifact(
            conn, "art_001", "job_001", "vid_001", stage_name="vlm"
        )
        _insert_task_artifact(
            conn,
            "art_002",
            "job_001",
            "vid_001",
            stage_name="refine",
            clip_id="clip_01",
        )
        _insert_candidate(
            conn,
            "candidate_001",
            "job_001",
            artifact_id="art_001",
            provenance_json=self.SAMPLE_PROVENANCE_JSON,
        )
        conn.commit()

    # -- Happy path ------------------------------------------------

    def test_resolves_all_provenance_fields(self, conn: sqlite3.Connection) -> None:
        """The resolved dict includes git_commit, config_hash, model_versions,
        prompt_hashes, stage_versions, source_fingerprint, task_job,
        stage info, and artifact_id."""
        self.seed_full_chain(conn)

        result = provenance_for_candidate(conn, "candidate_001")

        assert result["candidate_id"] == "candidate_001"
        assert result["source_run_id"] == "job_001"

        # Provenance from stored JSON
        assert result["git_commit"] == "deadbeef01234567"
        assert result["config_hash"] == "cfg_hash_002"
        assert result["model_versions"]["vlm.model"] == "claude-3-opus-20240229"
        assert result["model_versions"]["refine.model"] == "gpt-4-turbo"
        assert result["prompt_hashes"]["vlm"] == "abc123def456"
        assert result["stage_versions"]["vlm"] == "1.2.3"

        # Source fingerprint from task_videos
        assert result["source_fingerprint"] == "fp_video_abc"

        # Task job
        assert result["task_job"]["job_id"] == "job_001"

        # Task stages
        stage_names = {s["stage_name"] for s in result["task_stages"]}
        assert "vlm" in stage_names
        assert "refine" in stage_names

        # Task artifacts
        assert result["artifact_id"] == "art_001"
        art_ids = {a["artifact_id"] for a in result["task_artifacts"]}
        assert "art_001" in art_ids
        assert "art_002" in art_ids

    def test_full_chain_single_stage(self, conn: sqlite3.Connection) -> None:
        """Simpler chain with one stage and one artifact."""
        _insert_task_job(conn, "job_002", '{"vlm": {"model": "gpt-4"}}')
        _insert_task_video(
            conn, "vid_002", "job_002", fingerprint="fp_simple"
        )
        _insert_task_stage(conn, "stage_003", "vid_002", stage_name="vlm")
        _insert_task_artifact(
            conn, "art_003", "job_002", "vid_002", stage_name="vlm"
        )
        _insert_candidate(
            conn,
            "candidate_002",
            "job_002",
            artifact_id="art_003",
            provenance_json=self.SAMPLE_PROVENANCE_JSON,
        )
        conn.commit()

        result = provenance_for_candidate(conn, "candidate_002")
        assert result["task_job"]["job_id"] == "job_002"
        assert result["source_fingerprint"] == "fp_simple"
        assert result["artifact_id"] == "art_003"
        assert len(result["task_stages"]) == 1
        assert len(result["task_artifacts"]) == 1

    # -- Legacy candidate (null provenance_json) --------------------

    def test_legacy_candidate_null_provenance(self, conn: sqlite3.Connection) -> None:
        """A legacy candidate with null provenance_json should still resolve
        task engine fields but return None for provenance fields."""
        _insert_task_job(conn, "job_legacy", '{"vlm": {"model": "gpt-4"}}')
        _insert_task_video(
            conn, "vid_legacy", "job_legacy", fingerprint="fp_legacy"
        )
        _insert_task_stage(
            conn, "stage_legacy", "vid_legacy", stage_name="vlm"
        )
        _insert_task_artifact(
            conn, "art_legacy", "job_legacy", "vid_legacy", stage_name="vlm"
        )
        _insert_candidate(
            conn,
            "candidate_legacy",
            "job_legacy",
            artifact_id=None,
            provenance_json=None,
        )
        conn.commit()

        result = provenance_for_candidate(conn, "candidate_legacy")

        # Task engine fields resolve
        assert result["task_job"]["job_id"] == "job_legacy"
        assert result["source_fingerprint"] == "fp_legacy"

        # Provenance-specific fields are None/empty for legacy
        assert result["git_commit"] is None
        assert result["config_hash"] is None
        assert result["model_versions"] == {}
        assert result["prompt_hashes"] == {}
        assert result["stage_versions"] == {}
        assert result["artifact_id"] is None

    # -- Candidate not found ---------------------------------------

    def test_raises_on_missing_candidate(self, conn: sqlite3.Connection) -> None:
        with pytest.raises(ValueError, match="Candidate not found"):
            provenance_for_candidate(conn, "nonexistent")

    # -- Partial chain (no task job) -------------------------------

    def test_candidate_without_task_job(self, conn: sqlite3.Connection) -> None:
        """When a candidate has no matching task_job, task fields are None."""
        _insert_candidate(
            conn,
            "orphan",
            "job_missing",
            artifact_id=None,
            provenance_json=None,
        )
        conn.commit()

        result = provenance_for_candidate(conn, "orphan")
        assert result["task_job"] is None
        assert result["task_video"] is None
        assert result["source_fingerprint"] is None
        assert result["artifact_id"] is None

    # -- Schema migration: columns present -------------------------

    def test_migration_adds_artifact_id_and_provenance_json(
        self, conn: sqlite3.Connection
    ) -> None:
        """Verify that the migration added the new columns."""
        cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(candidate_gifs)").fetchall()
        }
        assert "artifact_id" in cols
        assert "provenance_json" in cols

    def test_migration_idempotent(self, conn: sqlite3.Connection) -> None:
        """Running the schema migration twice must not error."""
        from app.services.preference_schema import apply_preference_schema

        apply_preference_schema(conn)  # second call

        cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(candidate_gifs)").fetchall()
        }
        assert "artifact_id" in cols
        assert "provenance_json" in cols

    # -- provenance_json round-trip ---------------------------------

    def test_provenance_json_stored_and_retrieved(
        self, conn: sqlite3.Connection
    ) -> None:
        """Verify that a provenance set via INSERT is returned unchanged."""
        _insert_task_job(conn, "job_003", '{"vlm": {"model": "default"}}')
        _insert_task_video(conn, "vid_003", "job_003", fingerprint="fp_003")
        _insert_task_stage(conn, "stage_003", "vid_003", stage_name="vlm")
        _insert_task_artifact(
            conn, "art_003", "job_003", "vid_003", stage_name="vlm"
        )
        _insert_candidate(
            conn,
            "candidate_003",
            "job_003",
            artifact_id="art_003",
            provenance_json=self.SAMPLE_PROVENANCE_JSON,
        )
        conn.commit()

        result = provenance_for_candidate(conn, "candidate_003")
        assert result["provenance_json"] == self.SAMPLE_PROVENANCE_JSON

    # -- Multiple candidates per job --------------------------------

    def test_multiple_candidates_same_job(self, conn: sqlite3.Connection) -> None:
        """Two candidates sharing the same job both resolve properly."""
        _insert_task_job(conn, "job_shared", '{"vlm": {"model": "shared"}}')
        _insert_task_video(
            conn, "vid_shared", "job_shared", fingerprint="fp_shared"
        )
        _insert_task_stage(
            conn, "stage_shared", "vid_shared", stage_name="vlm"
        )
        _insert_task_artifact(
            conn, "art_a", "job_shared", "vid_shared", stage_name="vlm"
        )
        _insert_task_artifact(
            conn, "art_b", "job_shared", "vid_shared", stage_name="vlm"
        )
        _insert_candidate(
            conn,
            "cand_a",
            "job_shared",
            artifact_id="art_a",
            provenance_json=self.SAMPLE_PROVENANCE_JSON,
        )
        _insert_candidate(
            conn,
            "cand_b",
            "job_shared",
            artifact_id="art_b",
            provenance_json=self.SAMPLE_PROVENANCE_JSON,
        )
        conn.commit()

        result_a = provenance_for_candidate(conn, "cand_a")
        result_b = provenance_for_candidate(conn, "cand_b")

        assert result_a["task_job"]["job_id"] == "job_shared"
        assert result_b["task_job"]["job_id"] == "job_shared"
        assert result_a["artifact_id"] == "art_a"
        assert result_b["artifact_id"] == "art_b"
        assert result_a["source_fingerprint"] == "fp_shared"


# ===================================================================
# Integration: snapshot + provenance round-trip
# ===================================================================


class TestSnapshotProvenanceIntegration:
    """End-to-end: create a Provenance, snapshot it to ExperimentConfig,
    and verify the round-trip through provenance_for_candidate."""

    @pytest.fixture
    def conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        _apply_both_schemas(c)
        return c

    def test_snapshot_and_resolve(self, conn: sqlite3.Connection) -> None:
        """Creating a snapshot and storing its provenance on a candidate
        should allow full resolution via provenance_for_candidate."""
        config = {"vlm": {"model": "claude-3-opus", "temperature": 0.7}}

        prov = Provenance(
            git_commit="integ_commit",
            config_hash="integ_cfg_hash",
            model_versions={"vlm.model": "claude-3-opus"},
            prompt_hashes={"vlm": "integ_prompt_hash"},
            stage_versions={"vlm": "1.0.0"},
        )

        ec = snapshot_experiment_config(config, prov)

        _insert_task_job(conn, "job_integ", json.dumps(config))
        _insert_task_video(
            conn, "vid_integ", "job_integ", fingerprint="fp_integ"
        )
        _insert_task_stage(
            conn, "stage_integ", "vid_integ", stage_name="vlm"
        )
        _insert_task_artifact(
            conn, "art_integ", "job_integ", "vid_integ", stage_name="vlm"
        )
        _insert_candidate(
            conn,
            "cand_integ",
            "job_integ",
            artifact_id="art_integ",
            provenance_json=ec.provenance_json,
        )
        conn.commit()

        result = provenance_for_candidate(conn, "cand_integ")
        assert result["git_commit"] == "integ_commit"
        assert result["config_hash"] == "integ_cfg_hash"
        assert result["model_versions"]["vlm.model"] == "claude-3-opus"
        assert result["prompt_hashes"]["vlm"] == "integ_prompt_hash"
        assert result["stage_versions"]["vlm"] == "1.0.0"
        assert result["artifact_id"] == "art_integ"
        assert result["source_fingerprint"] == "fp_integ"
