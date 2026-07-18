from __future__ import annotations

import hashlib
import json
from pathlib import Path


def canonical_json(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def canonical_hash(obj: object) -> str:
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def fingerprint_video(path: str | Path, block_bytes: int = 1_048_576) -> str:
    p = Path(path)
    stat = p.stat()
    size = stat.st_size
    h = hashlib.sha256()
    with p.open("rb") as f:
        h.update(f.read(block_bytes))
        if size > block_bytes:
            f.seek(size - block_bytes)
            h.update(f.read(block_bytes))
    return f"{size}:{stat.st_mtime_ns}:{h.hexdigest()}"


def build_stage_input_key(
    *,
    video_fingerprint: str,
    config: dict,
    models: dict[str, str],
    stage_name: str,
    stage_version: str,
) -> str:
    return canonical_hash({
        "video_fingerprint": video_fingerprint,
        "config": config,
        "models": models,
        "stage_name": stage_name,
        "stage_version": stage_version,
    })


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1_048_576), b""):
            h.update(chunk)
    return h.hexdigest()
