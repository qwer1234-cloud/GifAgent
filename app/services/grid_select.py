"""Time-spread 9-grid selection for adaptive sample thumbnails."""

from __future__ import annotations

from typing import Any, Callable, Sequence


def _timestamp(frame: dict[str, Any]) -> float:
    return float(frame.get("timestamp") or 0.0)


def _score(frame: dict[str, Any]) -> float:
    return float(frame.get("gif_worthiness") or 0.0)


def select_grid_frames(
    frames: Sequence[dict[str, Any]],
    *,
    count: int = 9,
    phash_fn: Callable[[dict[str, Any]], Any] | None = None,
    phash_threshold: int = 10,
) -> list[dict[str, Any]]:
    """Pick ``count`` frames spread across the timeline, then pHash-dedup.

    When scores are tied, a score-desc sort is stable in time order and
    fills the grid from the start of the film. Bucketing first keeps one
    representative in each time slice.
    """
    if count <= 0:
        return []
    valid = [frame for frame in frames if frame.get("path")]
    if not valid:
        return []

    timestamps = [_timestamp(frame) for frame in valid]
    t_min = min(timestamps)
    t_max = max(timestamps)
    buckets: list[list[dict[str, Any]]] = [[] for _ in range(count)]
    if t_max <= t_min:
        buckets[0] = list(valid)
    else:
        span = t_max - t_min
        for frame in valid:
            index = int((_timestamp(frame) - t_min) / span * count)
            if index >= count:
                index = count - 1
            buckets[index].append(frame)

    selected: list[dict[str, Any]] = []
    hashes: list[Any] = []

    def accept(frame: dict[str, Any]) -> bool:
        if phash_fn is None:
            return True
        digest = phash_fn(frame)
        if digest is None:
            return False
        if any(digest - existing <= phash_threshold for existing in hashes):
            return False
        hashes.append(digest)
        return True

    selected_ids = set()
    for bucket in buckets:
        for frame in sorted(bucket, key=_score, reverse=True):
            marker = id(frame)
            if marker in selected_ids:
                continue
            if accept(frame):
                selected.append(frame)
                selected_ids.add(marker)
                break

    if len(selected) < count:
        leftovers = [
            frame for frame in sorted(valid, key=_score, reverse=True)
            if id(frame) not in selected_ids
        ]
        for frame in leftovers:
            if len(selected) >= count:
                break
            if accept(frame):
                selected.append(frame)
                selected_ids.add(id(frame))
    return selected
