"""Phase 4 Task 7: Taste Map — 2D projection of candidate vectors.

``project_taste_map`` uses NumPy SVD (not scikit-learn) to project
high-dimensional embedding vectors into two dimensions for visualisation
in a "taste map" scatter plot.

The projection is deterministic: centred normalised vectors are decomposed
with ``numpy.linalg.svd`` and the first two right-singular directions are
used.  Sign is stabilised by forcing each component's largest absolute
loading to be positive.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class TastePoint:
    """A single point in 2D taste-map space."""

    candidate_id: str
    x: float
    y: float


def project_taste_map(
    vectors: np.ndarray,
    candidate_ids: Sequence[str],
    *,
    seed: int = 0,
) -> list[TastePoint]:
    """Project *vectors* into 2D taste space via centred SVD.

    Parameters
    ----------
    vectors:
        Float32 array of shape ``(n, d)`` where each row is a normalised
        vector (or at least finite-valued).
    candidate_ids:
        Sequence of *n* candidate IDs matching the rows of *vectors*.
    seed:
        Random seed (present for interface consistency; has no effect when
        ``n >= 3`` because SVD itself is deterministic modulo sign).

    Returns
    -------
    list[TastePoint]
        One point per input vector.  Empty when *n == 0*.  A single point
        is placed at ``(0.0, 0.0)``.

    Raises
    ------
    ValueError
        If the length of *candidate_ids* and the first dimension of
        *vectors* do not match.
    """
    n = vectors.shape[0]

    if len(candidate_ids) != n:
        raise ValueError(
            f"candidate_ids length ({len(candidate_ids)}) does not match "
            f"vectors first dimension ({n})"
        )

    if n == 0:
        return []

    if n == 1:
        # A single point cannot be centred meaningfully — place at origin.
        return [TastePoint(candidate_id=candidate_ids[0], x=0.0, y=0.0)]

    # --- centre the data (subtract the mean) ---
    mean = vectors.mean(axis=0, keepdims=True)
    centred = vectors - mean

    # --- SVD on the centred matrix ---
    # For (n, d) with n ≪ d this is more efficient than a full eigendecomposition.
    # full_matrices=False returns at most min(n, d) singular vectors.
    _U, _S, Vt = np.linalg.svd(centred, full_matrices=False)

    # Project onto the first 2 right-singular vectors.
    # Vt has shape (min(n, d), d); we take the first 2 rows.
    coords = centred @ Vt[:2, :].T  # (n, 2)

    # --- sign stabilisation ---
    # For each component (column), find the entry with the largest absolute
    # value and flip the sign of the entire column if that entry is negative.
    for col_idx in range(coords.shape[1]):
        col = coords[:, col_idx]
        max_abs_idx = np.argmax(np.abs(col))
        if col[max_abs_idx] < 0:
            coords[:, col_idx] = -col

    return [
        TastePoint(
            candidate_id=candidate_ids[i],
            x=float(coords[i, 0]),
            y=float(coords[i, 1]),
        )
        for i in range(n)
    ]
