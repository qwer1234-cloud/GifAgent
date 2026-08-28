"""Direct vs staged adaptive parity harness (2026-08-28 plan Task 8).

Drives the same frozen config through ``run_pipeline`` (direct) and through
the eight shared ``_stage_*`` handlers (staged, in-process) with identical
mock VLM / ffprobe / embedding behavior, then compares the decisions the
plan locks: keep-gate survivors, merged clip windows, deduped candidate
identities, and export filenames/windows.

Uses only tmp_path fixtures; never touches ``data/exports``.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from app.pipeline import ranking as pipeline_ranking
from app.pipeline.stage_io import _load_manifest
from app.pipeline.stages import (
    discover as discover_stage,
    gif_clip as gif_clip_stage,
    rank_dedup as rank_dedup_stage,
    refine as refine_stage,
    sample as sample_stage,
    synthesize as synthesize_stage,
    vlm as vlm_stage,
)
from scripts import test_video_adaptive
from tests.test_adaptive_direct_transition import _cfg, _run_direct_pipeline_fixture


def _fake_rescore_factory(rescore_calls):
    """Mirror the direct fixture's action-rescore fake for the staged side."""
    real_score = test_video_adaptive._score_vlm_frame

    def fake_rescore(**kwargs):
        timestamp = kwargs.get("timestamp")
        is_action_rescore = timestamp == 12 or timestamp == 12.0
        if not is_action_rescore:
            return real_score(**kwargs)
        rescore_calls.append(kwargs)
        return ({
            "timestamp": kwargs["timestamp"], "path": kwargs["frame_path"],
            "caption": "rescored", "emotional_core": "joy",
            "gif_worthiness": 0.8, "aesthetic_notes": [], "reason": "clean",
        }, None)

    return fake_rescore


def _patch_llm(monkeypatch):
    import app.services.llm_client as llm_client

    def forbidden_llm(*_args, **_kwargs):
        raise AssertionError("parity harness must not call the LLM")

    monkeypatch.setattr(llm_client, "generate_llm_text", forbidden_llm)


def _run_staged_chain(root: Path, monkeypatch, *, max_output: int = 2) -> dict:
    """Run the eight stage handlers in-process, threading real manifests."""
    work = root / "work"
    frames = work / "frames"
    export = work / "exports"
    frames.mkdir(parents=True)
    export.mkdir(parents=True)
    video = root / "source.mp4"
    video.write_bytes(b"staged-source")

    cfg = _cfg()
    cfg["max_output"] = max_output
    config_data = {
        "vlm": {
            "provider": "ollama", "model": "llava:13b",
            "base_url": "http://staged-vlm.invalid",
            "manage_lifecycle": False, "launch_mode": "none",
            "retry_delay_s": 0.0,
        },
    }

    rescore_calls: list = []
    monkeypatch.setattr(
        rank_dedup_stage, "_score_vlm_frame", _fake_rescore_factory(rescore_calls)
    )

    class FakeEvidenceCache:
        def scan(self, *_args, **_kwargs):
            return object()

    monkeypatch.setattr(
        rank_dedup_stage, "TemporalEvidenceCache", FakeEvidenceCache
    )

    captured_exports: list = []

    def fake_export_attempt(**kwargs):
        captured_exports.append(kwargs)
        Path(kwargs["output_path"]).write_bytes(b"GIF89a")
        return SimpleNamespace(success=True, size_bytes=6, error=None)

    monkeypatch.setattr(
        gif_clip_stage, "run_gif_export_attempt", fake_export_attempt
    )

    ranker_inputs: list = []

    def fake_ranker(clips, _score):
        ranker_inputs.extend(clips)
        return sorted(clips, key=lambda clip: clip["gif_worthiness"], reverse=True)

    monkeypatch.setattr(
        pipeline_ranking, "rank_clips_for_export", fake_ranker
    )
    monkeypatch.setattr(
        pipeline_ranking,
        "compute_text_embedding",
        lambda text: (
            [1.0, 0.0]
            if text.startswith(("moment", "first"))
            else [0.0, 1.0]
        ),
    )

    def manifest_ref(path: Path, kind: str, stage_id: str) -> dict:
        raw = path.read_bytes()
        return {
            "path": str(path),
            "artifact_kind": kind,
            "stage_id": stage_id,
            "artifact_id": test_video_adaptive._hash_artifact_id(
                kind, str(path), stage_id=stage_id
            ),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "size_bytes": len(raw),
        }

    discover_out = discover_stage._stage_discover(str(video), str(work), cfg)
    discover_ref = manifest_ref(
        Path(discover_out["_artifacts"][0]["path"]), "discover_manifest", "staged"
    )

    sample_out = sample_stage._stage_sample(
        str(video), str(frames), str(work), cfg,
        {"discover_manifest": [discover_ref]}, config_data,
    )
    sample_ref = manifest_ref(
        Path(sample_out["_artifacts"][0]["path"]), "sample_manifest", "staged"
    )
    sample_manifest = _load_manifest(str(work), "sample")

    vlm_out = vlm_stage._stage_vlm(
        str(frames), str(work), cfg,
        {
            "sample_manifest": [sample_ref],
            "sample_frames": list(sample_manifest.get("frame_entries", [])),
        },
        config_data,
    )
    vlm_ref = manifest_ref(
        Path(vlm_out["_artifacts"][0]["path"]), "vlm_manifest", "staged"
    )
    vlm_manifest = _load_manifest(str(work), "vlm")

    refine_out = refine_stage._stage_refine(
        str(video), str(frames), str(work), cfg,
        {"vlm_manifest": [vlm_ref], "discover_manifest": [discover_ref]},
        config_data,
    )
    refine_ref = manifest_ref(
        Path(refine_out["_artifacts"][0]["path"]), "refine_manifest", "staged"
    )

    synth_out = synthesize_stage._stage_synthesize(
        str(work), cfg, {"refine_manifest": [refine_ref]}
    )
    synth_ref = manifest_ref(
        Path(synth_out["_artifacts"][0]["path"]), "synthesize_manifest", "staged"
    )

    rank_out = rank_dedup_stage._stage_rank_dedup(
        str(video), str(export), str(work), cfg,
        {"synthesize_manifest": [synth_ref]}, config_data,
    )
    rank_manifest = _load_manifest(str(work), "rank_dedup")
    rank_ref = manifest_ref(
        Path(rank_out["_artifacts"][0]["path"]), "rank_dedup_manifest", "staged"
    )

    gif_manifests = []
    # _write_rank_candidate_ledger hashed the ledger under the stage_id the
    # rank stage derived from config_data (no _stage_id -> standalone default).
    ledger_ref = manifest_ref(
        Path(rank_out["_artifacts"][1]["path"]),
        "rank_candidate_ledger",
        "standalone-rank-stage",
    )
    for clip in rank_manifest.get("clips", []):
        gif_clip_stage._stage_gif_clip(
            str(video), str(frames), str(export), str(work), cfg,
            clip_id=clip["clip_id"],
            inputs={
                "rank_dedup_manifest": [rank_ref],
                "rank_candidate_ledger": [ledger_ref],
                "synthesize_manifest": [synth_ref],
            },
        )
        gif_manifests.append(_load_manifest(str(work), f"gif_clip_{clip['clip_id']}"))

    return {
        "vlm_manifest": vlm_manifest,
        "rank_clips": rank_manifest.get("clips", []),
        "rank_out": rank_out,
        "gif_manifests": gif_manifests,
        "ranker_inputs": ranker_inputs,
        "captured_exports": captured_exports,
    }


def test_direct_and_staged_parity(tmp_path, monkeypatch):
    _patch_llm(monkeypatch)
    direct_root = tmp_path / "direct"
    direct_root.mkdir()
    direct = _run_direct_pipeline_fixture(
        direct_root, monkeypatch, max_output=2
    )
    direct_ranker_inputs = list(direct["_fixture_ranker_inputs"])

    staged = _run_staged_chain(tmp_path / "staged", monkeypatch, max_output=2)

    # 1. keep-gate survivors
    assert direct["scored_kept"] == staged["vlm_manifest"]["scored_count"], (
        f"keep-gate survivors differ: direct={direct['scored_kept']} "
        f"staged={staged['vlm_manifest']['scored_count']}"
    )

    # 2. post-guard/post-dedup clip windows and worthiness
    direct_windows = sorted(
        (c["start_ts"], c["end_ts"], c["gif_worthiness"])
        for c in direct["top_clips"]
    )
    staged_windows = sorted(
        (c["start_ts"], c["end_ts"], c["gif_worthiness"])
        for c in staged["rank_clips"]
    )
    assert direct_windows == staged_windows, (
        f"clip windows differ:\ndirect={direct_windows}\nstaged={staged_windows}"
    )

    # 3. candidate identities assigned before the quality boundary
    assert sorted(c["candidate_id"] for c in staged["rank_clips"]) == sorted(
        c["candidate_id"] for c in direct_ranker_inputs
    ), "candidate_id sets differ between direct and staged"

    # 4. export filenames and windows
    direct_exports = sorted(
        (Path(e["path"]).name, e["start_ts"], e["end_ts"])
        for e in direct["gif_exports"]
    )
    staged_exports = sorted(
        (m["gif_name"], m["start_ts"], m["end_ts"])
        for m in staged["gif_manifests"]
    )
    assert direct_exports == staged_exports, (
        f"exports differ:\ndirect={direct_exports}\nstaged={staged_exports}"
    )

    # 5. same ranker input population (dedup decisions)
    assert len(direct_ranker_inputs) == len(staged["ranker_inputs"]), (
        f"dedup survivor counts differ: direct={len(direct_ranker_inputs)} "
        f"staged={len(staged['ranker_inputs'])}"
    )
