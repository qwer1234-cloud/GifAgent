from pathlib import Path

import yaml


def test_adaptive_max_duration_is_configured_with_default_10():
    project_root = Path(__file__).resolve().parents[1]

    config = yaml.safe_load((project_root / "configs" / "models.yaml").read_text(encoding="utf-8"))
    assert config["adaptive"]["max_duration"] == 10

    script = (project_root / "scripts" / "test_video_adaptive.py").read_text(encoding="utf-8")
    assert 'MAX_DURATION = float(_adaptive.get("max_duration", 10))' in script
    assert "MAX_DURATION = 5.0" not in script
