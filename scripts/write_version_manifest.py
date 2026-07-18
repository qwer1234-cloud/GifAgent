#!/usr/bin/env python3
"""Write a version manifest JSON file to the dist directory.

Usage:
    uv run python scripts/write_version_manifest.py --dist <dist_dir>
    uv run python scripts/write_version_manifest.py --dist <dist_dir> --output MANIFEST.json

The manifest captures git commit, Python version, config schema version, and
SHA-256 hashes for key packaged files.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class FileEntry:
    sha256: str
    size_bytes: int


@dataclass
class Manifest:
    git_commit: str
    git_dirty: bool
    config_schema_version: str
    task_migration_version: int
    python_version: str
    created_at: str
    files: dict[str, FileEntry]
    files_missing: list[str]


def _git_commit() -> str:
    try:
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10,
            cwd=root,
        )
        commit = result.stdout.strip()
        return commit if result.returncode == 0 and commit else "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _git_dirty() -> bool:
    try:
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, timeout=10,
            cwd=root,
        )
        return bool(result.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        return True


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _collect_files(dist_dir: Path) -> tuple[dict[str, FileEntry], list[str]]:
    candidates = [
        "GifAgentUI.exe",
        "_internal/app/task_engine/repository.py",
    ]
    files: dict[str, FileEntry] = {}
    missing: list[str] = []
    for rel in candidates:
        abspath = dist_dir / rel
        if abspath.exists() and abspath.is_file():
            files[rel] = FileEntry(
                sha256=_sha256_file(abspath),
                size_bytes=abspath.stat().st_size,
            )
        else:
            missing.append(rel)
    return files, missing


def build_manifest(dist_dir: str | Path) -> Manifest:
    dist_dir = Path(dist_dir).resolve()
    files, missing = _collect_files(dist_dir)
    return Manifest(
        git_commit=_git_commit(),
        git_dirty=_git_dirty(),
        config_schema_version="1.0",
        task_migration_version=1,
        python_version=sys.version.split()[0],
        created_at=datetime.now(timezone.utc).isoformat(),
        files=files,
        files_missing=missing,
    )


def manifest_to_dict(m: Manifest) -> dict:
    d = asdict(m)
    # Convert FileEntry dataclasses to plain dicts for JSON serialisation
    d["files"] = {k: asdict(v) for k, v in m.files.items()}
    return d


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Write version manifest for GifAgent dist build"
    )
    parser.add_argument(
        "--dist", required=True,
        help="Path to the dist directory (e.g. dist/GifAgentUI)",
    )
    parser.add_argument(
        "--output", default="version_manifest.json",
        help="Output filename (relative to --dist, default: version_manifest.json)",
    )
    args = parser.parse_args()

    dist_dir = Path(args.dist).resolve()
    if not dist_dir.is_dir():
        print(f"Warning: dist directory does not exist: {dist_dir}", file=sys.stderr)
        print("Creating manifest with no file hashes.", file=sys.stderr)

    manifest = build_manifest(dist_dir)
    out_path = dist_dir / args.output if dist_dir.is_dir() else Path(args.output)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(manifest_to_dict(manifest), f, indent=2, ensure_ascii=False)
        f.write("\n")

    print(f"Version manifest written to {out_path}")
    print(f"  git_commit: {manifest.git_commit}")
    print(f"  git_dirty: {manifest.git_dirty}")
    print(f"  python_version: {manifest.python_version}")
    print(f"  files recorded: {len(manifest.files)}")
    if manifest.files_missing:
        print(f"  files missing: {manifest.files_missing}")


if __name__ == "__main__":
    main()
