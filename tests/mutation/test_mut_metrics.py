"""W4 kill tests for quality_lab.metrics mutation survivors.

Targets survivors from the cosmic-ray run recorded in
``<sprint-root>/w4/mutation_scores/cosmic-ray/metrics.json``:

- ``ndcg_at_k``: negative-relevance inputs make IDCG < 0; the guard is
  ``idcg <= 0.0`` (an ``== 0.0`` mutant would divide by it and return 1.0).
- ``temporal_coverage``: merge decisions must read/update the LAST merged
  interval (``merged[-1]``) — three-interval scenarios where merging into
  ``merged[0]`` changes the covered measure; sub-second durations pin the
  ``duration <= 0.0`` guard.
- ``diversity_score``: float32 self-dot can exceed 1.0 by rounding; the
  ``np.clip(..., 1.0)`` upper bound pins identical-vector diversity at 0.0.
- ``export_integrity``: negative attempt counts return 1.0 and a single
  failed attempt returns 0.0 (guards ``attempted <= 0`` and the ratio).

Equivalent mutants (``norms == 0.0`` vs ``<= 0`` on non-negative norms, the
zero-norm substitute value 1.0 vs 2.0, ``k <= 0`` variants whose k=0 path
returns 0.0 via the IDCG guard, adjacency ``>`` vs ``>=`` in interval
merging which is measure-preserving) are registered in
``docs/reports/2026-08-token-sprint/w4-surviving.md``.
"""

from __future__ import annotations

import numpy as np
import pytest

from app.quality_lab.metrics import (
    diversity_score,
    export_integrity,
    ndcg_at_k,
    temporal_coverage,
)


def test_ndcg_with_all_negative_relevances_is_zero():
    # IDCG < 0: the ``idcg <= 0.0`` guard (not ``== 0.0``) must return 0.0.
    assert ndcg_at_k([-1.0, -2.0], 2) == 0.0


def test_temporal_coverage_merges_into_the_last_interval():
    # (0,5) then (10,20) then (12,30): the third interval extends the SECOND
    # merged interval. Reading merged[0] instead of merged[-1] would count
    # 0.33-0.35 of the window instead of 0.25.
    covered = temporal_coverage([(0.0, 5.0), (10.0, 20.0), (12.0, 30.0)], 100.0)
    assert covered == pytest.approx(0.25)


def test_temporal_coverage_accepts_sub_second_durations():
    # duration 0.5 is positive: the ``duration <= 0.0`` guard (not <= 1.0)
    # must let the interval count, capped at 1.0.
    assert temporal_coverage([(0.0, 1.0)], 0.5) == pytest.approx(1.0)


def test_diversity_of_identical_vectors_is_zero_even_under_float32_rounding():
    # The self-dot of this normalized float32 vector is 1.0000001192 > 1.0;
    # the np.clip upper bound at 1.0 must keep the identical-pair distance 0.
    v = np.array(
        [36.15950393676758, 130.40000915527344, 94.70809936523438, -70.37351989746094],
        dtype=np.float32,
    )
    matrix = np.stack([v, v.copy()])
    u = (matrix[0] / np.linalg.norm(matrix[0])).astype(np.float32)
    # sanity: the raw self-dot really does exceed 1.0 (else test is vacuous)
    assert float(np.dot(u, u)) > 1.0
    assert diversity_score(matrix) == 0.0


def test_export_integrity_boundary_counts():
    assert export_integrity(0, 0) == 1.0
    assert export_integrity(-1, 0) == 1.0  # non-positive attempts: 1.0
    assert export_integrity(1, 0) == 0.0  # one failed attempt is NOT 1.0
    assert export_integrity(4, 1) == pytest.approx(0.25)
