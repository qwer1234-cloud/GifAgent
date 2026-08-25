"""Clip-level deduplication helpers for adaptive GIF export."""

from __future__ import annotations

from typing import Any, Sequence

import numpy as np


def _clip_score(clip: dict[str, Any]) -> float:
    return float(clip.get("final_score") or clip.get("gif_worthiness") or 0.0)


def _clip_peak_ts(clip: dict[str, Any]) -> float:
    best_frame = clip.get("best_frame")
    if isinstance(best_frame, dict) and best_frame.get("timestamp") is not None:
        return float(best_frame["timestamp"])
    if clip.get("start_ts") is not None and clip.get("end_ts") is not None:
        return (float(clip["start_ts"]) + float(clip["end_ts"])) / 2.0
    return float(clip.get("start_ts") or 0.0)


def temporal_dedup_clips(
    clips: list[dict[str, Any]],
    *,
    min_gap_s: float,
) -> list[dict[str, Any]]:
    """Keep the highest-scored clip within each peak-time window."""
    if min_gap_s <= 0 or len(clips) <= 1:
        return clips

    kept: list[dict[str, Any]] = []
    kept_peaks: list[float] = []
    for clip in sorted(clips, key=_clip_score, reverse=True):
        peak = _clip_peak_ts(clip)
        if any(abs(peak - kept_peak) <= min_gap_s for kept_peak in kept_peaks):
            continue
        kept.append(clip)
        kept_peaks.append(peak)
    return kept


def _cosine_similarity(left: Any, right: Any) -> float:
    vector_a = np.asarray(left, dtype=np.float32)
    vector_b = np.asarray(right, dtype=np.float32)
    denom = float(np.linalg.norm(vector_a) * np.linalg.norm(vector_b)) + 1e-8
    return float(np.dot(vector_a, vector_b) / denom)


def embedding_dedup_clips(
    clips: Sequence[dict[str, Any]],
    embeddings: Sequence[Any],
    *,
    threshold: float,
    max_gap_s: float = 0.0,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Drop caption-near-duplicates, optionally only when they are nearby.

    ``max_gap_s <= 0`` is the historical rule: any pair at or above
    ``threshold`` collapses, even if the peaks are minutes apart.
    A positive gap keeps distant caption twins as separate scenes.
    """
    if len(clips) != len(embeddings):
        raise ValueError("embeddings must align one-to-one with clips")
    if len(clips) <= 1:
        return list(clips), []

    order = sorted(
        range(len(clips)),
        key=lambda index: _clip_score(clips[index]),
        reverse=True,
    )
    kept_indices: list[int] = []
    groups: list[dict[str, Any]] = []
    for idx in order:
        emb = embeddings[idx]
        if emb is None:
            kept_indices.append(idx)
            continue
        is_dup = False
        peak = _clip_peak_ts(clips[idx])
        for keeper in kept_indices:
            other = embeddings[keeper]
            if other is None:
                continue
            sim = _cosine_similarity(emb, other)
            if sim < threshold:
                continue
            if max_gap_s > 0:
                other_peak = _clip_peak_ts(clips[keeper])
                if abs(peak - other_peak) > max_gap_s:
                    continue
            is_dup = True
            for group in groups:
                if group["keeper"] == keeper:
                    group["duplicates"].append(idx)
                    group["max_sim"] = max(group["max_sim"], sim)
                    break
            else:
                groups.append(
                    {"keeper": keeper, "duplicates": [idx], "max_sim": sim}
                )
            break
        if not is_dup:
            kept_indices.append(idx)
    return [clips[idx] for idx in kept_indices], groups
