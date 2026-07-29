from pathlib import Path

import yaml

from scripts.test_video_adaptive import extract_config


def test_adaptive_max_duration_is_configured_with_default_10():
    project_root = Path(__file__).resolve().parents[1]

    config = yaml.safe_load((project_root / "configs" / "models.yaml").read_text(encoding="utf-8"))
    assert config["adaptive"]["max_duration"] == 10

    script = (project_root / "scripts" / "test_video_adaptive.py").read_text(encoding="utf-8")
    # Config extraction must default max_duration to 10, not some other value
    assert '"max_duration", 10)' in script
    assert '"max_duration", 5.0)' not in script


def test_config_extracts_transition_defaults():
    """The frozen config supplies every transition guard default."""
    cfg = extract_config({"adaptive": {}})

    assert cfg["transition_guard_enabled"] is True
    assert cfg["transition_min_duration_s"] == 2.0
    assert cfg["transition_boundary_margin_s"] == 0.25
    assert cfg["transition_scan_fps"] == 8.0
    assert cfg["transition_scan_width"] == 320
    assert cfg["transition_motion_compensation"] is True
    assert cfg["transition_hard_threshold"] == 0.65
    assert cfg["transition_soft_threshold"] == 0.40
    assert cfg["transition_soft_run_frames"] == 3
    assert cfg["transition_rescore_split_segments"] is True
