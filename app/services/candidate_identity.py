"""Stable identity for the same GIF across export trees and import IDs.

Folder import uses ``cand_fs_{path_hash}``. Pipeline ingest uses
``cand_{sha(video+times+clip_id)}``. EXE and repo also write the same clip
to different ``adaptive_test`` roots. Review status must follow the clip,
not the path or candidate_id.

Re-exported collections (same 合集 folder name, new clip windows) are matched
at video-title and collection-instance level so Folder to Review does not
resurface already-rated videos.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Iterable

from app.services.gif_naming import parse_clip_filename

_EXPORT_MARKERS = ("adaptive_test",)
_TITLE_STOPWORDS = frozenset(
    {
        "a", "the", "to", "and", "of", "in", "on", "my", "me", "i", "you",
        "with", "for", "this", "that", "it", "is", "was",
    }
)
_TITLE_VIDEO_JACCARD = 0.72
_RESOLUTION_SUFFIX = re.compile(
    r"\s*(1080p|2160p|720p|4k|xxx)\s*$", re.IGNORECASE
)
_COPY_SUFFIX = re.compile(r"\s*\(\d+\)\s*$")
_NON_TOKEN = re.compile(r"[^a-z0-9\u4e00-\u9fff]+")


def _norm(text: str) -> str:
    return os.path.normcase(text.replace("/", os.sep).replace("\\", os.sep))


def export_relative_key(path: Path) -> str | None:
    """Return the path below ``adaptive_test`` when that marker is present."""
    parts = path.parts
    for index, part in enumerate(parts):
        if part.casefold() in _EXPORT_MARKERS:
            if index + 1 >= len(parts):
                return None
            relative = Path(*parts[index + 1 :])
            return _norm(str(relative))
    return None


def clip_identity_keys(
    artifact_path: str | None,
    start_sec: float | None = None,
    end_sec: float | None = None,
) -> tuple[str, ...]:
    """Return lookup keys for one GIF, strongest last-writer does not matter.

    Keys, when available:
    - ``path:`` resolved absolute path
    - ``rel:`` path below ``adaptive_test`` (repo vs ``dist/GifAgentUI``)
    - ``name:`` clip filename when it uses the ``@@@`` export pattern
    - ``clip:`` parent folder + filename + rounded times
    """
    if not artifact_path:
        return ()
    path = Path(artifact_path)
    name = _norm(path.name)
    parent = _norm(path.parent.name) if path.parent.name else ""
    parsed_start, parsed_end = parse_clip_filename(path)
    start = round(float(start_sec if start_sec is not None else parsed_start), 3)
    end = round(float(end_sec if end_sec is not None else parsed_end), 3)

    keys: list[str] = [f"clip:{parent}/{name}@{start:.3f}-{end:.3f}"]
    if "@@@" in path.name:
        keys.append(f"name:{name}")
    relative = export_relative_key(path)
    if relative:
        keys.append(f"rel:{relative}@{start:.3f}-{end:.3f}")
    normalized = _norm(os.path.normpath(str(path)))
    keys.append(f"path:{normalized}@{start:.3f}-{end:.3f}")
    return tuple(dict.fromkeys(keys))


def identity_keys_for_file(path: Path) -> tuple[str, ...]:
    start, end = parse_clip_filename(path)
    return clip_identity_keys(str(path), start, end)


def status_rank(status: str | None) -> int:
    if status == "favorited":
        return 3
    if status and status != "candidate":
        return 2
    return 1


def fetch_clip_identity_rows(conn: Any) -> list[Any]:
    """Load identity fields for every candidate, including favorites."""
    rows: list[Any] = conn.execute(
        """
        SELECT c.candidate_id, c.source_run_id, c.source_run_candidate_id,
               c.start_sec, c.end_sec, c.artifact_path, c.preview_path,
               COALESCE(c.preview_path, c.artifact_path) AS display_path,
               CASE WHEN fg.candidate_id IS NOT NULL THEN 'favorited' ELSE c.status END AS status,
               c.base_rag_similarity, c.final_score, c.created_at
        FROM candidate_gifs c
        LEFT JOIN favorite_gifs fg ON fg.candidate_id = c.candidate_id
        """
    ).fetchall()
    return rows


def build_clip_identity_index(rows: Iterable[Any]) -> dict[str, Any]:
    """Map every identity key to the best row (rated/favorited beats candidate)."""
    best: dict[str, Any] = {}
    for row in rows:
        keys = clip_identity_keys(row["artifact_path"], row["start_sec"], row["end_sec"])
        for key in keys:
            previous = best.get(key)
            previous_status = None if previous is None else previous["status"]
            if previous is None or status_rank(row["status"]) > status_rank(previous_status):
                best[key] = row
    return best


def best_row_for_keys(
    index: dict[str, Any],
    keys: Iterable[str],
) -> Any | None:
    best: Any | None = None
    for key in keys:
        row = index.get(key)
        if row is None:
            continue
        if best is None or status_rank(row["status"]) > status_rank(best["status"]):
            best = row
    return best


def inherited_status(
    index: dict[str, Any],
    *,
    artifact_path: str | None,
    start_sec: float | None,
    end_sec: float | None,
    local_status: str,
) -> str:
    twin = best_row_for_keys(
        index, clip_identity_keys(artifact_path, start_sec, end_sec)
    )
    if twin is None:
        return local_status
    if status_rank(twin["status"]) > status_rank(local_status):
        return str(twin["status"])
    return local_status


def primary_identity_key(
    artifact_path: str | None,
    start_sec: float | None = None,
    end_sec: float | None = None,
) -> str:
    keys = clip_identity_keys(artifact_path, start_sec, end_sec)
    for prefix in ("name:", "rel:", "clip:", "path:"):
        for key in keys:
            if key.startswith(prefix):
                return key
    return keys[-1] if keys else f"missing:{artifact_path}"


def normalize_video_title(name: str) -> str:
    text = _RESOLUTION_SUFFIX.sub("", name.casefold().strip())
    text = _COPY_SUFFIX.sub("", text)
    text = _NON_TOKEN.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def _title_tokens(title: str) -> set[str]:
    return {
        token
        for token in normalize_video_title(title).split()
        if len(token) > 1 and token not in _TITLE_STOPWORDS
    }


def _token_jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _is_export_tree(path: Path) -> bool:
    return any(part.casefold() in _EXPORT_MARKERS for part in Path(path).parts)


def _looks_like_collection_name(name: str) -> bool:
    if not name or name.casefold() in _EXPORT_MARKERS:
        return False
    if any("\u4e00" <= character <= "\u9fff" for character in name):
        return True
    lowered = name.casefold()
    if "合集" in name or "collection" in lowered:
        return True
    return len(name.strip()) >= 24


def _canonical_folder_key(path: Path, cache: dict[str, str] | None = None) -> str:
    """Stable folder identity that collapses junction/symlink aliases."""
    raw = _norm(os.path.normpath(str(path)))
    if cache is not None and raw in cache:
        return cache[raw]
    try:
        key = _norm(str(Path(path).resolve(strict=False)))
    except OSError:
        key = raw
    if cache is not None:
        cache[raw] = key
        cache.setdefault(key, key)
    return key


def collection_instance_for_video_folder(
    folder: Path,
    cache: dict[str, str] | None = None,
) -> tuple[str, str] | None:
    """Return (collection_name, instance_root) when the video sits in a 合集 folder."""
    collection_dir = Path(folder).parent
    name = collection_dir.name
    if not _looks_like_collection_name(name):
        return None
    return _norm(name), _canonical_folder_key(collection_dir, cache)


class DuplicateReviewIndex:
    """Video-title and collection copies of already-rated GIF folders."""

    def __init__(self, rows: Iterable[Any]) -> None:
        self._folder_key_cache: dict[str, str] = {}
        self.video_rated: dict[str, dict[str, str]] = {}
        self.collection_rated: dict[str, dict[str, int]] = {}
        for row in rows:
            path_value = row["artifact_path"]
            if not path_value:
                continue
            status = row["status"]
            folder = Path(path_value).parent
            folder_key = _canonical_folder_key(folder, self._folder_key_cache)
            title = normalize_video_title(folder.name)
            if title and status_rank(status) >= 2:
                by_folder = self.video_rated.setdefault(title, {})
                previous = by_folder.get(folder_key)
                if previous is None or status_rank(status) > status_rank(previous):
                    by_folder[folder_key] = status
            instance = collection_instance_for_video_folder(folder, self._folder_key_cache)
            if instance is not None and status_rank(status) >= 2:
                collection_name, instance_root = instance
                by_instance = self.collection_rated.setdefault(collection_name, {})
                by_instance[instance_root] = by_instance.get(instance_root, 0) + 1

    def shadow_status(self, folder: Path) -> str | None:
        """Return a rated status if this video folder is a duplicate export."""
        folder = Path(folder)
        if not _is_export_tree(folder):
            return None
        folder_key = _canonical_folder_key(folder, self._folder_key_cache)
        title = normalize_video_title(folder.name)
        rated = self.video_rated.get(title, {})
        for other_folder, status in rated.items():
            if other_folder != folder_key:
                return status
        instance = collection_instance_for_video_folder(folder, self._folder_key_cache)
        if instance is None:
            return None
        collection_name, instance_root = instance
        tokens = _title_tokens(folder.name)
        if tokens:
            for other_title, other_rated in self.video_rated.items():
                if other_title == title:
                    continue
                if _token_jaccard(tokens, _title_tokens(other_title)) < _TITLE_VIDEO_JACCARD:
                    continue
                for other_folder, status in other_rated.items():
                    other_instance = collection_instance_for_video_folder(
                        Path(other_folder), self._folder_key_cache
                    )
                    if (
                        other_instance is not None
                        and other_instance[0] == collection_name
                        and other_instance[1] != instance_root
                    ):
                        return status
        counts = self.collection_rated.get(collection_name, {})
        local = counts.get(instance_root, 0)
        elsewhere = max(
            (count for root, count in counts.items() if root != instance_root),
            default=0,
        )
        if elsewhere > local:
            return "liked"
        return None


def build_duplicate_review_index(rows: Iterable[Any]) -> DuplicateReviewIndex:
    return DuplicateReviewIndex(rows)
