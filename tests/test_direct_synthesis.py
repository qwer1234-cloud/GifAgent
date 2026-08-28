"""Direct-only synthesis: merge without per-clip LLM, managed VLM unload."""

from __future__ import annotations

import json
from pathlib import Path

from app.pipeline import direct as pipeline_direct
from app.pipeline.config import extract_config
from app.pipeline.stages import synthesize as synthesize_stage
from app.pipeline.vlm_runtime import VlmRuntimeConfig
from tests.test_adaptive_direct_transition import _run_direct_pipeline_fixture


def _refine_inputs(tmp_path: Path) -> dict:
    refine_path = tmp_path / "refine_manifest.json"
    refine_path.write_text(
        json.dumps({
            "schema_version": 1,
            "stage": "refine",
            "scored_count": 2,
            "frames": [
                {
                    "timestamp": 1.0,
                    "path": "a.jpg",
                    "gif_worthiness": 0.9,
                    "emotional_core": "awe",
                    "caption": "one",
                },
                {
                    "timestamp": 20.0,
                    "path": "b.jpg",
                    "gif_worthiness": 0.85,
                    "emotional_core": "joy",
                    "caption": "two",
                },
            ],
        }),
        encoding="utf-8",
    )
    return {
        "refine_manifest": [{"artifact_id": "r1", "path": str(refine_path)}],
    }


def _runtime(*, manage: bool, launch_mode: str, model: str = "qwen2.5vl:7b"):
    return VlmRuntimeConfig(
        provider="ollama",
        model=model,
        base_url="http://127.0.0.1:1",
        manage_lifecycle=manage,
        launch_mode=launch_mode,
        retry_delay_s=0.0,
    )


def test_stage_synthesize_skips_clip_llm_when_disabled(tmp_path, monkeypatch):
    calls: list[str] = []

    def boom(*_args, **_kwargs):
        calls.append("llm")
        raise AssertionError("clip_llm=False must not call generate_llm_text")

    monkeypatch.setattr(
        "app.services.llm_client.generate_llm_text", boom, raising=True
    )
    work = tmp_path / "work"
    work.mkdir()
    out = synthesize_stage._stage_synthesize(
        str(work),
        extract_config({"adaptive": {}}),
        _refine_inputs(tmp_path),
        clip_llm=False,
    )
    assert calls == []
    assert out["clip_count"] == 2
    clips = json.loads(
        Path(out["_artifacts"][0]["path"]).read_text(encoding="utf-8")
    )["clips"]
    assert all(clip.get("tags") == [] for clip in clips)
    assert all(clip.get("summary") == "" for clip in clips)


def test_stage_synthesize_clip_llm_default_calls_generate_llm_text(
    tmp_path, monkeypatch,
):
    calls: list[str] = []

    def fake_llm(_prompt, **_kwargs):
        calls.append("llm")
        return '{"summary":"tagged","tags":["x"]}'

    monkeypatch.setattr(
        "app.services.llm_client.generate_llm_text", fake_llm, raising=True
    )
    work = tmp_path / "work"
    work.mkdir()
    out = synthesize_stage._stage_synthesize(
        str(work),
        extract_config({"adaptive": {}}),
        _refine_inputs(tmp_path),
    )
    assert len(calls) == 2
    clips = json.loads(
        Path(out["_artifacts"][0]["path"]).read_text(encoding="utf-8")
    )["clips"]
    assert [clip.get("tags") for clip in clips] == [["x"], ["x"]]


def test_direct_pipeline_does_not_run_per_clip_llm(tmp_path, monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(
        synthesize_stage,
        "_synthesize_clips_with_llm",
        lambda *_a, **_k: calls.append(1),
    )
    _run_direct_pipeline_fixture(tmp_path, monkeypatch, max_output=2)
    assert calls == []


def test_release_vlm_skips_when_unmanaged(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(
        pipeline_direct,
        "stop_model",
        lambda name, runtime=None: calls.append((name, runtime)),
    )
    pipeline_direct._release_vlm_for_llm(None)
    pipeline_direct._release_vlm_for_llm(
        _runtime(manage=False, launch_mode="wsl")
    )
    pipeline_direct._release_vlm_for_llm(
        _runtime(manage=True, launch_mode="none")
    )
    assert calls == []


def test_release_vlm_stops_configured_model(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(
        pipeline_direct,
        "stop_model",
        lambda name, runtime=None: calls.append((name, runtime)),
    )
    runtime = _runtime(manage=True, launch_mode="native", model="qwen2.5vl:7b")
    pipeline_direct._release_vlm_for_llm(runtime)
    assert calls == [("qwen2.5vl:7b", runtime)]
    assert calls[0][0] != "llava"


def test_video_level_synthesis_skips_when_llm_unavailable(monkeypatch):
    waits: list[dict] = []

    def fake_wait(**kwargs):
        waits.append(kwargs)
        return False

    monkeypatch.setattr(pipeline_direct, "wait_for_llm", fake_wait)
    monkeypatch.setattr(
        pipeline_direct,
        "generate_llm_text",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("must not call generate_llm_text when wait fails")
        ),
    )
    result = pipeline_direct._video_level_synthesis([{
        "best_frame_ts": 1.0,
        "gif_worthiness": 0.9,
        "caption": "x",
        "emotional_core": "awe",
    }])
    assert result == {"_parse_error": True}
    assert len(waits) == 1


def test_video_level_synthesis_waits_once_then_calls_llm(monkeypatch):
    waits: list[dict] = []

    def fake_wait(**kwargs):
        waits.append(kwargs)
        return True

    monkeypatch.setattr(pipeline_direct, "wait_for_llm", fake_wait)
    monkeypatch.setattr(
        pipeline_direct,
        "generate_llm_text",
        lambda *_a, **_k: (
            '{"summary":"s","emotional_core":"awe",'
            '"aesthetic_notes":[],"tags":["t"],"scene_type":"other"}'
        ),
    )
    result = pipeline_direct._video_level_synthesis([{
        "best_frame_ts": 4.0,
        "gif_worthiness": 0.8,
        "caption": "peak",
        "emotional_core": "awe",
    }])
    assert result["summary"] == "s"
    assert result["tags"] == ["t"]
    assert len(waits) == 1


def test_direct_default_runtime_does_not_stop_llava(tmp_path, monkeypatch):
    result = _run_direct_pipeline_fixture(tmp_path, monkeypatch, max_output=2)
    names = [args[0] for args, _kwargs in result["_fixture_stop_calls"]]
    assert names == []
    assert "llava" not in names


def test_direct_unmanaged_runtime_does_not_stop_llava(tmp_path, monkeypatch):
    result = _run_direct_pipeline_fixture(
        tmp_path,
        monkeypatch,
        max_output=2,
        vlm_runtime=_runtime(manage=False, launch_mode="none"),
    )
    names = [args[0] for args, _kwargs in result["_fixture_stop_calls"]]
    assert names == []
    assert "llava" not in names


def test_direct_managed_runtime_stops_real_model(tmp_path, monkeypatch):
    runtime = _runtime(manage=True, launch_mode="native", model="qwen2.5vl:7b")
    result = _run_direct_pipeline_fixture(
        tmp_path, monkeypatch, max_output=2, vlm_runtime=runtime,
    )
    calls = result["_fixture_stop_calls"]
    assert len(calls) == 1
    args, _kwargs = calls[0]
    assert args[0] == "qwen2.5vl:7b"
    assert args[1] is runtime
