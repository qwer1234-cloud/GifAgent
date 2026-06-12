import yaml
import os
from pathlib import Path
from typing import Any

_config: dict[str, Any] = {}

def load_config(path: str = "configs/models.yaml") -> dict[str, Any]:
    global _config
    with open(path, "r") as f:
        _config = yaml.safe_load(f)
    return _config

def get(key: str, default: Any = None) -> Any:
    keys = key.split(".")
    val: Any = _config
    for k in keys:
        if isinstance(val, dict):
            val = val.get(k)
        else:
            return default
    return val if val is not None else default

_config_path = os.environ.get("GIFAGENT_CONFIG", "configs/models.yaml")
if Path(_config_path).exists():
    load_config(_config_path)
