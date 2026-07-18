#!/usr/bin/env python3
"""Create a frozen benchmark manifest from a CSV item list.

Usage:
    uv run python scripts/create_benchmark_manifest.py <items.csv> <output.json>
    uv run python scripts/create_benchmark_manifest.py <items.csv> <output.json> --new-version
    uv run python scripts/create_benchmark_manifest.py --help
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.quality_lab import BenchmarkItem, freeze_manifest
from app.quality_lab.manifests import assign_splits


def _parse_tags(raw: str) -> tuple[str, ...]:
    """Parse the difficulty_tags column (``|``-separated)."""
    return tuple(t.strip() for t in raw.split("|") if t.strip())


def _derive_item_id(source_path: str) -> str:
    """Derive a short item ID from the filename stem."""
    return Path(source_path).stem


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create a frozen benchmark manifest from a CSV item list."
    )
    parser.add_argument(
        "csv_path",
        type=Path,
        help="Path to CSV file with columns: "
             "source_path,duration_bucket,resolution_bucket,"
             "pace_bucket,difficulty_tags,video_fingerprint",
    )
    parser.add_argument(
        "output",
        type=Path,
        help="Output path for the frozen JSON manifest.",
    )
    parser.add_argument(
        "--new-version",
        action="store_true",
        help="Allow overwriting an existing manifest file by incrementing "
             "the version number.",
    )
    args = parser.parse_args()

    if args.output.exists() and not args.new_version:
        print(
            f"Error: {args.output} already exists. "
            "Use --new-version to overwrite with an incremented version.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Determine the version for the output file.
    if args.output.exists():
        import json
        existing = json.loads(args.output.read_text(encoding="utf-8"))
        version = existing.get("version", 0) + 1
    else:
        version = 1

    # Read CSV.
    items: list[BenchmarkItem] = []
    with open(args.csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            source_path = row["source_path"].strip()
            item_id = _derive_item_id(source_path)
            items.append(
                BenchmarkItem(
                    item_id=item_id,
                    source_path=source_path,
                    video_fingerprint=row["video_fingerprint"].strip(),
                    duration_bucket=row["duration_bucket"].strip(),
                    resolution_bucket=row["resolution_bucket"].strip(),
                    pace_bucket=row["pace_bucket"].strip(),
                    difficulty_tags=_parse_tags(row["difficulty_tags"]),
                    split="tune",
                )
            )

    if not items:
        print("Error: CSV contains no items.", file=sys.stderr)
        sys.exit(1)

    # Assign splits deterministically.
    assigned = assign_splits(items)

    # Freeze and write.
    manifest_id = freeze_manifest(assigned, args.output, version=version)
    tune_count = sum(1 for i in assigned if i.split == "tune")
    holdout_count = sum(1 for i in assigned if i.split == "holdout")

    print(
        f"Manifest {manifest_id[:12]}... written to {args.output} "
        f"(v{version}, {len(assigned)} items: "
        f"{tune_count} tune / {holdout_count} holdout)"
    )


if __name__ == "__main__":
    main()
