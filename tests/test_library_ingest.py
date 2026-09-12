"""Pipeline published-GIF ingest into candidate_gifs + vectors."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import numpy as np
import pytest

from app.services.library_ingest import (
    ingest_published_clips,
    ingest_succeeded_gifs_into_library,
    library_ingest_enabled,
    library_text_from_clip,
    published_clip_to_run_candidate,
)
from app.services.preference_schema import apply_preference_schema


def _embed(text: str) -> list[float]:
    rng = np.random.default_rng(abs(hash(text)) % (2**32))
    vec = rng.random(768).astype(np.float32)
    return (vec / np.linalg.norm(vec)).tolist()


@pytest.fixture
def library_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_preference_schema(conn)
    return conn


def test_library_text_from_clip_reads_best_frame():
    payload = library_text_from_clip(
        {
            "best_frame": {
                "caption": "two people kissing",
                "emotional_core": "desire",
                "scene_type": "indoor",
            },
            "tags": ["kiss"],
            "gif_worthiness": 0.8,
        }
    )
    assert payload["vlm_summary"]["caption"] == "two people kissing"
    assert payload["vlm_summary"]["emotion"] == "desire"
    assert payload["tags"] == ["kiss"]
    assert payload["final_score"] == 0.8


def test_ingest_published_clips_writes_candidate_and_vector(library_db):
    clips = [
        {
            "clip_id": "clip-aa",
            "start_ts": 1.0,
            "end_ts": 5.0,
            "formal_path": "/exports/clip.gif",
            "parent_source": {"source_file_sha256": "ab" * 32},
            "library_text": {
                "vlm_summary": {
                    "caption": "indoor closeup",
                    "emotion": "joy",
                    "emotional_core": "joy",
                    "scene_type": "indoor",
                },
                "tags": ["closeup"],
                "gif_worthiness": 0.7,
                "final_score": 0.7,
            },
        }
    ]
    result = ingest_published_clips(
        library_db,
        video_path="/videos/a.mp4",
        clips=clips,
        embed_fn=_embed,
    )
    assert result == {"ingested": 1, "skipped": 0, "needs_repair": 0}
    assert library_db.execute("SELECT COUNT(*) FROM candidate_gifs").fetchone()[0] == 1
    assert library_db.execute("SELECT COUNT(*) FROM candidate_vectors").fetchone()[0] == 1
    sha = library_db.execute(
        "SELECT source_video_sha256 FROM candidate_gifs"
    ).fetchone()[0]
    assert sha == "ab" * 32


def test_ingest_skips_vector_when_library_text_empty(library_db):
    calls: list[str] = []

    def tracking(text: str) -> list[float]:
        calls.append(text)
        return _embed(text)

    ingest_published_clips(
        library_db,
        video_path="/videos/a.mp4",
        clips=[
            {
                "clip_id": "clip-empty",
                "start_ts": 0.0,
                "end_ts": 2.0,
                "formal_path": "/exports/empty.gif",
            }
        ],
        embed_fn=tracking,
    )
    assert calls == []
    assert library_db.execute("SELECT COUNT(*) FROM candidate_gifs").fetchone()[0] == 1
    assert library_db.execute("SELECT COUNT(*) FROM candidate_vectors").fetchone()[0] == 0


def test_library_ingest_enabled_respects_env(monkeypatch):
    monkeypatch.setenv("GIFAGENT_LIBRARY_INGEST", "0")
    assert library_ingest_enabled({"library_ingest_enabled": True}) is False
    monkeypatch.setenv("GIFAGENT_LIBRARY_INGEST", "1")
    assert library_ingest_enabled({"library_ingest_enabled": False}) is True


def test_ingest_succeeded_gifs_never_raises(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("GIFAGENT_LIBRARY_INGEST", "1")

    def boom():
        raise RuntimeError("db locked")

    monkeypatch.setattr("app.db.get_connection", boom)
    ingest_succeeded_gifs_into_library(
        video_path=str(tmp_path / "v.mp4"),
        clips=[{"clip_id": "x", "start_ts": 0, "end_ts": 1}],
        cfg={},
    )


def test_stage_materialize_ingests_when_enabled(tmp_path: Path, monkeypatch):
    from scripts.test_video_adaptive import _stage_materialize, extract_config
    import hashlib

    monkeypatch.setenv("GIFAGENT_LIBRARY_INGEST", "1")
    db_path = tmp_path / "library.db"

    def fake_get_connection():
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        apply_preference_schema(conn)
        return conn

    monkeypatch.setattr("app.db.get_connection", fake_get_connection)
    monkeypatch.setattr(
        "app.services.embedding.compute_text_embedding",
        _embed,
    )

    video = tmp_path / "source.mp4"
    video.write_bytes(b"video")
    gif = tmp_path / "clip.gif"
    gif.write_bytes(b"GIF89a")
    gif_sha = hashlib.sha256(gif.read_bytes()).hexdigest()
    quality_assessment = {
        "candidate_id": "clip-1",
        "evaluation_version": "quality-moe-v1",
        "config_hash": "1" * 64,
        "policy_version": "quality-moe-policy-v1",
        "recommended_decision": "KEEP_AS_IS",
        "effective_decision": "KEEP_AS_IS",
        "decision": "KEEP_AS_IS",
        "confidence": 0.9,
        "negative_signal_families": [],
        "hard_reasons": [],
        "hard_gate_context": {},
        "hard_gate_context_hash": hashlib.sha256(b"{}").hexdigest(),
        "repair": None,
        "reason_codes": [],
        "summary": "clean",
        "input_hash": "4" * 64,
        "evidence": [],
        "provenance": {"source_file_sha256": "5" * 64},
        "evidence_hashes": [],
        "selected_recipe_id": None,
        "current_quality": 0.55,
        "recoverable_quality": 0.78,
    }
    quality_lineage = {
        "quality_decision": "KEEP_AS_IS",
        "current_quality": 0.55,
        "recoverable_quality": 0.78,
        "repair_applied": False,
        "recommended_recipe_id": None,
        "recommended_recipe": None,
        "applied_recipe_id": None,
        "applied_recipe": None,
        "evidence_hashes": [],
        "config_hash": "1" * 64,
        "parent_source": {
            "candidate_id": "clip-1",
            "input_hash": "4" * 64,
            "source_file_sha256": "5" * 64,
            "video_path": str(video.resolve()),
            "start_ts": 1.0,
            "end_ts": 5.0,
        },
    }
    gif_manifest = tmp_path / "gif_clip_manifest.json"
    gif_manifest.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "stage": "gif_clip",
                "clip_id": "clip-1",
                "gif_path": str(gif),
                "gif_name": "clip.gif",
                "sha256": gif_sha,
                "duration_s": 4.0,
                "size_bytes": gif.stat().st_size,
                "status": "succeeded",
                "start_ts": 1.0,
                "end_ts": 5.0,
                "action_boundary_mode": "cv",
                "action_boundary_confidence": 0.8,
                "action_vlm_verified": False,
                "action_analysis_version": 1,
                "guarded_export_window": True,
                "quality_assessment": quality_assessment,
                "library_text": {
                    "vlm_summary": {
                        "caption": "pipeline closeup",
                        "emotion": "joy",
                        "emotional_core": "joy",
                        "scene_type": "indoor",
                    },
                    "tags": ["closeup"],
                    "gif_worthiness": 0.66,
                    "final_score": 0.66,
                },
                **quality_lineage,
            }
        ),
        encoding="utf-8",
    )
    gif_manifest_raw = gif_manifest.read_bytes()
    work_dir = tmp_path / "materialize-work"
    work_dir.mkdir()
    export_base = tmp_path / "formal"
    cfg = extract_config({"adaptive": {"potplayer_pbf_enabled": False}})
    result = _stage_materialize(
        str(video),
        str(tmp_path / "unused"),
        str(work_dir),
        cfg,
        inputs={
            "schema_version": 1,
            "stage": "materialize",
            "artifacts": {
                "gif_file": [
                    {
                        "path": str(gif),
                        "clip_id": "clip-1",
                        "sha256": gif_sha,
                        "size_bytes": gif.stat().st_size,
                    }
                ],
                "gif_clip_manifest": [
                    {
                        "path": str(gif_manifest),
                        "clip_id": "clip-1",
                        "sha256": hashlib.sha256(gif_manifest_raw).hexdigest(),
                        "size_bytes": len(gif_manifest_raw),
                    }
                ],
            },
            "stage_statuses": [],
        },
        config_data={"export_base_dir": str(export_base)},
    )
    assert result["outcome"] == "succeeded"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    assert conn.execute("SELECT COUNT(*) FROM candidate_gifs").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM candidate_vectors").fetchone()[0] == 1
    summary = conn.execute("SELECT vlm_summary_json FROM candidate_gifs").fetchone()[0]
    assert "pipeline closeup" in summary
    conn.close()


def test_published_clip_to_run_candidate_uses_formal_path():
    payload = published_clip_to_run_candidate(
        {
            "clip_id": "c1",
            "start_ts": 2.5,
            "end_ts": 8.0,
            "formal_path": "D:/exports/a.gif",
            "library_text": {
                "vlm_summary": {"caption": "x", "emotion": "", "scene_type": ""},
                "tags": [],
            },
        },
        video_path="D:/videos/a.mp4",
        source_video_sha256="aa" * 32,
    )
    assert payload["artifact_path"] == "D:/exports/a.gif"
    assert payload["start_sec"] == 2.5
    assert payload["end_sec"] == 8.0


def test_ingest_reuses_already_rated_relocated_clip(library_db, tmp_path):
    gif_name = "Vid@@@001_1000ms-2000ms.gif"
    repo = tmp_path / "data" / "exports" / "adaptive_test" / "Vid"
    dist = tmp_path / "dist" / "GifAgentUI" / "data" / "exports" / "adaptive_test" / "Vid"
    repo.mkdir(parents=True)
    dist.mkdir(parents=True)
    repo_gif = repo / gif_name
    dist_gif = dist / gif_name
    repo_gif.write_bytes(b"gif")
    dist_gif.write_bytes(b"gif")

    library_db.execute(
        """INSERT INTO candidate_gifs
           (candidate_id, source_run_id, source_run_candidate_id,
            source_video_sha256, source_video_path, start_sec, end_sec,
            artifact_path, preview_path, status, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "cand_fs_old",
            "filesystem-folder-import",
            "digest",
            "folder-sha",
            str(repo),
            1.0,
            2.0,
            str(repo_gif),
            str(repo_gif),
            "liked",
            "2026-09-01T00:00:00+00:00",
            "2026-09-01T00:00:00+00:00",
        ),
    )
    library_db.commit()

    result = ingest_published_clips(
        library_db,
        video_path=str(tmp_path / "Vid.mp4"),
        clips=[
            {
                "clip_id": "clip-new",
                "start_ts": 1.0,
                "end_ts": 2.0,
                "formal_path": str(dist_gif),
                "parent_source": {"source_file_sha256": "cd" * 32},
                "library_text": {
                    "vlm_summary": {
                        "caption": "relocated ingest",
                        "emotion": "joy",
                        "emotional_core": "joy",
                        "scene_type": "indoor",
                    },
                    "tags": ["closeup"],
                    "gif_worthiness": 0.7,
                    "final_score": 0.7,
                },
            }
        ],
        embed_fn=_embed,
    )

    assert result == {"ingested": 1, "skipped": 0, "needs_repair": 0}
    rows = library_db.execute(
        "SELECT candidate_id, status, vlm_summary_json FROM candidate_gifs"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["candidate_id"] == "cand_fs_old"
    assert rows[0]["status"] == "liked"
    assert "relocated ingest" in rows[0]["vlm_summary_json"]
