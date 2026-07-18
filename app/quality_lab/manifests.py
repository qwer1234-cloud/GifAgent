from __future__ import annotations

import hashlib
import json
import random
from pathlib import Path
from typing import Sequence

from app.quality_lab.models import BenchmarkItem, BenchmarkManifest, Split
from app.task_engine.fingerprints import canonical_json


def freeze_manifest(
    items: Sequence[BenchmarkItem],
    output: Path,
    *,
    version: int = 1,
) -> str:
    """Write an immutable JSON manifest for the given benchmark items.

    Items must already have their ``split`` assigned.  The manifest ID is
    the SHA-256 digest of the JSON content (excluding the self-referential
    *manifest_id* field).

    Returns the computed manifest ID.
    """
    items_tuple = tuple(items)
    _validate_items(items_tuple)

    # Compute manifest_id from the content hash (excluding manifest_id field).
    manifest_id = _compute_content_id(
        version, _items_to_dicts(items_tuple)
    )

    # Rebuild with the real manifest_id and write.
    final = _manifest_dict(items_tuple, version=version, manifest_id=manifest_id)
    output.write_text(canonical_json(final), encoding="utf-8")

    return manifest_id


def load_manifest(
    path: Path,
    *,
    verify_files: bool = True,
) -> BenchmarkManifest:
    """Load a frozen manifest from *path* and verify its integrity.

    When *verify_files* is ``True``, every ``source_path`` must exist on
    disk (used for moved-file detection).
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    manifest_id = raw["manifest_id"]
    version = raw["version"]

    # Verify content hash.
    expected_id = _compute_content_id(raw["version"], raw["items"])
    if manifest_id != expected_id:
        raise ValueError(
            f"Manifest ID mismatch: file says {manifest_id}, "
            f"content hash is {expected_id}"
        )

    items = []
    seen_fingerprints: set[str] = set()
    for entry in raw["items"]:
        item = BenchmarkItem(
            item_id=entry["item_id"],
            source_path=entry["source_path"],
            video_fingerprint=entry["video_fingerprint"],
            duration_bucket=entry["duration_bucket"],
            resolution_bucket=entry["resolution_bucket"],
            pace_bucket=entry["pace_bucket"],
            difficulty_tags=tuple(entry.get("difficulty_tags", [])),
            split=entry["split"],
        )
        if item.video_fingerprint in seen_fingerprints:
            raise ValueError(
                f"Duplicate video_fingerprint {item.video_fingerprint} "
                f"in manifest {manifest_id}"
            )
        seen_fingerprints.add(item.video_fingerprint)
        items.append(item)

        if verify_files:
            src = Path(item.source_path)
            if not src.exists():
                raise FileNotFoundError(
                    f"Source file not found: {item.source_path} "
                    f"(item {item.item_id})"
                )

    return BenchmarkManifest(
        manifest_id=manifest_id,
        version=version,
        items=tuple(items),
    )


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def assign_splits(
    items: Sequence[BenchmarkItem],
) -> list[BenchmarkItem]:
    """Deterministically assign tune/holdout splits.

    Items that share the same ``video_fingerprint`` always receive the
    same split.  The seed is derived from all items' content so that the
    split is purely a function of the benchmark contents.
    """
    seed = _compute_split_seed(items)
    rng = random.Random(seed)

    # Group by fingerprint.
    groups: dict[str, list[BenchmarkItem]] = {}
    order: list[str] = []
    for item in items:
        fp = item.video_fingerprint
        if fp not in groups:
            order.append(fp)
        groups.setdefault(fp, []).append(item)

    fp_split: dict[str, Split] = {}
    for fp in order:
        fp_split[fp] = "tune" if rng.random() < 0.70 else "holdout"

    result: list[BenchmarkItem] = []
    for item in items:
        split = fp_split[item.video_fingerprint]
        result.append(
            BenchmarkItem(
                item_id=item.item_id,
                source_path=item.source_path,
                video_fingerprint=item.video_fingerprint,
                duration_bucket=item.duration_bucket,
                resolution_bucket=item.resolution_bucket,
                pace_bucket=item.pace_bucket,
                difficulty_tags=item.difficulty_tags,
                split=split,
            )
        )
    return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _validate_items(items: tuple[BenchmarkItem, ...]) -> None:
    seen: set[str] = set()
    for item in items:
        if item.video_fingerprint in seen:
            raise ValueError(
                f"Duplicate video_fingerprint: {item.video_fingerprint}"
            )
        seen.add(item.video_fingerprint)


def _manifest_dict(
    items: tuple[BenchmarkItem, ...],
    *,
    version: int,
    manifest_id: str,
) -> dict:
    return {
        "manifest_id": manifest_id,
        "version": version,
        "items": _items_to_dicts(items),
    }


def _compute_content_id(version: int, items: list[dict]) -> str:
    """SHA-256 of the manifest content *without* the manifest_id field."""
    payload = {"version": version, "items": items}
    return hashlib.sha256(
        canonical_json(payload).encode("utf-8")
    ).hexdigest()


def _items_to_dicts(items: tuple[BenchmarkItem, ...]) -> list[dict]:
    """Serialise items to the dict form used in the manifest JSON."""
    return [
        {
            "item_id": item.item_id,
            "source_path": item.source_path,
            "video_fingerprint": item.video_fingerprint,
            "duration_bucket": item.duration_bucket,
            "resolution_bucket": item.resolution_bucket,
            "pace_bucket": item.pace_bucket,
            "difficulty_tags": list(item.difficulty_tags),
            "split": item.split,
        }
        for item in items
    ]


def _compute_split_seed(items: Sequence[BenchmarkItem]) -> int:
    """Deterministic seed derived from item content only (no split)."""
    sorted_items = sorted(items, key=lambda i: i.video_fingerprint)
    parts = [
        f"{i.video_fingerprint}:{i.duration_bucket}:"
        f"{i.resolution_bucket}:{i.pace_bucket}"
        for i in sorted_items
    ]
    content = "|".join(parts)
    return int(hashlib.sha256(content.encode()).hexdigest()[:16], 16)
