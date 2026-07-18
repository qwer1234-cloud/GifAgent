from pathlib import Path

import yaml


def test_adaptive_max_duration_is_configured_with_default_10():
    project_root = Path(__file__).resolve().parents[1]

    config = yaml.safe_load((project_root / "configs" / "models.yaml").read_text(encoding="utf-8"))
    assert config["adaptive"]["max_duration"] == 10

    script = (project_root / "scripts" / "test_video_adaptive.py").read_text(encoding="utf-8")
    # Config extraction must default max_duration to 10, not some other value
    assert '"max_duration", 10)' in script
    assert '"max_duration", 5.0)' not in script
