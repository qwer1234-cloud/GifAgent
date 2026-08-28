"""run_stage_mode must restore process-global config on every exit path."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import app.config as config_mod
from app.pipeline import stage_io as stage_io_mod

_MARKER = "_test_config_restore_marker"


def _write_snapshot(tmp_path: Path) -> Path:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({_MARKER: "leaked", "adaptive": {}}),
        encoding="utf-8",
    )
    return config_path


def _run_stage_mode(tmp_path: Path, config_path: Path) -> None:
    stage_io_mod.run_stage_mode(
        stage="discover",
        video_path=str(tmp_path / "source.mp4"),
        work_dir=str(tmp_path / "work"),
        result_path=str(tmp_path / "result.json"),
        config_path=str(config_path),
    )


def test_run_stage_mode_restores_config_when_init_db_fails(tmp_path, monkeypatch):
    previous = config_mod.swap_config_override({_MARKER: "baseline"})
    try:
        monkeypatch.setattr(
            stage_io_mod,
            "init_db",
            lambda: (_ for _ in ()).throw(RuntimeError("db boom")),
        )
        with pytest.raises(RuntimeError, match="db boom"):
            _run_stage_mode(tmp_path, _write_snapshot(tmp_path))
        assert config_mod.get(_MARKER) == "baseline"
        assert not (tmp_path / "result.json").exists()
    finally:
        config_mod.swap_config_override(previous)


def test_run_stage_mode_restores_config_when_extract_config_fails(
    tmp_path, monkeypatch,
):
    previous = config_mod.swap_config_override({_MARKER: "baseline"})
    try:
        monkeypatch.setattr(stage_io_mod, "init_db", lambda: None)
        monkeypatch.setattr(
            stage_io_mod,
            "extract_config",
            lambda *_a, **_k: (_ for _ in ()).throw(ValueError("bad cfg")),
        )
        with pytest.raises(ValueError, match="bad cfg"):
            _run_stage_mode(tmp_path, _write_snapshot(tmp_path))
        assert config_mod.get(_MARKER) == "baseline"
        assert not (tmp_path / "result.json").exists()
    finally:
        config_mod.swap_config_override(previous)


def test_run_stage_mode_restores_config_after_success(tmp_path, monkeypatch):
    previous = config_mod.swap_config_override({_MARKER: "baseline"})
    try:
        monkeypatch.setattr(stage_io_mod, "init_db", lambda: None)
        monkeypatch.setattr(
            stage_io_mod,
            "_run_stage",
            lambda *_a, **_k: {"output_key": "discover", "_artifacts": []},
        )
        _run_stage_mode(tmp_path, _write_snapshot(tmp_path))
        assert config_mod.get(_MARKER) == "baseline"
        result_path = tmp_path / "result.json"
        assert result_path.exists()
        payload = json.loads(result_path.read_text(encoding="utf-8"))
        assert payload["stage"] == "discover"
        assert payload["output_key"] == "discover"
    finally:
        config_mod.swap_config_override(previous)
