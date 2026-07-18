"""Tests for Phase 3 Task 3: Active review queue."""

from __future__ import annotations

import numpy as np
import pytest

from app.services.review_queue import (
    ReviewCandidate,
    ReviewQueueItem,
    ReviewReason,
    build_review_queue,
)


# ── helpers ──────────────────────────────────────────────────────────────────


def _make_candidate(
    candidate_id: str,
    preference_score: float = 0.5,
    calibrated_probability: float = 0.5,
    source_video_sha256: str = "video-sha-default",
    vector: np.ndarray | None = None,
    cluster_key: str = "cluster-default",
) -> ReviewCandidate:
    if vector is None:
        vector = np.zeros(768, dtype=np.float32)
    return ReviewCandidate(
        candidate_id=candidate_id,
        source_video_sha256=source_video_sha256,
        preference_score=preference_score,
        calibrated_probability=calibrated_probability,
        vector=vector,
        cluster_key=cluster_key,
    )


def _make_test_candidates_100() -> list[ReviewCandidate]:
    """Build 110 candidates for a 70/20/10 allocation test.

    Groups:
      - 80 high-preference (fill exploit, scores 1.0 down to 0.605)
      - 20 near-boundary (fill uncertain, calibrated ~0.5)
      - 10 far-from-centroid (fill explore, vectors very far from exploit centroid)
    """
    candidates: list[ReviewCandidate] = []

    # 80 exploit-type candidates: high preference_score, high calibrated_probability
    for i in range(80):
        candidates.append(_make_candidate(
            candidate_id=f"high-{i:04d}",
            preference_score=round(1.0 - i * 0.005, 4),  # 1.0, 0.995, ..., 0.605
            calibrated_probability=0.95,  # far from 0.5 — not uncertain
            source_video_sha256=f"video-high-{i % 10}",
            cluster_key="cluster-high",
            vector=np.random.RandomState(i).randn(768).astype(np.float32),
        ))

    # 20 uncertain-type candidates: near decision boundary
    for i in range(20):
        candidates.append(_make_candidate(
            candidate_id=f"mid-{i:04d}",
            preference_score=0.3,  # low enough to fall outside exploit top 70
            calibrated_probability=round(0.5 + (i - 9.5) * 0.01, 4),
            source_video_sha256=f"video-mid-{i}",
            cluster_key="cluster-mid",
            vector=np.random.RandomState(200 + i).randn(768).astype(np.float32),
        ))

    # 10 explore-type candidates: far from exploit centroid
    for i in range(10):
        candidates.append(_make_candidate(
            candidate_id=f"low-{i:04d}",
            preference_score=0.1,
            calibrated_probability=0.05,  # far from 0.5
            source_video_sha256=f"video-low-{i}",
            cluster_key=f"cluster-low-{i}",  # each in unique cluster
            vector=np.full(768, 10.0 + i, dtype=np.float32),
        ))

    return candidates


# ── basic edge cases ─────────────────────────────────────────────────────────


class TestEdgeCases:
    def test_empty_candidates_returns_empty(self):
        queue = build_review_queue([], limit=10, seed=42)
        assert queue == []

    def test_limit_zero_returns_empty(self):
        c = _make_candidate("c1")
        queue = build_review_queue([c], limit=0, seed=42)
        assert queue == []

    def test_limit_greater_than_pool_returns_all(self):
        candidates = [_make_candidate(f"c{i}") for i in range(5)]
        queue = build_review_queue(candidates, limit=100, seed=42)
        assert len(queue) == 5


# ── allocation ratios ────────────────────────────────────────────────────────


class TestAllocationRatios:
    def test_allocates_70_20_10(self):
        """100 output slots from 110 candidates: 70 exploit, 20 uncertain, 10 explore."""
        candidates = _make_test_candidates_100()
        queue = build_review_queue(candidates, limit=100, seed=42)

        assert len(queue) == 100
        reasons = [q.reason for q in queue]
        assert reasons.count("exploit") == 70
        assert reasons.count("uncertain") == 20
        assert reasons.count("explore") == 10

    def test_exploit_has_highest_scores(self):
        """All exploit items should have preference_score >= boundary threshold."""
        candidates = _make_test_candidates_100()
        queue = build_review_queue(candidates, limit=100, seed=42)

        exploit = [q for q in queue if q.reason == "exploit"]
        # The 70th-highest score in the high group is 1.0 - 69*0.005 = 0.655
        for item in exploit:
            assert item.score >= 0.65, (
                f"exploit {item.candidate_id} has score {item.score} < 0.65"
            )

    def test_uncertain_near_decision_boundary(self):
        """Uncertain picks should have calibrated_probability near 0.5."""
        candidates = _make_test_candidates_100()
        queue = build_review_queue(candidates, limit=100, seed=42)

        uncertain_ids = {q.candidate_id for q in queue if q.reason == "uncertain"}
        assert len(uncertain_ids) == 20

        for c in candidates:
            if c.candidate_id in uncertain_ids:
                dist = abs(c.calibrated_probability - 0.5)
                assert dist < 0.15, (
                    f"uncertain {c.candidate_id} has calibrated_probability "
                    f"{c.calibrated_probability} (distance {dist} from 0.5)"
                )

    def test_explore_far_from_exploit_centroid(self):
        """Explore picks should be farthest from the exploit centroid."""
        candidates = _make_test_candidates_100()
        queue = build_review_queue(candidates, limit=100, seed=42)

        explore_ids = {q.candidate_id for q in queue if q.reason == "explore"}
        assert len(explore_ids) == 10

        # All explore candidates should come from the "low-" group (far vectors)
        for cid in explore_ids:
            assert cid.startswith("low-"), f"Explore candidate {cid} not from low group"

    def test_explore_prefers_diverse_source_videos(self):
        """Explore selections should avoid duplicate source videos."""
        candidates = _make_test_candidates_100()
        queue = build_review_queue(candidates, limit=100, seed=42)

        explore = [q for q in queue if q.reason == "explore"]
        videos = set()
        for item in explore:
            c = next(c for c in candidates if c.candidate_id == item.candidate_id)
            videos.add(c.source_video_sha256)

        # The 10 low-* candidates each have a unique source_video_sha256
        assert len(videos) == 10, f"Expected 10 unique videos, got {len(videos)}"


# ── determinism ──────────────────────────────────────────────────────────────


class TestDeterminism:
    def test_same_seed_identical_results(self):
        candidates = _make_test_candidates_100()
        q1 = build_review_queue(candidates, limit=100, seed=42)
        q2 = build_review_queue(candidates, limit=100, seed=42)
        assert q1 == q2

    def test_no_duplicate_candidate_ids(self):
        candidates = _make_test_candidates_100()
        queue = build_review_queue(candidates, limit=100, seed=42)
        ids = [q.candidate_id for q in queue]
        assert len(ids) == len(set(ids)), "Duplicate candidate IDs found"


# ── custom ratios ────────────────────────────────────────────────────────────


class TestCustomRatios:
    def test_zero_uncertain_ratio(self):
        """Setting uncertain_ratio=0 should yield no uncertain items."""
        candidates = _make_test_candidates_100()
        queue = build_review_queue(
            candidates, limit=100,
            exploit_ratio=0.50, uncertain_ratio=0.0, explore_ratio=0.50,
            seed=42,
        )
        assert len(queue) == 100
        reasons = [q.reason for q in queue]
        assert reasons.count("uncertain") == 0
        assert reasons.count("exploit") == 50
        assert reasons.count("explore") == 50

    def test_custom_ratios_respected(self):
        """Explicit ratios are applied instead of defaults."""
        candidates = _make_test_candidates_100()
        queue = build_review_queue(
            candidates, limit=100,
            exploit_ratio=0.80, uncertain_ratio=0.10, explore_ratio=0.10,
            seed=42,
        )
        assert len(queue) == 100
        reasons = [q.reason for q in queue]
        assert reasons.count("exploit") == 80
        assert reasons.count("uncertain") == 10
        assert reasons.count("explore") == 10


# ── reason_detail strings ────────────────────────────────────────────────────


class TestReasonDetails:
    def test_reason_detail_exploit(self):
        """Exploit items have Chinese reason_detail."""
        # Need at least 2 candidates so exploit quota = int(2*0.70) = 1
        candidates = [
            _make_candidate("c1", preference_score=0.9, calibrated_probability=0.95),
            _make_candidate("c2", preference_score=0.3, calibrated_probability=0.4),
        ]
        queue = build_review_queue(candidates, limit=2, seed=42)
        exploit = [q for q in queue if q.reason == "exploit"]
        assert len(exploit) >= 1
        assert exploit[0].reason_detail == "偏好得分较高"

    def test_reason_detail_uncertain(self):
        """Uncertain items have Chinese reason_detail."""
        # Need at least 5 candidates so uncertain quota = int(5*0.20) = 1
        candidates = [
            _make_candidate("c1", preference_score=0.9, calibrated_probability=0.95),
            _make_candidate("c2", preference_score=0.8, calibrated_probability=0.90),
            _make_candidate("c3", preference_score=0.7, calibrated_probability=0.85),
            _make_candidate("c4", preference_score=0.6, calibrated_probability=0.80),
            _make_candidate("c5", preference_score=0.5, calibrated_probability=0.52),
        ]
        queue = build_review_queue(candidates, limit=5, seed=42)
        uncertain = [q for q in queue if q.reason == "uncertain"]
        assert len(uncertain) >= 1
        assert uncertain[0].reason_detail == "模型判断最不确定"

    def test_reason_detail_explore(self):
        """Explore items have Chinese reason_detail."""
        candidates = [
            _make_candidate("c1", preference_score=0.9, calibrated_probability=0.95),
            _make_candidate("c2", preference_score=0.3, calibrated_probability=0.4,
                            cluster_key="cluster-x",
                            vector=np.full(768, 10.0, dtype=np.float32)),
            _make_candidate("c3", preference_score=0.2, calibrated_probability=0.3),
        ]
        queue = build_review_queue(candidates, limit=3, seed=42)
        explore = [q for q in queue if q.reason == "explore"]
        assert len(explore) >= 1
        assert explore[0].reason_detail == "探索较少出现的风格"


# ── dataclass contracts ──────────────────────────────────────────────────────


class TestDataclassContracts:
    def test_review_queue_item_is_frozen(self):
        item = ReviewQueueItem(
            candidate_id="c1", reason="exploit", reason_detail="test", score=0.8,
        )
        with pytest.raises(AttributeError):
            item.score = 0.9  # type: ignore[misc]

    def test_review_candidate_is_frozen(self):
        c = ReviewCandidate(
            candidate_id="c1", source_video_sha256="v1",
            preference_score=0.8, calibrated_probability=0.9,
            vector=np.zeros(768), cluster_key="c",
        )
        with pytest.raises(AttributeError):
            c.preference_score = 0.5  # type: ignore[misc]

    def test_review_reason_literal(self):
        r: ReviewReason = "exploit"
        assert r == "exploit"
        r = "uncertain"
        assert r == "uncertain"
        r = "explore"
        assert r == "explore"
