"""Tests for champion promotion/rollback service (Phase 2 Task 6)."""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from pathlib import Path

import pytest

from app.quality_lab import apply_quality_schema
from app.quality_lab.promotion import (
    PROMOTION_GATE_THRESHOLD,
    CONFIG_VERSIONS_DIR,
    CURRENT_CONFIG_PATH,
    promote_config,
    rollback,
    list_champion_history,
)


# ===================================================================
# Factory helpers
# ===================================================================


def _seed_config(db: sqlite3.Connection, *, config_id: str | None = None) -> str:
    """Insert a minimal experiment config and return its config_id."""
    cid = config_id or f"cfg_{uuid.uuid4().hex[:8]}"
    db.execute(
        "INSERT INTO experiment_configs (config_id, config_json, provenance_json, created_at) "
        "VALUES (?, ?, ?, ?)",
        (cid, json.dumps({"vlm": {"model": "test"}}), "{}", "2026-07-18T00:00:00"),
    )
    db.commit()
    return cid


def _seed_manifest(db: sqlite3.Connection, n_items: int = 2) -> str:
    """Insert a manifest with *n_items*."""
    manifest_id = f"m_{uuid.uuid4().hex[:8]}"
    db.execute(
        "INSERT INTO benchmark_manifests (manifest_id, version, item_count, created_at) "
        "VALUES (?, ?, ?, ?)",
        (manifest_id, 1, n_items, "2026-07-18T00:00:00"),
    )
    for i in range(n_items):
        fp = f"fp_video{i:04d}"
        db.execute(
            "INSERT INTO benchmark_items "
            "(item_id, manifest_id, source_path, video_fingerprint, "
            " duration_bucket, resolution_bucket, pace_bucket, difficulty_tags, split) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f"item_{i:04d}",
                manifest_id,
                f"/videos/video{i:04d}.mp4",
                fp,
                "short", "hd", "medium",
                "action",
                "tune" if i == 0 else "holdout",
            ),
        )
    db.commit()
    return manifest_id


def _seed_run(
    db: sqlite3.Connection,
    manifest_id: str,
    config_id: str,
    *,
    split: str = "tune",
    status: str = "completed",
) -> str:
    """Insert an experiment run and return its run_id."""
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    db.execute(
        "INSERT INTO experiment_runs (run_id, manifest_id, config_id, split, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (run_id, manifest_id, config_id, split, status,
         "2026-07-18T00:00:00", "2026-07-18T00:00:00"),
    )
    db.commit()
    return run_id


def _seed_metric(
    db: sqlite3.Connection,
    run_id: str,
    *,
    metric_name: str = "export_integrity",
    value: float = 1.0,
) -> str:
    """Insert a metric value and return its metric_id."""
    metric_id = f"m_{uuid.uuid4().hex[:8]}"
    db.execute(
        "INSERT INTO metric_values (metric_id, run_id, metric_name, value, item_id, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (metric_id, run_id, metric_name, value, None, "2026-07-18T00:00:00"),
    )
    db.commit()
    return metric_id


def _seed_ab_session(
    db: sqlite3.Connection,
    run_a: str,
    run_b: str,
    *,
    status: str = "completed",
) -> str:
    """Insert an AB session and return its session_id."""
    session_id = f"ab_{uuid.uuid4().hex[:8]}"
    db.execute(
        "INSERT INTO ab_sessions (session_id, run_a, run_b, seed, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (session_id, run_a, run_b, 42, status, "2026-07-18T00:00:00"),
    )
    db.commit()
    return session_id


# ===================================================================
# Tests
# ===================================================================


class TestPromoteConfig:
    """``promote_config`` — champion promotion with gates."""

    @pytest.fixture
    def db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        apply_quality_schema(conn)
        return conn

    @pytest.fixture
    def seed_promotable(self, db: sqlite3.Connection, tmp_path: Path):
        """Seed a database with a fully promotable config.

        Returns a dict with keys: config_id, tune_run_id, holdout_run_id.
        Patches CONFIG_VERSIONS_DIR and CURRENT_CONFIG_PATH to tmp_path.
        """
        cid = _seed_config(db)
        mid = _seed_manifest(db)
        tune_rid = _seed_run(db, mid, cid, split="tune", status="completed")
        holdout_rid = _seed_run(db, mid, cid, split="holdout", status="completed")
        _seed_metric(db, tune_rid, metric_name="export_integrity", value=0.95)
        _seed_metric(db, holdout_rid, metric_name="export_integrity", value=0.98)
        _seed_ab_session(db, tune_rid, holdout_rid, status="completed")

        # Patch paths to tmp_path for atomic write verification
        orig_versions = CONFIG_VERSIONS_DIR
        orig_current = CURRENT_CONFIG_PATH

        import app.quality_lab.promotion as promo_module
        promo_module.CONFIG_VERSIONS_DIR = str(tmp_path / "config_versions")
        promo_module.CURRENT_CONFIG_PATH = str(tmp_path / "current_config.json")

        yield {"config_id": cid, "tune_run_id": tune_rid, "holdout_run_id": holdout_rid}

        # Restore
        promo_module.CONFIG_VERSIONS_DIR = orig_versions
        promo_module.CURRENT_CONFIG_PATH = orig_current

    # -- Gate: confirmation ------------------------------------------------

    def test_promote_with_correct_confirmation(self, db: sqlite3.Connection, seed_promotable: dict):
        """``promote_config`` succeeds when confirmation equals config_id."""
        result = promote_config(
            seed_promotable["config_id"], db_conn=db,
            confirmation=seed_promotable["config_id"],
        )
        assert result["status"] == "promoted"
        assert result["config_id"] == seed_promotable["config_id"]

    def test_promote_rejects_wrong_confirmation(self, db: sqlite3.Connection, seed_promotable: dict):
        """``promote_config`` raises ``ValueError`` when confirmation does not match."""
        with pytest.raises(ValueError, match="Confirmation string does not match"):
            promote_config(
                seed_promotable["config_id"], db_conn=db,
                confirmation="wrong_confirmation",
            )

    def test_promote_rejects_nonexistent_config(self, db: sqlite3.Connection):
        """``promote_config`` raises ``ValueError`` when config does not exist."""
        with pytest.raises(ValueError, match="Config not found"):
            promote_config("nonexistent", db_conn=db, confirmation="nonexistent")

    # -- Gate: completed tune run ------------------------------------------

    def test_promote_rejects_without_completed_tune_run(
        self, db: sqlite3.Connection, seed_promotable: dict,
    ):
        """``promote_config`` raises ``ValueError`` when no completed tune run exists."""
        cid = seed_promotable["config_id"]
        # Change the tune run status from completed to running
        db.execute(
            "UPDATE experiment_runs SET status='running' WHERE run_id=?",
            (seed_promotable["tune_run_id"],),
        )
        db.commit()

        with pytest.raises(ValueError, match="completed tune run"):
            promote_config(cid, db_conn=db, confirmation=cid)

    # -- Gate: completed holdout run ---------------------------------------

    def test_promote_rejects_without_completed_holdout_run(
        self, db: sqlite3.Connection, seed_promotable: dict,
    ):
        """``promote_config`` raises ``ValueError`` when no completed holdout run exists."""
        cid = seed_promotable["config_id"]
        db.execute(
            "UPDATE experiment_runs SET status='running' WHERE run_id=?",
            (seed_promotable["holdout_run_id"],),
        )
        db.commit()

        with pytest.raises(ValueError, match="completed holdout run"):
            promote_config(cid, db_conn=db, confirmation=cid)

    # -- Gate: completed blind review --------------------------------------

    def test_promote_rejects_without_completed_ab_session(
        self, db: sqlite3.Connection, seed_promotable: dict,
    ):
        """``promote_config`` raises ``ValueError`` when no completed AB session exists."""
        cid = seed_promotable["config_id"]
        # Change AB session status from completed to active
        db.execute(
            "UPDATE ab_sessions SET status='active'",
        )
        db.commit()

        with pytest.raises(ValueError, match="completed blind"):
            promote_config(cid, db_conn=db, confirmation=cid)

    # -- Gate: export_integrity --------------------------------------------

    def test_promote_rejects_low_export_integrity(
        self, db: sqlite3.Connection, seed_promotable: dict,
    ):
        """``promote_config`` raises ``ValueError`` when export_integrity is below threshold."""
        cid = seed_promotable["config_id"]
        # Set export_integrity to a very low value
        db.execute(
            "UPDATE metric_values SET value=? WHERE metric_name='export_integrity'",
            (PROMOTION_GATE_THRESHOLD - 0.1,),
        )
        db.commit()

        with pytest.raises(ValueError, match="below gate"):
            promote_config(cid, db_conn=db, confirmation=cid)

    # -- Side effects: versioned config file -------------------------------

    def test_promote_creates_versioned_config_file(
        self, db: sqlite3.Connection, seed_promotable: dict, tmp_path: Path,
    ):
        """``promote_config`` writes a versioned config file to config_versions/."""
        cid = seed_promotable["config_id"]
        result = promote_config(cid, db_conn=db, confirmation=cid)

        versions_dir = tmp_path / "config_versions"
        assert versions_dir.is_dir()
        files = list(versions_dir.iterdir())
        assert len(files) >= 1

        # Verify content
        with open(files[0], encoding="utf-8") as f:
            data = json.load(f)
        assert data["config_id"] == cid
        assert "config_json" in data
        assert "scorecard" in data
        assert "promoted_at" in data

    # -- Side effects: current_config.json ---------------------------------

    def test_promote_creates_current_config_json(
        self, db: sqlite3.Connection, seed_promotable: dict, tmp_path: Path,
    ):
        """``promote_config`` writes current_config.json atomically."""
        cid = seed_promotable["config_id"]
        promote_config(cid, db_conn=db, confirmation=cid)

        current_path = tmp_path / "current_config.json"
        assert current_path.is_file()
        with open(current_path, encoding="utf-8") as f:
            data = json.load(f)
        assert data["config_id"] == cid

    # -- Side effects: champion_history ------------------------------------

    def test_promote_records_champion_history(
        self, db: sqlite3.Connection, seed_promotable: dict,
    ):
        """``promote_config`` inserts a 'promote' row in champion_history."""
        cid = seed_promotable["config_id"]
        promote_config(cid, db_conn=db, confirmation=cid)

        row = db.execute(
            "SELECT * FROM champion_history WHERE config_id=? AND action='promote'",
            (cid,),
        ).fetchone()
        assert row is not None
        assert row["config_id"] == cid
        assert row["action"] == "promote"

    # -- Scorecard in result -----------------------------------------------

    def test_promote_returns_scorecard(
        self, db: sqlite3.Connection, seed_promotable: dict,
    ):
        """``promote_config`` returns a scorecard with aggregate metrics."""
        cid = seed_promotable["config_id"]
        result = promote_config(cid, db_conn=db, confirmation=cid)

        assert "scorecard" in result
        assert "export_integrity" in result["scorecard"]
        integrity = result["scorecard"]["export_integrity"]
        assert integrity["mean"] > 0
        assert integrity["count"] >= 1
        assert "min" in integrity
        assert "max" in integrity


class TestRollback:
    """``rollback`` — champion rollback."""

    @pytest.fixture
    def db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        apply_quality_schema(conn)
        return conn

    @pytest.fixture
    def seeded_two_promotions(
        self, db: sqlite3.Connection, tmp_path: Path,
    ):
        """Seed DB with two promoted configs.

        Promotes cfg_a first, then cfg_b second, so cfg_b is current.
        Returns dict with cfg_a, cfg_b details.
        Patches paths to tmp_path.
        """
        import app.quality_lab.promotion as promo_module
        orig_versions = promo_module.CONFIG_VERSIONS_DIR
        orig_current = promo_module.CURRENT_CONFIG_PATH
        promo_module.CONFIG_VERSIONS_DIR = str(tmp_path / "config_versions")
        promo_module.CURRENT_CONFIG_PATH = str(tmp_path / "current_config.json")

        cid_a = _seed_config(db, config_id="cfg_a_test")
        cid_b = _seed_config(db, config_id="cfg_b_test")
        mid = _seed_manifest(db, n_items=2)
        tune_a = _seed_run(db, mid, cid_a, split="tune")
        holdout_a = _seed_run(db, mid, cid_a, split="holdout")
        _seed_metric(db, tune_a, metric_name="export_integrity", value=1.0)
        _seed_metric(db, holdout_a, metric_name="export_integrity", value=1.0)
        _seed_ab_session(db, tune_a, holdout_a)

        tune_b = _seed_run(db, mid, cid_b, split="tune")
        holdout_b = _seed_run(db, mid, cid_b, split="holdout")
        _seed_metric(db, tune_b, metric_name="export_integrity", value=1.0)
        _seed_metric(db, holdout_b, metric_name="export_integrity", value=1.0)
        _seed_ab_session(db, tune_b, holdout_b)

        promote_config(cid_a, db_conn=db, confirmation=cid_a)
        promote_config(cid_b, db_conn=db, confirmation=cid_b)

        yield {
            "cfg_a": cid_a,
            "cfg_b": cid_b,
        }

        promo_module.CONFIG_VERSIONS_DIR = orig_versions
        promo_module.CURRENT_CONFIG_PATH = orig_current

    def test_rollback_sets_previous_as_current(
        self, db: sqlite3.Connection, seeded_two_promotions: dict, tmp_path: Path,
    ):
        """``rollback`` sets the prior champion config as current champion."""
        result = rollback(db_conn=db)

        assert result["status"] == "rolled_back"
        assert result["config_id"] == seeded_two_promotions["cfg_a"]

        # Verify current_config.json
        current_path = tmp_path / "current_config.json"
        with open(current_path, encoding="utf-8") as f:
            data = json.load(f)
        assert data["config_id"] == seeded_two_promotions["cfg_a"]

    def test_rollback_records_rollback_event(
        self, db: sqlite3.Connection, seeded_two_promotions: dict,
    ):
        """``rollback`` inserts a 'rollback' row in champion_history (does not delete promote rows)."""
        # Count promote rows before
        promote_count_before = db.execute(
            "SELECT COUNT(*) AS cnt FROM champion_history WHERE action='promote'"
        ).fetchone()["cnt"]

        rollback(db_conn=db)

        # Rollback row created
        rollback_row = db.execute(
            "SELECT * FROM champion_history WHERE action='rollback'"
        ).fetchone()
        assert rollback_row is not None

        # Promote rows preserved
        promote_count_after = db.execute(
            "SELECT COUNT(*) AS cnt FROM champion_history WHERE action='promote'"
        ).fetchone()["cnt"]
        assert promote_count_after == promote_count_before

    def test_rollback_raises_when_no_previous(
        self, db: sqlite3.Connection, tmp_path: Path,
    ):
        """``rollback`` raises ``ValueError`` when there is no previous champion."""
        import app.quality_lab.promotion as promo_module
        promo_module.CONFIG_VERSIONS_DIR = str(tmp_path / "config_versions")
        promo_module.CURRENT_CONFIG_PATH = str(tmp_path / "current_config.json")

        cid = _seed_config(db)
        mid = _seed_manifest(db)
        tune_rid = _seed_run(db, mid, cid, split="tune")
        holdout_rid = _seed_run(db, mid, cid, split="holdout")
        _seed_metric(db, tune_rid, metric_name="export_integrity", value=1.0)
        _seed_metric(db, holdout_rid, metric_name="export_integrity", value=1.0)
        _seed_ab_session(db, tune_rid, holdout_rid)

        promote_config(cid, db_conn=db, confirmation=cid)

        # Only one promote event, no previous
        with pytest.raises(ValueError, match="No previous champion"):
            rollback(db_conn=db)

    def test_rollback_raises_when_no_champion(
        self, db: sqlite3.Connection, tmp_path: Path,
    ):
        """``rollback`` raises ``ValueError`` when there is no current champion."""
        import app.quality_lab.promotion as promo_module
        promo_module.CURRENT_CONFIG_PATH = str(tmp_path / "current_config.json")
        promo_module.CONFIG_VERSIONS_DIR = str(tmp_path / "config_versions")

        with pytest.raises(ValueError, match="No current champion"):
            rollback(db_conn=db)


class TestListChampionHistory:
    """``list_champion_history`` — champion history listing."""

    @pytest.fixture
    def db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        apply_quality_schema(conn)
        return conn

    def test_returns_empty_when_no_history(self, db: sqlite3.Connection):
        """``list_champion_history`` returns an empty list when no history exists."""
        history = list_champion_history(db_conn=db)
        assert history == []

    def test_returns_ordered_history(
        self, db: sqlite3.Connection, tmp_path: Path,
    ):
        """``list_champion_history`` returns events in descending order."""
        import app.quality_lab.promotion as promo_module
        promo_module.CONFIG_VERSIONS_DIR = str(tmp_path / "config_versions")
        promo_module.CURRENT_CONFIG_PATH = str(tmp_path / "current_config.json")

        cid = _seed_config(db)
        mid = _seed_manifest(db)
        tune_rid = _seed_run(db, mid, cid, split="tune")
        holdout_rid = _seed_run(db, mid, cid, split="holdout")
        _seed_metric(db, tune_rid, metric_name="export_integrity", value=1.0)
        _seed_metric(db, holdout_rid, metric_name="export_integrity", value=1.0)
        _seed_ab_session(db, tune_rid, holdout_rid)

        promote_config(cid, db_conn=db, confirmation=cid)
        promote_config(cid, db_conn=db, confirmation=cid)

        history = list_champion_history(db_conn=db)
        assert len(history) == 2
        assert history[0]["action"] == "promote"
        assert history[1]["action"] == "promote"
