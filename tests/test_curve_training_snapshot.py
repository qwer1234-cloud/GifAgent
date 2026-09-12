"""Tests for curve training manifests, immutable snapshots, and L2 logistic regression."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from app.services.curve_training import (
    train_draft,
    validate_training_manifest,
)


def test_source_leakage_blocks_training_snapshot():
    assert "source_overlap" in validate_training_manifest({
        "train_sources": ["a"],
        "development_sources": ["b"],
        "holdout_sources": ["a"],
        "feature_version": "curve-source-v1",
    })

    assert "source_overlap" in validate_training_manifest({
        "train_sources": ["a"],
        "development_sources": ["a"],
        "holdout_sources": ["c"],
        "feature_version": "curve-source-v1",
    })


def test_valid_manifest_passes():
    manifest = {
        "train_sources": ["src_1", "src_2"],
        "development_sources": ["src_3"],
        "holdout_sources": ["src_4", "src_5"],
        "feature_version": "curve-source-v1",
        "extractor_sha256": "0" * 64,
        "rows": [{"candidate_id": "c1", "source": "src_1", "rating": "like", "features": {"f1": 1.0}}],
    }
    assert validate_training_manifest(manifest) == []


def test_train_draft_num_py_l2(tmp_path: Path):
    snapshot_dir = tmp_path / "snapshot"
    output_dir = tmp_path / "output"
    snapshot_dir.mkdir()
    output_dir.mkdir()

    # Create synthetic training and holdout rows
    train_rows = [
        {"candidate_id": f"c_pos_{i}", "source_video_sha256": f"src_train_{i%2}", "label": 1, "features": {"extension_mean": 1.5 + 0.1*i, "torso_speed_p95": 0.8}}
        for i in range(10)
    ] + [
        {"candidate_id": f"c_neg_{i}", "source_video_sha256": f"src_train_{i%2}", "label": 0, "features": {"extension_mean": 0.2 + 0.05*i, "torso_speed_p95": 0.1}}
        for i in range(10)
    ]

    manifest = {
        "feature_version": "curve-source-v1",
        "extractor_sha256": "f" * 64,
        "train_sources": ["src_train_0", "src_train_1"],
        "development_sources": ["src_dev_0"],
        "holdout_sources": ["src_holdout_0"],
        "train_rows": train_rows,
        "holdout_rows": [],
    }

    manifest_file = snapshot_dir / "manifest.json"
    manifest_file.write_text(json.dumps(manifest), encoding="utf-8")

    result = train_draft(snapshot_dir, output_dir, feature_version="curve-source-v1")
    assert result["status"] in ("DRAFT", "NEEDS_ATTENTION")
    assert "model_version" in result
    assert (output_dir / "model.json").exists()


# ── Task 0 regression (R6): source roles recomputed from row facts ──────────


def test_manifest_rows_must_match_declared_source_roles():
    """R6: declared source sets must be recomputed from the actual rows; a
    holdout source smuggled into train_rows must be rejected."""
    manifest = {
        "feature_version": "curve-source-v1",
        "train_sources": ["train"],
        "development_sources": ["dev"],
        "holdout_sources": ["holdout"],
        "train_rows": [{"source_video_sha256": "holdout"}],
    }
    assert "source_role_mismatch" in validate_training_manifest(manifest)
