from __future__ import annotations

import json

from app.services.provenance import Provenance, current_provenance, provenance_to_json
from app.task_engine.artifacts import validate_artifact
from app.task_engine.fingerprints import (
    build_stage_input_key,
    fingerprint_video,
    sha256_file,
)
from app.task_engine.models import ArtifactRef


def make_ref(path, **overrides):
    defaults = dict(
        artifact_id="a1",
        job_id="j1",
        video_id="v1",
        stage_name="sample",
        clip_id=None,
        path=str(path),
        sha256=sha256_file(path),
        size_bytes=path.stat().st_size,
        provenance_json="{}",
    )
    defaults.update(overrides)
    return ArtifactRef(**defaults)


def test_stage_key_ignores_dictionary_order(tmp_path):
    left = build_stage_input_key(video_fingerprint="fp", config={"b": 2, "a": 1}, models={"vlm": "m"}, stage_name="vlm", stage_version="1")
    right = build_stage_input_key(video_fingerprint="fp", config={"a": 1, "b": 2}, models={"vlm": "m"}, stage_name="vlm", stage_version="1")
    assert left == right


def test_stage_key_ignores_nested_dictionary_order():
    left = build_stage_input_key(
        video_fingerprint="fp",
        config={"outer": {"b": 2, "a": 1}, "x": [1, 2]},
        models={"vlm": "m"},
        stage_name="vlm",
        stage_version="1",
    )
    right = build_stage_input_key(
        video_fingerprint="fp",
        config={"x": [1, 2], "outer": {"a": 1, "b": 2}},
        models={"vlm": "m"},
        stage_name="vlm",
        stage_version="1",
    )
    assert left == right


def test_stage_key_changes_with_config():
    base = dict(video_fingerprint="fp", models={"vlm": "m"}, stage_name="vlm", stage_version="1")
    assert build_stage_input_key(config={"a": 1}, **base) != build_stage_input_key(config={"a": 2}, **base)


def test_stage_key_changes_with_model_version():
    base = dict(video_fingerprint="fp", config={"a": 1}, stage_name="vlm", stage_version="1")
    assert build_stage_input_key(models={"vlm": "m1"}, **base) != build_stage_input_key(models={"vlm": "m2"}, **base)


def test_stage_key_changes_with_stage_version():
    base = dict(video_fingerprint="fp", config={"a": 1}, models={"vlm": "m"}, stage_name="vlm")
    assert build_stage_input_key(stage_version="1", **base) != build_stage_input_key(stage_version="2", **base)


def test_stage_key_changes_with_video_fingerprint():
    base = dict(config={"a": 1}, models={"vlm": "m"}, stage_name="vlm", stage_version="1")
    assert build_stage_input_key(video_fingerprint="fp1", **base) != build_stage_input_key(video_fingerprint="fp2", **base)


def test_fingerprint_video_deterministic_for_same_file(tmp_path):
    video = tmp_path / "a.mp4"
    video.write_bytes(b"fake-video-bytes" * 100)
    assert fingerprint_video(video) == fingerprint_video(video)


def test_fingerprint_video_differs_for_different_content(tmp_path):
    left = tmp_path / "a.mp4"
    right = tmp_path / "b.mp4"
    left.write_bytes(b"content-one" * 100)
    right.write_bytes(b"content-two" * 100)
    assert fingerprint_video(left) != fingerprint_video(right)


def test_fingerprint_video_handles_file_smaller_than_block(tmp_path):
    video = tmp_path / "small.mp4"
    video.write_bytes(b"tiny")
    fp = fingerprint_video(video, block_bytes=1_048_576)
    assert isinstance(fp, str)
    assert fp == fingerprint_video(video)


def test_fingerprint_video_handles_file_larger_than_block(tmp_path):
    video = tmp_path / "big.mp4"
    video.write_bytes(b"x" * 4096)
    fp = fingerprint_video(video, block_bytes=1024)
    assert isinstance(fp, str)
    assert fp == fingerprint_video(video, block_bytes=1024)


def test_fingerprint_video_handles_file_exactly_block_bytes(tmp_path):
    video = tmp_path / "exact.mp4"
    video.write_bytes(b"y" * 1024)
    fp = fingerprint_video(video, block_bytes=1024)
    assert isinstance(fp, str)
    assert fp == fingerprint_video(video, block_bytes=1024)


def test_fingerprint_video_handles_empty_file(tmp_path):
    video = tmp_path / "empty.mp4"
    video.write_bytes(b"")
    fp = fingerprint_video(video)
    assert isinstance(fp, str)
    assert fp == fingerprint_video(video)


def test_fingerprint_video_invalidated_by_mtime_only_change(tmp_path):
    import os

    video = tmp_path / "a.mp4"
    video.write_bytes(b"same-content" * 100)
    before = fingerprint_video(video)
    stat = video.stat()
    os.utime(video, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    after = fingerprint_video(video)
    assert before != after


def test_sha256_file_matches_known_digest(tmp_path):
    import hashlib

    f = tmp_path / "x.bin"
    f.write_bytes(b"hello world")
    assert sha256_file(f) == hashlib.sha256(b"hello world").hexdigest()


def test_validate_artifact_accepts_unchanged_file(tmp_path):
    output = tmp_path / "x.json"
    output.write_text("one", encoding="utf-8")
    assert validate_artifact(make_ref(output)) is True


def test_validate_artifact_rejects_changed_file(tmp_path):
    output = tmp_path / "x.json"
    output.write_text("one", encoding="utf-8")
    ref = ArtifactRef(
        artifact_id="a1", job_id="j1", video_id="v1", stage_name="sample",
        clip_id=None, path=str(output), sha256=sha256_file(output),
        size_bytes=3, provenance_json="{}",
    )
    output.write_text("two", encoding="utf-8")
    assert validate_artifact(ref) is False


def test_validate_artifact_rejects_missing_file(tmp_path):
    missing = tmp_path / "nope.json"
    ref = ArtifactRef(
        artifact_id="a1", job_id="j1", video_id="v1", stage_name="sample",
        clip_id=None, path=str(missing), sha256="0" * 64,
        size_bytes=3, provenance_json="{}",
    )
    assert validate_artifact(ref) is False


def test_validate_artifact_rejects_size_mismatch(tmp_path):
    output = tmp_path / "x.json"
    output.write_text("one", encoding="utf-8")
    ref = make_ref(output, size_bytes=output.stat().st_size + 1)
    assert validate_artifact(ref) is False


def test_provenance_config_hash_ignores_key_order():
    left = current_provenance({"b": 2, "a": 1}, {"vlm": "1"})
    right = current_provenance({"a": 1, "b": 2}, {"vlm": "1"})
    assert left.config_hash == right.config_hash


def test_provenance_extracts_model_versions():
    config = {
        "vlm": {"provider": "ollama", "model": "llava:13b"},
        "llm": {"model": "deepseek-v4-flash"},
        "embedding": {"image_model": "llava:13b", "text_model": "nomic-embed-text:latest"},
        "media": {"source_dir": "E:/data"},
    }
    prov = current_provenance(config, {})
    assert prov.model_versions == {
        "vlm.model": "llava:13b",
        "llm.model": "deepseek-v4-flash",
        "embedding.image_model": "llava:13b",
        "embedding.text_model": "nomic-embed-text:latest",
    }


def test_provenance_prompt_hashes_empty_when_none_configured():
    prov = current_provenance({"vlm": {"model": "m"}}, {})
    assert prov.prompt_hashes == {}


def test_provenance_hashes_configured_prompts():
    import hashlib

    config = {"prompts": {"vlm_describe": "describe this frame"}}
    prov = current_provenance(config, {})
    assert prov.prompt_hashes == {
        "vlm_describe": hashlib.sha256(b"describe this frame").hexdigest()
    }


def test_provenance_hashes_passed_in_prompts_deterministically():
    import hashlib

    prompts = {"score": "score this clip", "describe": "describe this frame"}
    left = current_provenance({}, {}, prompts=prompts)
    right = current_provenance({}, {}, prompts=dict(reversed(list(prompts.items()))))
    expected = {
        name: hashlib.sha256(text.encode("utf-8")).hexdigest()
        for name, text in prompts.items()
    }
    assert left.prompt_hashes == expected
    assert right.prompt_hashes == expected


def test_provenance_passed_in_prompts_override_config_prompts():
    import hashlib

    config = {"prompts": {"from_config": "config text"}}
    prov = current_provenance(config, {}, prompts={"from_caller": "caller text"})
    assert prov.prompt_hashes == {
        "from_caller": hashlib.sha256(b"caller text").hexdigest()
    }


def test_provenance_git_commit_unknown_on_subprocess_failure(monkeypatch):
    import subprocess as sp

    import app.services.provenance as prov_mod

    def boom(*args, **kwargs):
        raise OSError("git not found")

    monkeypatch.setattr(sp, "run", boom)
    monkeypatch.setattr(prov_mod.subprocess, "run", boom)
    prov = current_provenance({}, {})
    assert prov.git_commit == "unknown"


def test_provenance_git_commit_runs_in_repo_root(monkeypatch):
    from pathlib import Path

    import app.services.provenance as prov_mod

    captured = {}

    class FakeResult:
        returncode = 0
        stdout = "abc123\n"

    def fake_run(*args, **kwargs):
        captured.update(kwargs)
        return FakeResult()

    monkeypatch.setattr(prov_mod.subprocess, "run", fake_run)
    prov = current_provenance({}, {})
    assert prov.git_commit == "abc123"
    assert Path(captured["cwd"]) == Path(prov_mod.__file__).resolve().parents[2]


def test_provenance_captures_git_commit_and_stage_versions():
    prov = current_provenance({}, {"vlm": "1", "sample": "2"})
    assert isinstance(prov.git_commit, str)
    assert prov.git_commit != ""
    assert prov.stage_versions == {"vlm": "1", "sample": "2"}


def test_provenance_serializes_deterministically():
    left = current_provenance({"b": 2, "a": 1}, {"vlm": "1"})
    right = current_provenance({"a": 1, "b": 2}, {"vlm": "1"})
    assert provenance_to_json(left) == provenance_to_json(right)
    parsed = json.loads(provenance_to_json(left))
    assert set(parsed) == {"git_commit", "config_hash", "model_versions", "prompt_hashes", "stage_versions"}
