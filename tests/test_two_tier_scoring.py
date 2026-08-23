"""Task 9: two-tier scoring prompt and caption backfill."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.clip_merge import merge_scored_frames_into_clips
from scripts.test_video_adaptive import (
    SCORE_PROMPT,
    SCORE_PROMPT_ADULT,
    _score_vlm_frame,
    _scoring_vlm_options,
    backfill_clip_captions,
    extract_config,
    get_score_prompt,
)


def _jpeg_bytes() -> bytes:
    return b"\xff\xd8\xff\xe0" + b"\x00" * 40


def _merge_kwargs(cfg: dict | None = None) -> dict:
    cfg = cfg or extract_config({"adaptive": {}})
    return {
        "merge_gap": cfg["merge_gap"],
        "merge_score_threshold": cfg["merge_score_threshold"],
        "max_merge_span_s": cfg["max_merge_span_s"],
        "peak_threshold": cfg["merge_peak_threshold"],
    }


def _synthetic_scored_set(*, caption: str) -> list[dict]:
    return [
        {
            "timestamp": 10,
            "path": "a.jpg",
            "gif_worthiness": 0.80,
            "sex_act": 0.4,
            "caption": caption,
            "emotional_core": "desire" if caption else "?",
        },
        {
            "timestamp": 16,
            "path": "b.jpg",
            "gif_worthiness": 0.88,
            "sex_act": 0.7,
            "caption": caption,
            "emotional_core": "desire" if caption else "?",
        },
        {
            "timestamp": 40,
            "path": "c.jpg",
            "gif_worthiness": 0.91,
            "sex_act": 0.8,
            "caption": caption,
            "emotional_core": "desire" if caption else "?",
        },
    ]


def test_score_schema_requests_only_numeric_fields():
    prompt = get_score_prompt("adult", schema="score")
    assert '"gif_worthiness"' in prompt and '"sex_act"' in prompt
    assert '"caption"' not in prompt and '"aesthetic_notes"' not in prompt
    assert "0.8-1.0" in prompt
    assert get_score_prompt("adult", schema="full") == SCORE_PROMPT_ADULT
    assert get_score_prompt("default", schema="full") == SCORE_PROMPT
    default_fast = get_score_prompt("default", schema="score")
    assert '"gif_worthiness"' in default_fast
    assert '"sex_act"' not in default_fast
    assert '"caption"' not in default_fast


def test_score_schema_skips_caption_quality_gate(tmp_path):
    from tests.task_engine.test_vlm_stage_runtime import _StubServer

    stub = _StubServer({"response": json.dumps({"gif_worthiness": 0.71})})
    stub.start()
    try:
        parsed, error = _score_vlm_frame(
            base_url=stub.base_url,
            model="stub-vlm",
            image_bytes=_jpeg_bytes(),
            prompt=get_score_prompt("adult", schema="score"),
            options={},
            threshold=0.2,
            timestamp=4.0,
            frame_path=str(tmp_path / "frame.jpg"),
            retry_delay_s=0.0,
            schema="score",
        )
    finally:
        stub.stop()

    assert error is None
    assert parsed is not None
    assert parsed["gif_worthiness"] == pytest.approx(0.71)
    assert parsed.get("caption", "") == ""


def test_score_schema_still_rejects_invalid_worthiness(tmp_path):
    from tests.task_engine.test_vlm_stage_runtime import _StubServer

    stub = _StubServer({"response": json.dumps({"gif_worthiness": "AVERAGE"})})
    stub.start()
    try:
        parsed, error = _score_vlm_frame(
            base_url=stub.base_url,
            model="stub-vlm",
            image_bytes=_jpeg_bytes(),
            prompt="score",
            options={},
            threshold=0.2,
            timestamp=4.0,
            frame_path=str(tmp_path / "frame.jpg"),
            retry_delay_s=0.0,
            schema="score",
        )
    finally:
        stub.stop()

    assert parsed is None
    assert error is not None
    assert "invalid gif_worthiness" in error


def test_caption_backfill_is_non_fatal():
    clips = [
        {
            "start_ts": 10,
            "end_ts": 10,
            "gif_worthiness": 0.8,
            "best_frame": {
                "timestamp": 10,
                "path": "missing.jpg",
                "gif_worthiness": 0.8,
            },
        }
    ]

    def fail(_frame: dict) -> dict | None:
        raise RuntimeError("vlm down")

    out = backfill_clip_captions(clips, score_frame=fail, max_frames=10)
    assert all("caption" in clip["best_frame"] for clip in out)
    assert out[0]["best_frame"]["caption"] == ""


def test_backfill_respects_budget():
    calls: list[dict] = []

    def score_frame(frame: dict) -> dict:
        calls.append(frame)
        return {
            "caption": f"frame {frame['timestamp']}",
            "emotional_core": "awe",
            "aesthetic_notes": ["visible subject lighting", "clear motion"],
            "reason": "peak readable action",
        }

    clips = [
        {
            "gif_worthiness": 0.5 + (index / 1000.0),
            "best_frame": {
                "timestamp": index,
                "path": f"{index}.jpg",
                "gif_worthiness": 0.5 + (index / 1000.0),
            },
        }
        for index in range(300)
    ]
    backfill_clip_captions(clips, score_frame=score_frame, max_frames=150)
    assert len(calls) == 150


def test_two_tier_and_legacy_produce_the_same_clip_intervals():
    kwargs = _merge_kwargs()
    legacy = merge_scored_frames_into_clips(
        _synthetic_scored_set(caption="full prose"), **kwargs
    )
    two_tier = merge_scored_frames_into_clips(
        _synthetic_scored_set(caption=""), **kwargs
    )
    assert [(clip["start_ts"], clip["end_ts"]) for clip in legacy] == [
        (clip["start_ts"], clip["end_ts"]) for clip in two_tier
    ]


def test_refine_provisional_best_frames_match_synthesize(tmp_path, monkeypatch):
    from scripts import test_video_adaptive as mod

    work_dir = tmp_path / "refine_work"
    frames_dir = work_dir / "frames"
    frames_dir.mkdir(parents=True)

    scored = []
    for timestamp, worth in ((10, 0.80), (16, 0.88), (40, 0.91)):
        path = frames_dir / f"ts_{timestamp:06d}.jpg"
        path.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 600)
        scored.append({
            "timestamp": timestamp,
            "path": str(path),
            "gif_worthiness": worth,
            "caption": "",
            "emotional_core": "?",
        })

    discover = {
        "schema_version": 1, "stage": "discover", "duration_s": 60.0,
    }
    vlm = {
        "schema_version": 1, "stage": "vlm",
        "scored_count": len(scored), "frames": scored,
        "attempted_count": len(scored), "response_count": len(scored),
        "parsed_count": len(scored), "failed_count": 0,
        "output_key": "vlm",
    }
    discover_path = work_dir / "discover_manifest.json"
    vlm_path = work_dir / "vlm_manifest.json"
    discover_path.write_text(json.dumps(discover), encoding="utf-8")
    vlm_path.write_text(json.dumps(vlm), encoding="utf-8")

    def fake_score(**kwargs):
        return {
            "caption": f"visible action at {kwargs['timestamp']}",
            "emotional_core": "desire",
            "aesthetic_notes": ["clear body framing here", "readable motion now"],
            "reason": "this moment works as a short gif",
            "gif_worthiness": kwargs.get("timestamp", 0) and 0.9,
        }, None

    monkeypatch.setattr(mod, "_score_vlm_frame", lambda **kw: fake_score(**kw))
    monkeypatch.setattr(mod, "wait_model", lambda *a, **k: True)

    config_data = {
        "vlm": {
            "provider": "ollama",
            "model": "stub-vlm",
            "base_url": "http://127.0.0.1:1",
            "manage_lifecycle": False,
            "launch_mode": "none",
        },
        "adaptive": {
            "score_schema_mode": "two_tier",
            "refine_threshold": 1.1,
            "merge_gap": 12,
            "merge_score_threshold": 0.55,
        },
    }
    cfg = extract_config(config_data)
    inputs = {
        "vlm_manifest": [{"artifact_id": "a", "path": str(vlm_path), "clip_id": None}],
        "discover_manifest": [
            {"artifact_id": "b", "path": str(discover_path), "clip_id": None}
        ],
    }

    refine_out = mod._stage_refine(
        str(tmp_path / "missing.mp4"),
        str(frames_dir),
        str(work_dir),
        cfg,
        inputs,
        config_data,
    )
    refine_path = refine_out["_artifacts"][0]["path"]
    refine_manifest = json.loads(Path(refine_path).read_text(encoding="utf-8"))
    assert refine_manifest["caption_backfill_attempted"] >= 1
    assert refine_manifest["caption_backfill_succeeded"] >= 1

    synth_out = mod._stage_synthesize(
        str(work_dir),
        cfg,
        {
            "refine_manifest": [
                {"artifact_id": "c", "path": refine_path, "clip_id": None}
            ]
        },
    )
    synth_manifest = json.loads(
        Path(synth_out["_artifacts"][0]["path"]).read_text(encoding="utf-8")
    )

    provisional = merge_scored_frames_into_clips(
        refine_manifest["frames"], **_merge_kwargs(cfg)
    )
    assert {clip["best_frame"]["timestamp"] for clip in provisional} == {
        clip["best_frame_ts"] for clip in synth_manifest["clips"]
    }
    assert all(clip.get("caption") for clip in synth_manifest["clips"])


def test_scoring_options_apply_schema_num_predict_caps():
    cfg = extract_config({
        "adaptive": {
            "vlm_num_predict_score": 48,
            "vlm_num_predict_caption": 320,
        }
    })
    score_opts = _scoring_vlm_options(cfg, "score")
    full_opts = _scoring_vlm_options(cfg, "full")
    assert score_opts["num_predict"] == 48
    assert full_opts["num_predict"] == 320
    legacy = _scoring_vlm_options(extract_config({"adaptive": {}}), "score")
    assert "num_predict" not in legacy


def test_extract_config_defaults_keep_legacy_scoring():
    cfg = extract_config({"adaptive": {}})
    assert cfg["score_schema_mode"] == "legacy"
    assert cfg["caption_backfill_max_frames"] == 150
    assert cfg["vlm_num_predict_score"] is None
    assert cfg["vlm_num_predict_caption"] is None
