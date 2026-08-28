"""Shared L2-normalization and small-k clustering for preference vectors."""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

_UNIT_NORM_TOL = 1e-5


def l2_normalize(vec: np.ndarray | Sequence[float]) -> np.ndarray:
    """Return a float32 unit vector. A zero vector is returned unchanged."""
    arr = np.array(vec, dtype=np.float32, copy=True).reshape(-1)
    norm = float(np.linalg.norm(arr))
    if norm > 0:
        arr /= np.float32(norm)
    return arr


def is_unit_vector(vec: np.ndarray | Sequence[float], *, tol: float = _UNIT_NORM_TOL) -> bool:
    arr = np.asarray(vec, dtype=np.float32).reshape(-1)
    norm = float(np.linalg.norm(arr))
    if norm == 0.0:
        return True
    return abs(norm - 1.0) <= tol


def vector_to_blob(vector: np.ndarray | Sequence[float], *, embedding_dim: int) -> bytes:
    """Serialize an L2-normalized float32 vector of *embedding_dim*."""
    arr = l2_normalize(vector)
    if arr.size != embedding_dim:
        raise ValueError(
            f"embedding_dim mismatch: got {arr.size}, expected {embedding_dim}"
        )
    return arr.astype(np.float32, copy=False).tobytes()


def blob_to_vector(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


def max_cosine(candidate: np.ndarray | Sequence[float], centroid_blob: bytes) -> float:
    """Cosine similarity of *candidate* to one or more packed centroids.

    A blob whose length equals the candidate dimension is a single centroid.
    Longer blobs are interpreted as ``k = size // dim`` concatenated centroids;
    the maximum similarity is returned.
    """
    query = l2_normalize(candidate)
    dim = int(query.shape[0])
    packed = blob_to_vector(centroid_blob)
    if packed.size == 0 or dim <= 0:
        return 0.0
    if packed.size == dim:
        return float(np.dot(query, l2_normalize(packed)))
    if packed.size % dim != 0:
        return float(np.dot(query, l2_normalize(packed[:dim])))
    prototypes = packed.reshape(-1, dim)
    sims = [float(np.dot(query, l2_normalize(row))) for row in prototypes]
    return max(sims)


def weighted_kmeans(
    vectors: np.ndarray,
    weights: np.ndarray,
    k: int,
    *,
    seed: int = 0,
    n_iter: int = 25,
) -> np.ndarray:
    """Deterministic weighted k-means. Returns shape ``(k, dim)`` float32.

    *k* is clamped to ``[1, n]``. Empty or all-zero weights raise ``ValueError``.
    """
    if vectors.ndim != 2 or vectors.shape[0] == 0:
        raise ValueError("vectors must be a non-empty 2-D array")
    n, dim = vectors.shape
    if weights.shape != (n,):
        raise ValueError("weights must be a 1-D array matching vectors")
    weight_sum = float(np.sum(weights))
    if weight_sum <= 0:
        raise ValueError("weights must sum to a positive value")

    k = max(1, min(int(k), n))
    stacked = np.asarray(vectors, dtype=np.float64)
    w = np.asarray(weights, dtype=np.float64)

    if k == 1:
        centroid = np.average(stacked, axis=0, weights=w)
        return centroid.astype(np.float32).reshape(1, dim)

    rng = np.random.default_rng(seed)
    centers = np.empty((k, dim), dtype=np.float64)
    probs = w / w.sum()
    centers[0] = stacked[int(rng.choice(n, p=probs))]
    for c in range(1, k):
        delta = stacked[:, None, :] - centers[None, :c, :]
        dists = np.min(np.sum(delta * delta, axis=2), axis=1)
        weighted = np.maximum(dists, 0.0) * w
        total = float(weighted.sum())
        if total <= 0:
            idx = int(rng.integers(0, n))
        else:
            idx = int(rng.choice(n, p=weighted / total))
        centers[c] = stacked[idx]

    for _ in range(n_iter):
        delta = stacked[:, None, :] - centers[None, :, :]
        labels = np.sum(delta * delta, axis=2).argmin(axis=1)
        new_centers = centers.copy()
        for c in range(k):
            mask = labels == c
            if not np.any(mask):
                continue
            new_centers[c] = np.average(stacked[mask], axis=0, weights=w[mask])
        if np.allclose(new_centers, centers):
            break
        centers = new_centers
    return centers.astype(np.float32)
