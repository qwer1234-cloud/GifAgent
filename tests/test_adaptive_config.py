import hashlib
import json

import pytest
import yaml

from app.ui.tabs import settings
from scripts.test_video_adaptive import extract_config


def test_adaptive_action_defaults_are_frozen():
    cfg = extract_config({"adaptive": {}})

    assert cfg["min_duration"] == 2.0
    assert cfg["max_duration"] == 20.0
    assert cfg["action_guard_enabled"] is True
    assert cfg["action_vlm_verify_enabled"] is True
    assert cfg["action_analysis_version"] == 1
    assert cfg["action_analysis_window_s"] == 30.0
    assert cfg["action_preferred_min_duration_s"] == 4.0
    assert cfg["action_preferred_max_duration_s"] == 12.0
    assert cfg["action_scan_fps"] == 4.0
    assert cfg["action_boundary_confidence_threshold"] == 0.65
    assert cfg["action_loop_adjust_s"] == 0.75
    assert cfg["action_vlm_min_worthiness"] == 0.60
    assert cfg["action_fallback_mode"] == "fixed_window"


@pytest.mark.parametrize(
    ("adaptive", "offending_key"),
    [
        (
            {
                "action_preferred_min_duration_s": 13,
                "action_preferred_max_duration_s": 12,
            },
            "action_preferred_min_duration_s",
        ),
        (
            {
                "action_preferred_max_duration_s": 21,
                "max_duration": 20,
            },
            "action_preferred_max_duration_s",
        ),
        (
            {
                "max_duration": 31,
                "action_analysis_window_s": 30,
            },
            "max_duration",
        ),
    ],
)
def test_adaptive_action_relationships_are_strict(adaptive, offending_key):
    with pytest.raises(ValueError, match=offending_key):
        extract_config({"adaptive": adaptive})


def test_action_config_hash_is_canonical_and_action_only():
    adaptive = {
        "action_scan_fps": 6,
        "action_boundary_confidence_threshold": 0.7,
    }
    cfg = extract_config({"adaptive": adaptive})
    reordered = extract_config(
        {"adaptive": dict(reversed(list(adaptive.items())))}
    )
    unrelated = extract_config(
        {"adaptive": {**adaptive, "sample_interval": 99}}
    )
    stale_snapshot = extract_config(
        {
            "adaptive": adaptive,
            "action_config_hash": "STALE_ACTION_HASH",
        }
    )

    action_subset = {
        key: value
        for key, value in cfg.items()
        if key.startswith("action_") and key != "action_config_hash"
    }
    action_subset["min_duration"] = cfg["min_duration"]
    action_subset["max_duration"] = cfg["max_duration"]
    expected = hashlib.sha256(
        json.dumps(
            action_subset,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    assert cfg["action_config_hash"] == expected
    assert reordered["action_config_hash"] == expected
    assert unrelated["action_config_hash"] == expected
    assert stale_snapshot["action_config_hash"] == expected


def test_settings_action_checkboxes_load_after_transition_fields(
    tmp_path, monkeypatch,
):
    config_path = tmp_path / "models.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "adaptive": {
                    "transition_guard_enabled": False,
                    "transition_min_duration_s": 2.5,
                    "transition_boundary_margin_s": 0.5,
                    "action_guard_enabled": False,
                    "action_vlm_verify_enabled": True,
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "CONFIG_FILE", str(config_path))

    _, _, adaptive_fields, _, _ = settings.load_config()

    assert adaptive_fields[6:11] == [False, "2.5", "0.5", False, True]


def test_settings_reject_invalid_action_relationship_without_writing(
    tmp_path, monkeypatch,
):
    config_path = tmp_path / "models.yaml"
    original = yaml.safe_dump(
        {
            "adaptive": {
                "min_duration": 2,
                "max_duration": 20,
                "action_preferred_max_duration_s": 12,
            }
        },
        sort_keys=False,
    )
    config_path.write_text(original, encoding="utf-8")
    monkeypatch.setattr(settings, "CONFIG_FILE", str(config_path))

    status, _ = settings.save_config(
        "", "", "", "", "0.3", "2048", "120",
        "", "",
        "10", "12", "0.55", "0.2", "0.5", "3",
        True, "2", "0.25", True, True,
        "0.65", "1.0", "0", "24",
        False, "0.5", "0.5", "",
    )

    assert "配置错误" in status
    assert config_path.read_text(encoding="utf-8") == original


def test_settings_reject_malformed_number_without_writing(
    tmp_path, monkeypatch,
):
    config_path = tmp_path / "models.yaml"
    original = yaml.safe_dump(
        {
            "adaptive": {
                "min_duration": 2,
                "max_duration": 20,
                "action_preferred_max_duration_s": 12,
            }
        },
        sort_keys=False,
    )
    config_path.write_text(original, encoding="utf-8")
    monkeypatch.setattr(settings, "CONFIG_FILE", str(config_path))

    status, _ = settings.save_config(
        "", "", "", "", "0.3", "2048", "120",
        "", "",
        "10", "12", "0.55", "0.2", "0.5", "not-a-number",
        True, "2", "0.25", True, True,
        "0.65", "1.0", "0", "24",
        False, "0.5", "0.5", "",
    )

    assert "配置错误" in status
    assert config_path.read_text(encoding="utf-8") == original


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
