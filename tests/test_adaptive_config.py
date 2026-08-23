import hashlib
import json
from pathlib import Path

import pytest
import yaml

from app.services.gif_encode import is_divisible_gif_fps
from app.task_engine.fingerprints import canonical_hash
from app.ui.tabs import settings
from scripts import test_video_adaptive
from scripts.test_video_adaptive import (
    DEFAULT_MAX_REFINE_FRAMES,
    _palette_filters_for,
    collect_refine_timestamps,
    extract_config,
)


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


def test_implicit_preferred_action_maximum_never_exceeds_configured_maximum():
    cfg = extract_config({"adaptive": {"max_duration": 10}})

    assert cfg["max_duration"] == 10.0
    assert cfg["action_preferred_max_duration_s"] == 10.0


def test_implicit_action_preferences_stay_within_configured_duration_bounds():
    cfg = extract_config(
        {"adaptive": {"min_duration": 6, "max_duration": 10}}
    )

    assert cfg["action_preferred_min_duration_s"] == 6.0
    assert cfg["action_preferred_max_duration_s"] == 10.0


def test_implicit_action_preferences_support_minimum_above_twelve_seconds():
    cfg = extract_config(
        {"adaptive": {"min_duration": 15, "max_duration": 20}}
    )

    assert cfg["action_preferred_min_duration_s"] == 15.0
    assert cfg["action_preferred_max_duration_s"] == 15.0


def test_explicit_preferred_action_minimum_below_configured_minimum_is_rejected():
    with pytest.raises(ValueError, match="action_preferred_min_duration_s"):
        extract_config(
            {
                "adaptive": {
                    "min_duration": 6,
                    "max_duration": 10,
                    "action_preferred_min_duration_s": 4,
                }
            }
        )


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

    assert adaptive_fields[7:12] == [False, "2.5", "0.5", False, True]
    assert adaptive_fields[-2] == "default"
    assert adaptive_fields[-1] == "1"


def test_settings_save_score_prompt_mode(tmp_path, monkeypatch):
    config_path = tmp_path / "models.yaml"
    config_path.write_text(
        yaml.safe_dump({"adaptive": {"score_prompt_mode": "default"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "CONFIG_FILE", str(config_path))

    status, _ = settings.save_config(
        "", "", "", "", "0.3", "2048", "120",
        "", "",
        "10", "12", "0.55", "0.2", "0.5", "20", "5",
        True, "2", "0.25", True, True,
        "0.65", "1.0", "0", "24", "adult", "6",
        False, "0.5", "0.5", "",
    )

    assert status.startswith("Saved")
    saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert saved["adaptive"]["score_prompt_mode"] == "adult"
    assert saved["adaptive"]["single_frame_max_duration_s"] == 5.0
    assert saved["adaptive"]["frame_extract_workers"] == 6


def test_settings_save_rewrites_stale_wsl_ollama_urls(tmp_path, monkeypatch):
    config_path = tmp_path / "models.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "vlm": {"base_url": "http://172.27.227.98:11434"},
                "embedding": {"base_url": "http://172.27.227.98:11434"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "CONFIG_FILE", str(config_path))
    monkeypatch.setattr("app.services.ollama_runtime.os.name", "nt")

    status, _ = settings.save_config(
        "", "", "", "", "0.3", "2048", "120",
        "llava:13b", "http://172.27.227.98:11434",
        "10", "12", "0.55", "0.2", "0.5", "20", "",
        True, "2", "0.25", True, True,
        "0.65", "1.0", "0", "24", "adult", "6",
        False, "0.5", "0.5", "",
    )

    assert status.startswith("Saved")
    saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert saved["vlm"]["base_url"] == "auto"
    assert saved["vlm"]["manage_lifecycle"] is True
    assert saved["vlm"]["launch_mode"] == "wsl"
    assert saved["embedding"]["base_url"] == "auto"
    assert saved["embedding"]["manage_lifecycle"] is True
    assert saved["embedding"]["launch_mode"] == "wsl"


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
        "10", "12", "0.55", "0.2", "0.5", "3", "",
        True, "2", "0.25", True, True,
        "0.65", "1.0", "0", "24", "default", "6",
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
        "10", "12", "0.55", "0.2", "0.5", "not-a-number", "",
        True, "2", "0.25", True, True,
        "0.65", "1.0", "0", "24", "default", "6",
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
    assert cfg["score_prompt_mode"] == "default"


def test_extract_config_freezes_score_prompt_mode_from_the_job_snapshot(monkeypatch):
    monkeypatch.setenv("GIFAGENT_SCORE_PROMPT_MODE", "adult")

    cfg = extract_config({"adaptive": {}})
    assert cfg["score_prompt_mode"] == "default"

    frozen = extract_config({"adaptive": {"score_prompt_mode": "adult"}})
    monkeypatch.setenv("GIFAGENT_SCORE_PROMPT_MODE", "default")
    assert frozen["score_prompt_mode"] == "adult"

    aliased = extract_config({"adaptive": {"score_prompt_mode": "nsfw"}})
    assert aliased["score_prompt_mode"] == "adult"

    with pytest.raises(ValueError, match="score_prompt_mode"):
        extract_config({"adaptive": {"score_prompt_mode": "cinematic-v2"}})


def test_extract_config_rejects_quality_ranking_weights_that_do_not_sum_to_one():
    with pytest.raises(ValueError, match="quality ranking weights"):
        extract_config(
            {
                "adaptive": {
                    "quality_ranking_adult_weight": 0.90,
                    "quality_ranking_cinematic_weight": 0.20,
                }
            }
        )


def test_get_score_prompt_uses_frozen_mode_not_environment(monkeypatch):
    from scripts.test_video_adaptive import (
        SCORE_PROMPT,
        SCORE_PROMPT_ADULT,
        get_score_prompt,
    )

    monkeypatch.setenv("GIFAGENT_SCORE_PROMPT_MODE", "adult")
    assert get_score_prompt("default") == SCORE_PROMPT
    assert get_score_prompt("adult") == SCORE_PROMPT_ADULT
    assert get_score_prompt("optimized") == SCORE_PROMPT_ADULT
    assert "sex_act" in SCORE_PROMPT_ADULT
    assert "equally eligible" not in SCORE_PROMPT_ADULT


def test_extract_config_freezes_quality_moe_from_the_job_snapshot():
    source = {
        "quality_moe": {
            "report_only": False,
            "soft_reject": {"min_judge_confidence": 0.9},
        }
    }

    cfg = extract_config(source)
    source["quality_moe"]["report_only"] = True
    source["quality_moe"]["soft_reject"]["min_judge_confidence"] = 0.8

    assert cfg["quality_moe"]["enabled"] is True
    assert cfg["quality_moe"]["report_only"] is False
    assert cfg["quality_moe"]["soft_reject"]["min_judge_confidence"] == 0.9
    assert len(cfg["quality_moe_config_hash"]) == 64
    assert cfg["quality_moe_config_hash"] == extract_config(
        {
            "quality_moe": {
                "report_only": False,
                "soft_reject": {"min_judge_confidence": 0.9},
            }
        }
    )["quality_moe_config_hash"]


def test_models_yaml_enables_active_adult_quality_moe_by_default():
    config_path = Path(__file__).resolve().parents[1] / "configs" / "models.yaml"
    config_data = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    assert config_data["quality_moe"]["report_only"] is False
    cfg = extract_config(config_data)

    assert cfg["quality_moe"]["enabled"] is True
    assert cfg["quality_moe"]["report_only"] is False
    assert cfg["score_prompt_mode"] == "adult"
    assert cfg["quality_ranking_adult_weight"] == 0.80
    assert cfg["quality_ranking_cinematic_weight"] == 0.20
    assert cfg["quality_moe"]["repairability"]["photometric_mode"] == "clip_global"
    assert config_data["quality_moe"]["judge"]["base_url"] == "inherit_vlm"
    assert "Uncensored" in str(config_data["quality_moe"]["judge"]["model_id"])
    assert config_data["vlm"]["base_url"] == "auto"
    assert "172.27.227.98" not in config_path.read_text(encoding="utf-8")


def test_models_yaml_balances_quality_gates_with_nontrivial_output_capacity():
    """Catch profiles that equate quality with sparse discovery or tiny output."""
    config_path = Path(__file__).resolve().parents[1] / "configs" / "models.yaml"
    config_data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    cfg = extract_config(config_data)

    assert cfg["sample_interval"] <= 7
    assert cfg["merge_score_threshold"] >= 0.58
    assert cfg["merge_peak_threshold"] >= 0.65
    assert cfg["max_merge_span_s"] <= 18
    assert cfg["worthiness_threshold"] >= 0.62
    assert cfg["refine_threshold"] >= 0.70
    assert cfg["action_preferred_min_duration_s"] == 5.0
    assert cfg["action_preferred_max_duration_s"] == 8.0
    assert cfg["vlm_temperature"] <= 0.25
    assert cfg["embed_sim_threshold"] <= 0.88
    assert cfg["temporal_dedup_min_gap_s"] >= 15
    assert cfg["gif_fps"] == 25
    assert cfg["gif_max_width"] == 720
    assert cfg["output_ratio"] == 1.0
    assert cfg["max_output"] == 100

    qualified_candidates = 40
    planned_output = min(
        int(qualified_candidates * cfg["output_ratio"]),
        cfg["max_output"],
    )
    assert planned_output >= 30
    assert cfg["quality_moe"]["report_only"] is False


def test_quality_runtime_snapshot_resolves_inherit_vlm_once():
    from scripts import test_video_adaptive as adaptive

    source = {
        "vlm": {
            "provider": "ollama",
            "model": "llava:13b",
            "base_url": "http://frozen-vlm.example:11434/",
        },
        "quality_moe": {
            "judge": {"model_id": "llava:13b", "base_url": "inherit_vlm"},
        },
    }

    frozen = adaptive._resolve_quality_runtime_snapshot(
        source,
        auto_resolver=lambda runtime, _snapshot: runtime.base_url,
    )
    source["vlm"]["base_url"] = "http://drifted.example:11434"
    source["quality_moe"]["judge"]["base_url"] = "http://drifted.example:11434"

    assert frozen["vlm"]["base_url"] == "http://frozen-vlm.example:11434"
    assert frozen["quality_moe"]["judge"]["base_url"] == (
        "http://frozen-vlm.example:11434"
    )
    cfg = adaptive.extract_config(frozen)
    assert cfg["quality_moe"]["judge"]["base_url"].startswith("http://")
    assert "inherit_vlm" not in cfg["quality_moe"]["judge"]["base_url"]


def test_quality_runtime_snapshot_resolves_auto_before_judge_http_boundary():
    from scripts import test_video_adaptive as adaptive

    seen = []

    def resolve_auto(runtime, _snapshot):
        seen.append(runtime.base_url)
        return (
            "http://resolved-vlm.example:11434"
            if len(seen) == 1
            else "http://resolved-quality.example:11434"
        )

    frozen = adaptive._resolve_quality_runtime_snapshot(
        {
            "vlm": {
                "provider": "ollama",
                "model": "llava:13b",
                "base_url": "auto",
            },
            "quality_moe": {
                "judge": {"model_id": "llava:13b", "base_url": "auto"},
            },
        },
        auto_resolver=resolve_auto,
    )

    assert seen == ["auto", "auto"]
    assert frozen["vlm"]["base_url"] == "http://resolved-vlm.example:11434"
    assert frozen["quality_moe"]["judge"]["base_url"] == (
        "http://resolved-quality.example:11434"
    )


def test_explicit_quality_endpoint_is_not_overwritten_by_vlm_auto_resolution():
    from types import SimpleNamespace
    from app.quality_moe.config import freeze_quality_runtime_config

    seen = []

    def ready(runtime_config):
        seen.append(runtime_config.base_url)
        return SimpleNamespace(base_url="http://resolved-vlm.example:11434")

    frozen = freeze_quality_runtime_config({
        "vlm": {"base_url": "auto", "model": "llava:13b"},
        "quality_moe": {"judge": {
            "base_url": "https://explicit-quality.example:443/",
            "model_id": "llava:13b",
        }},
    }, ready_resolver=ready)

    assert seen == ["auto"]
    assert frozen["vlm"]["base_url"] == "http://resolved-vlm.example:11434"
    assert frozen["quality_moe"]["judge"]["base_url"] == (
        "https://explicit-quality.example:443"
    )


def test_quality_auto_uses_independent_resolver_not_explicit_vlm_endpoint():
    from types import SimpleNamespace
    from app.quality_moe.config import freeze_quality_runtime_config

    seen = []

    def ready(runtime_config):
        seen.append(runtime_config.base_url)
        return SimpleNamespace(base_url="http://quality-auto.example:11434")

    frozen = freeze_quality_runtime_config({
        "vlm": {
            "base_url": "http://explicit-vlm.example:11434/",
            "model": "llava:13b",
        },
        "quality_moe": {"judge": {
            "base_url": "auto", "model_id": "llava:13b",
        }},
    }, ready_resolver=ready)

    assert seen == ["auto"]
    assert frozen["vlm"]["base_url"] == "http://explicit-vlm.example:11434"
    assert frozen["quality_moe"]["judge"]["base_url"] == (
        "http://quality-auto.example:11434"
    )


def test_frozen_wsl_nat_vlm_url_is_rediscovered_on_windows(monkeypatch):
    from types import SimpleNamespace
    from app.quality_moe.config import freeze_quality_runtime_config

    monkeypatch.setattr("app.services.ollama_runtime.os.name", "nt")
    seen = []

    def ready(runtime_config):
        seen.append(runtime_config.base_url)
        return SimpleNamespace(base_url="http://172.27.228.10:11434")

    frozen = freeze_quality_runtime_config(
        {
            "vlm": {
                "base_url": "http://172.27.227.98:11434",
                "model": "llava:13b",
            },
            "quality_moe": {
                "judge": {
                    "base_url": "inherit_vlm",
                    "model_id": "llava:13b",
                }
            },
        },
        ready_resolver=ready,
    )

    assert seen == ["auto"]
    assert frozen["vlm"]["base_url"] == "http://172.27.228.10:11434"
    assert frozen["quality_moe"]["judge"]["base_url"] == (
        "http://172.27.228.10:11434"
    )


def test_max_refine_frames_default_caps_dense_adult_scores():
    cfg = extract_config({"adaptive": {}})
    assert cfg["max_refine_frames"] == DEFAULT_MAX_REFINE_FRAMES


def test_max_refine_frames_rejects_negative():
    with pytest.raises(ValueError, match="max_refine_frames"):
        extract_config({"adaptive": {"max_refine_frames": -1}})


def test_collect_refine_timestamps_respects_interval_and_existing():
    timestamps = collect_refine_timestamps(
        {10},
        radius=4,
        interval=2,
        existing_timestamps={10},
        duration_s=30,
        max_frames=0,
    )
    assert timestamps == [6, 8, 12, 14]


def test_collect_refine_timestamps_caps_evenly():
    peaks = set(range(0, 400, 10))
    timestamps = collect_refine_timestamps(
        peaks,
        radius=6,
        interval=3,
        existing_timestamps=(),
        duration_s=400,
        max_frames=20,
    )
    assert len(timestamps) == 20
    assert timestamps[0] == min(timestamps)
    assert timestamps[-1] == max(timestamps)
    assert timestamps == sorted(timestamps)
    assert len(set(timestamps)) == 20


def test_palette_defaults_reproduce_the_legacy_ffmpeg_commands():
    cfg = extract_config({"adaptive": {}})

    assert cfg["gif_palette_stats_mode"] == "full"
    assert cfg["gif_dither"] == "sierra2_4a"
    assert cfg["gif_diff_mode"] == "none"
    assert _palette_filters_for(cfg) == ("palettegen", "paletteuse")


def test_palette_overrides_flow_from_the_frozen_snapshot():
    cfg = extract_config(
        {
            "adaptive": {
                "gif_palette_stats_mode": "diff",
                "gif_dither": "sierra2_4a",
                "gif_diff_mode": "rectangle",
            }
        }
    )

    assert _palette_filters_for(cfg) == (
        "palettegen=stats_mode=diff",
        "paletteuse=dither=sierra2_4a:diff_mode=rectangle",
    )


def test_an_unknown_palette_value_is_rejected_at_config_freeze():
    with pytest.raises(ValueError, match="dither"):
        extract_config({"adaptive": {"gif_dither": "not-a-dither"}})


def test_palette_keys_stay_out_of_the_action_config_hash():
    baseline = extract_config({"adaptive": {}})
    tuned = extract_config(
        {
            "adaptive": {
                "gif_palette_stats_mode": "diff",
                "gif_diff_mode": "rectangle",
            }
        }
    )

    assert tuned["action_config_hash"] == baseline["action_config_hash"]


@pytest.mark.parametrize(
    "name", ["models.yaml", "models.adult_candidate.yaml"]
)
def test_every_shipped_config_freezes_without_raising(name):
    """The adult preset has no other coverage, so config drift lands here.

    ``extract_config`` runs strict action-boundary validation, which is how
    a tightened ``max_duration`` silently broke this preset before.
    """
    data = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "configs" / name).read_text(
            encoding="utf-8"
        )
    )

    cfg = extract_config(data)

    assert cfg["action_preferred_max_duration_s"] <= cfg["max_duration"]
    assert cfg["action_preferred_min_duration_s"] >= cfg["min_duration"]
    assert cfg["single_frame_max_duration_s"] <= cfg["max_duration"]


def test_shipped_configs_use_a_centisecond_divisible_frame_rate():
    for name in ("models.yaml", "models.adult_candidate.yaml"):
        data = yaml.safe_load(
            (Path(__file__).resolve().parents[1] / "configs" / name).read_text(
                encoding="utf-8"
            )
        )
        fps = data["adaptive"]["gif_fps"]
        assert is_divisible_gif_fps(fps), (
            f"configs/{name} gif_fps={fps} does not divide 100"
        )


def test_shipped_configs_pin_a_deterministic_scoring_seed():
    for name in ("models.yaml", "models.adult_candidate.yaml"):
        adaptive = yaml.safe_load(
            (Path(__file__).resolve().parents[1] / "configs" / name).read_text(
                encoding="utf-8"
            )
        )["adaptive"]

        assert adaptive["vlm_temperature"] == 0.0
        assert adaptive["vlm_top_p"] == 1.0
        assert adaptive["vlm_top_k"] == 1
        assert isinstance(adaptive["vlm_seed"], int)


def test_vlm_seed_changes_the_job_level_config_hash():
    """Reproducibility depends on the seed being part of the job identity."""
    unseeded = canonical_hash({"adaptive": {"sample_interval": 4}})
    seeded = canonical_hash(
        {"adaptive": {"sample_interval": 4, "vlm_seed": 20260823}}
    )

    assert unseeded != seeded


def test_an_indivisible_frame_rate_warns_once_without_raising(capsys):
    test_video_adaptive._WARNED_FPS.discard(24)

    extract_config({"adaptive": {"gif_fps": 24}})
    extract_config({"adaptive": {"gif_fps": 24}})

    warnings = [
        line
        for line in capsys.readouterr().out.splitlines()
        if "does not divide 100" in line
    ]
    assert len(warnings) == 1
    assert "25" in warnings[0]

