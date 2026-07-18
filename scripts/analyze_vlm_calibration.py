#!/usr/bin/env python3
"""Analyse VLM score calibration: reliability diagram, isotonic regression,
ranking AUC, and an advisory recommendation on threshold strategy.

Usage
-----
    uv run python scripts/analyze_vlm_calibration.py [--bins N] SCORES_JSON LABELS_JSON

The two JSON files must contain arrays of the same length:
- *scores* — VLM-predicted probabilities in ``[0, 1]``.
- *labels* — binary ground-truth judgments (0 = bad, 1 = good).

Output
------
Writes the following to stdout:

    Calibration curve    (reliability bins)
    Distribution stats   (min, max, mean, median, quantiles)
    MonotonicCalibrator  (thresholds and calibrated values)
    Ranking AUC          (probability a random positive outranks a random negative)
    Recommendation       (among: fixed-threshold, percentile-threshold, monotonic-calibration)

The recommendation is advisory — it is based on simple heuristics and is
not a substitute for human judgement.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.quality_lab.calibration import (
    calibration_curve,
    fit_monotonic_calibrator,
)
from app.quality_lab.metrics import ndcg_at_k


# ---------------------------------------------------------------------------
# Ranking AUC (Mann-Whitney U)
# ---------------------------------------------------------------------------


def ranking_auc(scores: Sequence[float], labels: Sequence[int]) -> float:
    """Compute the area under the ROC curve via the Mann-Whitney U statistic.

    AUC is the probability that a randomly chosen positive is ranked
    higher than a randomly chosen negative.  Returns 0.5 for random
    performance, 1.0 for perfect separation.

    The implementation uses the U-statistic definition to avoid an
    explicit sort of the full array.
    """
    if not scores or not labels:
        return 0.5

    pos_scores = [s for s, lbl in zip(scores, labels) if lbl == 1]
    neg_scores = [s for s, lbl in zip(scores, labels) if lbl == 0]

    n_pos = len(pos_scores)
    n_neg = len(neg_scores)

    if n_pos == 0 or n_neg == 0:
        return 0.5

    # Mann-Whitney U: for each positive, count how many negatives it
    # outranks (lower score = higher rank in this context).  The AUC
    # is U / (n_pos * n_neg).
    pos_scores_sorted = sorted(pos_scores)
    neg_scores_sorted = sorted(neg_scores)

    # Count pairs where positive score > negative score
    u_stat = 0
    j = 0
    for ps in pos_scores_sorted:
        while j < n_neg and neg_scores_sorted[j] < ps:
            j += 1
        u_stat += j

    return u_stat / (n_pos * n_neg)


# ---------------------------------------------------------------------------
# Distribution quantiles
# ---------------------------------------------------------------------------


def _describe(scores: Sequence[float]) -> dict[str, float]:
    """Compute distribution statistics for *scores*."""
    n = len(scores)
    if n == 0:
        return {"min": 0.0, "max": 0.0, "mean": 0.0, "median": 0.0, "p25": 0.0, "p75": 0.0}

    sorted_s = sorted(scores)
    return {
        "min": sorted_s[0],
        "max": sorted_s[-1],
        "mean": sum(scores) / n,
        "median": _quantile(sorted_s, 0.5),
        "p25": _quantile(sorted_s, 0.25),
        "p75": _quantile(sorted_s, 0.75),
    }


def _quantile(sorted_data: list[float], q: float) -> float:
    """Linear-interpolation quantile (type R7, same as numpy default)."""
    n = len(sorted_data)
    if n == 0:
        return 0.0
    idx = q * (n - 1)
    lo = int(math.floor(idx))
    hi = min(lo + 1, n - 1)
    frac = idx - lo
    return sorted_data[lo] * (1.0 - frac) + sorted_data[hi] * frac


# ---------------------------------------------------------------------------
# Recommendation heuristic
# ---------------------------------------------------------------------------


def _recommend(
    scores: Sequence[float],
    labels: Sequence[int],
    dist: dict[str, float],
) -> str:
    """Return an advisory recommendation among three strategies.

    Heuristics (all advisory):
    1. **Fixed threshold** (e.g., ``score >= 0.5``) — recommended when
       scores are reasonably well-calibrated (positive rate near 0.5
       at score 0.5 in the calibration curve).
    2. **Percentile threshold** (e.g., top 20%) — recommended when
       scores are monotonic with labels but poorly calibrated.
    3. **Monotonic calibration** (PAV transform) — recommended when
       there are clear monotonicity violations (calibration would help).
    """
    if len(scores) < 10:
        return "fixed-threshold (insufficient data for reliable calibration)"

    curve = calibration_curve(scores, labels, bins=10)

    # Measure miscalibration: average absolute difference between
    # mean_score and positive_rate across bins.
    populated = [b for b in curve if b.count > 0]
    if not populated:
        return "fixed-threshold (no populated bins)"

    miscal = sum(abs(b.mean_score - b.positive_rate) for b in populated) / len(populated)

    # Check for well-calibrated at score 0.5
    mid_bins = [b for b in populated if b.lower <= 0.5 < b.upper]
    well_calibrated_mid = False
    if mid_bins:
        mid = mid_bins[0]
        well_calibrated_mid = abs(mid.mean_score - mid.positive_rate) < 0.15

    # Count monotonicity violations in the calibrator fit
    calibrator = fit_monotonic_calibrator(scores, labels)
    n_pools = len(calibrator.thresholds)
    n_points = len(scores)
    merge_ratio = 1.0 - (n_pools / max(n_points, 1))

    if merge_ratio > 0.3:
        # Significant merging needed -> calibration would meaningfully change scores
        return "monotonic-calibration"

    if well_calibrated_mid:
        return "fixed-threshold"

    # Check score range: if all scores are in a narrow band, percentile
    # threshold is more robust.
    score_range = dist.get("max", 1.0) - dist.get("min", 0.0)
    if score_range < 0.3:
        return "percentile-threshold"

    # Default: if miscalibration is moderate, suggest fixed threshold
    if miscal < 0.2:
        return "fixed-threshold"

    return "monotonic-calibration"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Analyse VLM score calibration.",
    )
    parser.add_argument("scores_json", help="Path to JSON array of VLM scores.")
    parser.add_argument("labels_json", help="Path to JSON array of binary labels.")
    parser.add_argument(
        "--bins", type=int, default=10,
        help="Number of reliability bins (default: 10).",
    )
    args = parser.parse_args()

    with open(args.scores_json, encoding="utf-8") as f:
        scores = json.load(f)
    with open(args.labels_json, encoding="utf-8") as f:
        labels = json.load(f)

    if len(scores) != len(labels):
        print(
            f"ERROR: scores ({len(scores)}) and labels ({len(labels)}) "
            "must have the same length.",
            file=sys.stderr,
        )
        sys.exit(1)

    # --- Distribution ---------------------------------------------------
    dist = _describe(scores)
    print("=== Distribution ===")
    for key, val in dist.items():
        print(f"  {key}: {val:.4f}")
    print()

    # --- Calibration curve ---------------------------------------------
    curve = calibration_curve(scores, labels, bins=args.bins)
    print(f"=== Calibration Curve ({args.bins} bins) ===")
    print(f"  {'bin':>8s}  {'lower':>6s}  {'upper':>6s}  {'mean_score':>11s}  "
          f"{'pos_rate':>9s}  {'count':>5s}")
    for i, b in enumerate(curve):
        if b.count > 0:
            print(f"  {i:>8d}  {b.lower:>6.3f}  {b.upper:>6.3f}  "
                  f"{b.mean_score:>11.4f}  {b.positive_rate:>9.4f}  {b.count:>5d}")
        else:
            print(f"  {i:>8d}  {b.lower:>6.3f}  {b.upper:>6.3f}  "
                  f"{'—':>11s}  {'—':>9s}  {b.count:>5d}")
    print()

    # --- Monotonic calibrator ------------------------------------------
    calibrator = fit_monotonic_calibrator(scores, labels)
    print("=== Monotonic Calibrator (PAV) ===")
    print(f"  pools: {len(calibrator.thresholds)}  "
          f"(from {len(scores)} points)")
    print(f"  thresholds: {', '.join(f'{t:.4f}' for t in calibrator.thresholds)}")
    print(f"  values:     {', '.join(f'{v:.4f}' for v in calibrator.values)}")
    print()

    # --- Ranking AUC ---------------------------------------------------
    auc = ranking_auc(scores, labels)
    print(f"=== Ranking AUC ===")
    print(f"  AUC: {auc:.4f}  ({_auc_rating(auc)})")
    print()

    # --- NDCG for various k --------------------------------------------
    print("=== NDCG ===")
    sorted_indices = sorted(
        range(len(scores)), key=lambda i: scores[i], reverse=True
    )
    # Use binary labels as "relevance" for NDCG
    ranked_labels = [labels[i] for i in sorted_indices]
    for k in [1, 5, 10, 50, 100]:
        if k <= len(ranked_labels):
            ndcg = ndcg_at_k(ranked_labels, k)
            print(f"  NDCG@{k:>3d}: {ndcg:.4f}")
    print()

    # --- Recommendation -------------------------------------------------
    recommendation = _recommend(scores, labels, dist)
    print(f"=== Advisory Recommendation ===")
    print(f"  Strategy: {recommendation}")


def _auc_rating(auc: float) -> str:
    if auc >= 0.95:
        return "excellent"
    if auc >= 0.85:
        return "good"
    if auc >= 0.70:
        return "fair"
    if auc >= 0.60:
        return "poor"
    return "worst-than-random"


if __name__ == "__main__":
    main()
