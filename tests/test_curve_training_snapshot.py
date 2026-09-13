"""Tests for curve training manifests, immutable snapshots, and L2 logistic regression."""
from __future__ import annotations

import hashlib as _hashlib
import json as _json
import json
from pathlib import Path

import numpy as np
import pytest

from app.services.curve_source_features import CURVE_SOURCE_FEATURE_NAMES
from app.services.curve_training import (
    register_draft,
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


def _v2_feature_row(candidate_id: str, source: str, label: int) -> dict:
    return {
        "candidate_id": candidate_id,
        "source_video_sha256": source,
        "label": label,
        "preexport_key": "a" * 64,
        "start_us": 1_000_000,
        "end_us": 4_000_000,
        "features": {name: 0.5 for name in CURVE_SOURCE_FEATURE_NAMES},
        "extractor_sha256": "0" * 64,
        "artifact_sha256": "b" * 64,
        "baseline_score": 0.7,
        "event_id": f"ev-{candidate_id}",
        "event_rating": "like" if label == 1 else "dislike",
        "event_created_at": "2026-09-01T00:00:00Z",
        "event_content_sha256": "c" * 64,
    }


def test_valid_manifest_passes():
    from app.services.curve_source_features import CURVE_SOURCE_FEATURE_NAMES

    manifest = {
        "manifest_version": "curve-training-manifest-v2",
        "train_sources": ["src_1", "src_2"],
        "development_sources": ["src_3"],
        "holdout_sources": ["src_4"],
        "feature_version": "curve-source-v1",
        "feature_names": list(CURVE_SOURCE_FEATURE_NAMES),
        "extractor_sha256": "0" * 64,
        "train_rows": [
            _v2_feature_row("c1", "src_1", 1),
            _v2_feature_row("c2", "src_2", 0),
        ],
        "development_rows": [_v2_feature_row("c3", "src_3", 1)],
        "holdout_rows": [_v2_feature_row("c4", "src_4", 0)],
    }
    assert validate_training_manifest(manifest) == []


def test_undeclared_row_source_is_refused():
    manifest = {
        "manifest_version": "curve-training-manifest-v2",
        "train_sources": ["src_1"],
        "development_sources": [],
        "holdout_sources": [],
        "feature_version": "curve-source-v1",
        "train_rows": [_v2_feature_row("c1", "unknown-src", 1)],
        "development_rows": [],
        "holdout_rows": [],
    }
    assert "source_role_mismatch" in validate_training_manifest(manifest)


def _v2_manifest(tmp_path: Path) -> tuple[Path, dict]:
    from app.services.curve_source_features import CURVE_SOURCE_FEATURE_NAMES

    train_rows = []
    for i in range(10):
        feats = {name: 1.5 + 0.1 * i for name in CURVE_SOURCE_FEATURE_NAMES}
        feats["torso_speed_p95"] = 0.8
        row = _v2_feature_row(f"c_pos_{i}", f"src_train_{i % 2}", 1)
        row["features"] = feats
        train_rows.append(row)
    for i in range(10):
        feats = {name: 0.2 + 0.05 * i for name in CURVE_SOURCE_FEATURE_NAMES}
        feats["torso_speed_p95"] = 0.1
        row = _v2_feature_row(f"c_neg_{i}", f"src_train_{i % 2}", 0)
        row["features"] = feats
        train_rows.append(row)
    dev_rows = [_v2_feature_row("c_dev_0", "src_dev_0", 1)]
    holdout_rows = [_v2_feature_row("c_hold_0", "src_hold_0", 0)]

    manifest = {
        "manifest_version": "curve-training-manifest-v2",
        "feature_version": "curve-source-v1",
        "feature_names": list(CURVE_SOURCE_FEATURE_NAMES),
        "extractor_sha256": "f" * 64,
        "train_sources": ["src_train_0", "src_train_1"],
        "development_sources": ["src_dev_0"],
        "holdout_sources": ["src_hold_0"],
        "train_rows": train_rows,
        "development_rows": dev_rows,
        "holdout_rows": holdout_rows,
    }
    snapshot_dir = tmp_path / "snapshot"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    (snapshot_dir / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True), encoding="utf-8"
    )
    return snapshot_dir, manifest


def test_train_draft_num_py_l2(tmp_path: Path):
    snapshot_dir, _manifest = _v2_manifest(tmp_path)
    output_dir = tmp_path / "output"

    result = train_draft(snapshot_dir, output_dir, feature_version="curve-source-v1")
    assert result["status"] == "DRAFT"
    assert result["model_version"]
    assert result["model_path"] and Path(result["model_path"]).is_file()
    assert result["model_sha256"]

    import sqlite3 as _sqlite3

    from app.services.preference_schema import apply_preference_schema

    conn = _sqlite3.connect(":memory:")
    apply_preference_schema(conn)
    model_version = register_draft(conn, result)
    assert model_version == result["model_version"]
    row = conn.execute(
        "SELECT status, holdout_row_count FROM curve_preference_models "
        "WHERE model_version = ?",
        (model_version,),
    ).fetchone()
    assert row[0] == "DRAFT"
    assert row[1] == 1


def test_train_draft_refuses_incomplete_features_and_missing_class(tmp_path: Path):
    snapshot_dir, manifest = _v2_manifest(tmp_path)
    for row in manifest["train_rows"]:
        row["features"].pop("verticality_mean")
    (snapshot_dir / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True), encoding="utf-8"
    )
    result = train_draft(snapshot_dir, tmp_path / "out-1")
    assert result["status"] == "NEEDS_ATTENTION"
    assert result["error_code"] == "feature_set_incomplete"

    snapshot_dir2, manifest2 = _v2_manifest(tmp_path)
    for row in manifest2["train_rows"]:
        row["label"] = 1
    (snapshot_dir2 / "manifest.json").write_text(
        json.dumps(manifest2, sort_keys=True), encoding="utf-8"
    )
    result2 = train_draft(snapshot_dir2, tmp_path / "out-2")
    assert result2["status"] == "NEEDS_ATTENTION"
    assert result2["error_code"] == "insufficient_class_diversity"


def test_holdout_labels_are_never_read_by_the_trainer(tmp_path: Path):
    """Mutating ONLY holdout labels changes the manifest hash but cannot
    change the trained DRAFT (the trainer never reads holdout labels)."""
    snapshot_a, manifest_a = _v2_manifest(tmp_path / "a")
    result_a = train_draft(snapshot_a, tmp_path / "out-a")

    snapshot_b_dir = tmp_path / "b" / "snapshot"
    snapshot_b_dir.mkdir(parents=True)
    manifest_b = json.loads(json.dumps(manifest_a))
    for row in manifest_b["holdout_rows"]:
        row["label"] = 1  # tamper holdout labels only
    (snapshot_b_dir / "manifest.json").write_text(
        json.dumps(manifest_b, sort_keys=True), encoding="utf-8"
    )
    result_b = train_draft(snapshot_b_dir, tmp_path / "out-b")

    assert result_a["status"] == "DRAFT" and result_b["status"] == "DRAFT"
    artifact_a = json.loads(Path(result_a["model_path"]).read_text(encoding="utf-8"))
    artifact_b = json.loads(Path(result_b["model_path"]).read_text(encoding="utf-8"))
    assert artifact_a["coefficients_json"] == artifact_b["coefficients_json"]
    assert artifact_a["intercept"] == artifact_b["intercept"]


def test_tampering_any_training_fact_changes_manifest_sha(tmp_path: Path):
    from app.services.curve_preference import canonical_json_sha256

    _snapshot_dir, manifest = _v2_manifest(tmp_path)
    base_sha = canonical_json_sha256(manifest)

    def mutated(change):
        clone = json.loads(json.dumps(manifest))
        change(clone)
        return canonical_json_sha256(clone)

    assert mutated(lambda m: m["train_rows"][0]["features"].__setitem__("extension_mean", 9.9)) != base_sha
    assert mutated(lambda m: m["train_rows"][0].__setitem__("baseline_score", 0.1)) != base_sha
    assert mutated(lambda m: m["train_rows"][0].__setitem__("event_id", "ev-tampered")) != base_sha
    assert mutated(lambda m: m["train_rows"][0].__setitem__("source_video_sha256", "other-src")) != base_sha
    assert mutated(lambda m: m["train_rows"][0].__setitem__("artifact_sha256", "d" * 64)) != base_sha
    assert mutated(lambda m: m["train_rows"][0].__setitem__("label", 0)) != base_sha


def test_build_training_snapshot_from_row_facts(tmp_path: Path):
    """End-to-end: events + publications + capture payloads + source features
    produce a snapshot whose roles are recomputed from the rows."""
    import sqlite3 as _sqlite3

    from app.services.candidate_capture import persist_capture
    from app.services.preference_schema import apply_preference_schema
    from app.services.curve_training import build_training_snapshot
    from app.services.curve_source_features import CURVE_SOURCE_FEATURE_NAMES

    conn = _sqlite3.connect(":memory:")
    apply_preference_schema(conn)

    def add_sample(i: int, rating: str, source: str) -> None:
        import base64 as _b64

        import numpy as _np

        from app.services.reranker import preference_embedding_text
        from app.services.vector_math import vector_to_blob

        cid = f"cand-{i:02d}"
        conn.execute(
            "INSERT INTO candidate_gifs (candidate_id, source_run_id, "
            "source_run_candidate_id, source_video_sha256, source_video_path, "
            "start_sec, end_sec, status) VALUES (?, 'run', ?, ?, '/v.mp4', 0.0, 5.0, 'candidate')",
            (cid, f"rc-{cid}", source),
        )
        conn.execute(
            "INSERT INTO preference_events (event_id, target_type, target_id, "
            "rating, source_video_sha256, scenario_keys_json, created_at, event_kind) "
            "VALUES (?, 'candidate_gif', ?, ?, ?, '[]', ?, 'feedback')",
            (f"ev-{i:02d}", cid, rating, source, f"2026-09-01T00:{i % 60:02d}:00Z"),
        )
        pref_text = preference_embedding_text(
            caption=f"synthetic clip {i}", emotion="joy", scene_type=""
        )
        pref_vec = _np.asarray(
            [(i % 7) / 7.0] + [0.0] * 767, dtype=_np.float32
        )
        payload = {
            "run_id": "run-snap",
            "preexport_key": f"{i:064x}",
            "capture_version": "capture-v1",
            "source_video_sha256": source,
            "start_us": 1_000_000 + i,
            "end_us": 4_000_000 + i,
            "anchor_us": 2_500_000,
            "baseline_score": 0.5 + i / 100.0,
            "capture_status": "CAPTURED",
            "preference_identity": {
                "embedding_text": pref_text,
                "embedding_text_sha256": _hashlib.sha256(
                    pref_text.encode("utf-8")
                ).hexdigest(),
                "embedding_model": "nomic-embed-text:latest",
                "scenario_keys": [],
                "vector_float32_b64": _b64.b64encode(pref_vec.tobytes()).decode(
                    "ascii"
                ),
                "vector_sha256": _hashlib.sha256(pref_vec.tobytes()).hexdigest(),
                "scoring_input_committed": True,
            },
        }
        persisted = persist_capture(conn, payload, tmp_path / "evidence")
        conn.execute(
            "INSERT INTO candidate_analysis_publications "
            "(publication_id, record_id, candidate_id, gif_sha256) VALUES (?, ?, ?, ?)",
            (f"pub-{i:02d}", persisted.record_id, cid, f"{i:064x}"),
        )
        features = {name: 0.4 + (i % 10) / 25.0 for name in CURVE_SOURCE_FEATURE_NAMES}
        conn.execute(
            "INSERT INTO curve_features (candidate_id, feature_version, "
            "availability, features_json, extractor_sha256, artifact_sha256, "
            "wall_clock_ms) VALUES (?, 'curve-source-v1', 'AVAILABLE', ?, 'ex', ?, 1.0)",
            (cid, json.dumps(features), f"{i:064x}"),
        )
        conn.execute(
            "INSERT INTO candidate_vectors (candidate_id, vector_type, "
            "embedding_model, embedding_dim, vector_blob, normalized, source_text_hash) "
            "VALUES (?, 'clip', 'nomic-embed-text:latest', 768, ?, 1, ?)",
            (
                cid,
                vector_to_blob(pref_vec, embedding_dim=768),
                _hashlib.sha256(pref_text.encode("utf-8")).hexdigest(),
            ),
        )

    train_source = "a" * 64
    holdout_source = "b" * 64
    for i in range(6):
        source = holdout_source if i % 2 == 1 else train_source
        add_sample(i, "like" if i % 2 == 0 else "dislike", source)
    conn.execute(
        "INSERT INTO preference_profile_current (slot, profile_version) "
        "VALUES ('current', 'profile-v1')"
    )
    conn.commit()

    snapshot = build_training_snapshot(
        conn,
        {"split_seed": 7, "final_holdout_source_videos": [holdout_source]},
        tmp_path / "snapshots",
    )
    manifest = json.loads(
        (Path(snapshot.snapshot_dir) / "manifest.json").read_text(encoding="utf-8")
    )
    assert snapshot.train_row_count + snapshot.development_row_count >= 3
    assert snapshot.final_holdout_row_count == 3  # holdout-source rows (i=1,3,5)
    assert manifest["holdout_sources"] == [holdout_source]
    for row in manifest["holdout_rows"]:
        assert row["source_video_sha256"] == holdout_source
    for row in manifest["train_rows"] + manifest["development_rows"]:
        assert row["source_video_sha256"] != holdout_source
        assert row["source_video_sha256"] in (
            manifest["train_sources"] + manifest["development_sources"]
        )
    # Same source never splits across roles.
    train_dev = {}
    for row in manifest["train_rows"]:
        train_dev.setdefault(row["source_video_sha256"], set()).add("train")
    for row in manifest["development_rows"]:
        train_dev.setdefault(row["source_video_sha256"], set()).add("dev")
    assert all(len(v) == 1 for v in train_dev.values())


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


def test_ambiguous_publication_fails_closed(tmp_path: Path):
    """P2 remediation: a candidate linked to multiple analysis records has
    ambiguous baseline/window provenance and is excluded, never guessed."""
    import sqlite3 as _sqlite3

    from app.services.candidate_capture import persist_capture
    from app.services.curve_training import build_training_snapshot
    from app.services.curve_source_features import CURVE_SOURCE_FEATURE_NAMES
    from app.services.preference_schema import apply_preference_schema

    conn = _sqlite3.connect(":memory:")
    apply_preference_schema(conn)

    cid = "cand-amb-0"
    source = "a" * 64
    conn.execute(
        "INSERT INTO candidate_gifs (candidate_id, source_run_id, "
        "source_run_candidate_id, source_video_sha256, source_video_path, "
        "start_sec, end_sec, status) VALUES (?, 'run', 'rc', ?, '/v.mp4', 0.0, 5.0, 'candidate')",
        (cid, source),
    )
    conn.execute(
        "INSERT INTO preference_events (event_id, target_type, target_id, "
        "rating, source_video_sha256, scenario_keys_json, created_at, event_kind) "
        "VALUES ('ev-amb', 'candidate_gif', ?, 'like', ?, '[]', "
        "'2026-09-01T00:00:00Z', 'feedback')",
        (cid, source),
    )
    for record_index in range(2):
        payload = {
            "run_id": f"run-{record_index}",
            "preexport_key": f"{record_index:064x}",
            "capture_version": "capture-v1",
            "source_video_sha256": source,
            "start_us": 1_000_000 + record_index,
            "end_us": 4_000_000 + record_index,
            "anchor_us": 2_500_000,
            "baseline_score": 0.5 + record_index / 10.0,
            "capture_status": "CAPTURED",
        }
        persisted = persist_capture(conn, payload, tmp_path / "evidence")
        conn.execute(
            "INSERT INTO candidate_analysis_publications "
            "(publication_id, record_id, candidate_id, gif_sha256) VALUES (?, ?, ?, ?)",
            (f"pub-{record_index}", persisted.record_id, cid, f"{record_index:064x}"),
        )
        if record_index == 0:
            conn.execute(
                "INSERT INTO curve_features (candidate_id, feature_version, "
                "availability, features_json, extractor_sha256, artifact_sha256, wall_clock_ms) "
                "VALUES (?, 'curve-source-v1', 'AVAILABLE', ?, 'ex', ?, 1.0)",
                (cid, _json.dumps({n: 0.5 for n in CURVE_SOURCE_FEATURE_NAMES}), f"{record_index:064x}"),
            )
    conn.execute(
        "INSERT INTO preference_profile_current (slot, profile_version) "
        "VALUES ('current', 'profile-v1')"
    )
    conn.commit()

    snapshot = build_training_snapshot(conn, {"split_seed": 1}, tmp_path / "snap")
    manifest = _json.loads(
        (Path(snapshot.snapshot_dir) / "manifest.json").read_text(encoding="utf-8")
    )
    total_rows = (
        len(manifest["train_rows"])
        + len(manifest["development_rows"])
        + len(manifest["holdout_rows"])
        + len(manifest["reference_rows"])
    )
    assert total_rows == 0
    assert any(
        e["reason"] == "ambiguous_publication" for e in manifest["exclusions"]
    )


def test_mixed_extractor_identities_hard_fail_training(tmp_path: Path):
    """P2 remediation: samples from two extractor identities never train; the
    trainer refuses the snapshot instead of producing a mixed-domain DRAFT."""
    snapshot_dir, manifest = _v2_manifest(tmp_path)
    manifest["train_rows"][0]["extractor_sha256"] = "9" * 64
    manifest["extractor_sha256"] = ""
    (snapshot_dir / "manifest.json").write_text(
        _json.dumps(manifest, sort_keys=True), encoding="utf-8"
    )
    result = train_draft(snapshot_dir, tmp_path / "out")
    assert result["status"] == "NEEDS_ATTENTION"
    assert result["error_code"] == "invalid_manifest"


def test_capture_vector_identity_is_a_training_gate(tmp_path: Path):
    """P2 remediation: a sample without a committed capture vector identity
    is excluded; an identity inconsistent with the formal ledger vector is
    excluded; a consistent identity trains."""
    import base64 as _b64

    import numpy as _np
    import sqlite3 as _sqlite3

    from app.services.candidate_capture import persist_capture
    from app.services.curve_training import build_training_snapshot
    from app.services.curve_source_features import (
        CURVE_SOURCE_FEATURE_NAMES,
        CurveWindow,
        window_input_hash,
    )
    from app.services.preference_schema import apply_preference_schema
    from app.services.reranker import preference_embedding_text
    from app.services.vector_math import vector_to_blob

    def build_db(
        capture_offset: int, ledger_offset: int, *, with_identity: bool
    ) -> _sqlite3.Connection:
        conn = _sqlite3.connect(":memory:")
        apply_preference_schema(conn)
        cid = "cand-gate-0"
        source = "a" * 64
        conn.execute(
            "INSERT INTO candidate_gifs (candidate_id, source_run_id, "
            "source_run_candidate_id, source_video_sha256, source_video_path, "
            "start_sec, end_sec, status) VALUES (?, 'run', 'rc', ?, '/v.mp4', 0.0, 5.0, 'candidate')",
            (cid, source),
        )
        conn.execute(
            "INSERT INTO preference_events (event_id, target_type, target_id, "
            "rating, source_video_sha256, scenario_keys_json, created_at, event_kind) "
            "VALUES ('ev-gate', 'candidate_gif', ?, 'like', ?, '[]', "
            "'2026-09-01T00:00:00Z', 'feedback')",
            (cid, source),
        )
        pref_text = preference_embedding_text(
            caption="synthetic clip", emotion="joy", scene_type=""
        )
        pref_vec = _np.asarray(
            [(capture_offset % 7) / 7.0] + [0.0] * 767, dtype=_np.float32
        )
        payload = {
            "run_id": "run-gate",
            "preexport_key": "a" * 64,
            "capture_version": "capture-v1",
            "source_video_sha256": source,
            "start_us": 1_000_000,
            "end_us": 4_000_000,
            "anchor_us": 2_500_000,
            "baseline_score": 0.8,
            "capture_status": "CAPTURED",
        }
        if with_identity:
            payload["preference_identity"] = {
                "embedding_text": pref_text,
                "embedding_text_sha256": _hashlib.sha256(
                    pref_text.encode("utf-8")
                ).hexdigest(),
                "embedding_model": "nomic-embed-text:latest",
                "scenario_keys": [],
                "vector_float32_b64": _b64.b64encode(pref_vec.tobytes()).decode(
                    "ascii"
                ),
                "vector_sha256": _hashlib.sha256(pref_vec.tobytes()).hexdigest(),
                "scoring_input_committed": True,
            }
            payload["source_feature"] = {
                "status": "AVAILABLE",
                "feature_version": "curve-source-v1",
                "extractor_sha256": "e" * 64,
                "features": {n: 0.5 for n in CURVE_SOURCE_FEATURE_NAMES},
                "actual_timestamps": [1.0, 2.0, 3.0],
                "input_hash": window_input_hash(
                    source,
                    CurveWindow(
                        preexport_key="a" * 64,
                        start_us=1_000_000,
                        end_us=4_000_000,
                    ),
                    extractor_sha256="e" * 64,
                ),
                "missing_reasons": [],
                "wall_clock_ms": 1.0,
            }
        persisted = persist_capture(conn, payload, tmp_path / "evidence")
        conn.execute(
            "INSERT INTO candidate_analysis_publications "
            "(publication_id, record_id, candidate_id, gif_sha256) "
            "VALUES ('pub-gate', ?, ?, ?)",
            (persisted.record_id, cid, "f" * 64),
        )
        features = {n: 0.5 for n in CURVE_SOURCE_FEATURE_NAMES}
        conn.execute(
            "INSERT INTO curve_features (candidate_id, feature_version, "
            "availability, features_json, extractor_sha256, artifact_sha256, wall_clock_ms) "
            "VALUES (?, 'curve-source-v1', 'AVAILABLE', ?, ?, ?, 1.0)",
            (cid, _json.dumps(features), "e" * 64, "f" * 64),
        )
        ledger_vec = _np.asarray(
            [(ledger_offset % 7) / 7.0] + [0.0] * 767, dtype=_np.float32
        )
        conn.execute(
            "INSERT INTO candidate_vectors (candidate_id, vector_type, "
            "embedding_model, embedding_dim, vector_blob, normalized, source_text_hash) "
            "VALUES (?, 'clip', 'nomic-embed-text:latest', 768, ?, 1, ?)",
            (
                cid,
                vector_to_blob(ledger_vec, embedding_dim=768),
                _hashlib.sha256(pref_text.encode("utf-8")).hexdigest(),
            ),
        )
        conn.execute(
            "INSERT INTO preference_profile_current (slot, profile_version) "
            "VALUES ('current', 'profile-v1')"
        )
        conn.commit()
        return conn

    # Missing identity -> excluded.
    conn = build_db(0, 0, with_identity=False)
    snapshot = build_training_snapshot(conn, {"split_seed": 1}, tmp_path / "s1")
    manifest = _json.loads(
        (Path(snapshot.snapshot_dir) / "manifest.json").read_text(encoding="utf-8")
    )
    assert any(
        e["reason"] == "preference_identity_missing" for e in manifest["exclusions"]
    )
    total = sum(
        len(manifest[k])
        for k in ("train_rows", "development_rows", "holdout_rows", "reference_rows")
    )
    assert total == 0

    # Consistent identity -> sample trains.
    conn_ok = build_db(0, 0, with_identity=True)
    snapshot_ok = build_training_snapshot(conn_ok, {"split_seed": 1}, tmp_path / "s2")
    manifest_ok = _json.loads(
        (Path(snapshot_ok.snapshot_dir) / "manifest.json").read_text(encoding="utf-8")
    )
    total_ok = sum(
        len(manifest_ok[k])
        for k in ("train_rows", "development_rows", "holdout_rows", "reference_rows")
    )
    assert total_ok == 1

    # Ledger vector inconsistent with the frozen capture vector -> excluded.
    conn_bad = build_db(0, 3, with_identity=True)
    snapshot_bad = build_training_snapshot(conn_bad, {"split_seed": 1}, tmp_path / "s3")
    manifest_bad = _json.loads(
        (Path(snapshot_bad.snapshot_dir) / "manifest.json").read_text(encoding="utf-8")
    )
    assert any(
        e["reason"] == "preference_vector_mismatch" for e in manifest_bad["exclusions"]
    )
