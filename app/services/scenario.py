from __future__ import annotations

import json
from typing import Iterable


def normalize_scenario_keys(
    *, emotion: str | None, scene_type: str | None, tags: Iterable[str]
) -> list[str]:
    keys: set[str] = set()
    if emotion:
        keys.add(f"emotion:{emotion.strip().lower()}")
    if scene_type:
        keys.add(f"scene:{scene_type.strip().lower()}")
    for tag in tags:
        clean = tag.strip().lower()
        if clean:
            keys.add(f"tag:{clean}")
    return sorted(keys)


def json_dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)
