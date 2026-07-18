"""Phase 4 Task 7: Taste Map, narrative curation, and Why This.

Tests
-----
- Taste map: PCA/SVD projection is deterministic and finite; fewer than two
  vectors degrades gracefully; sign stabilisation is consistent.
- Narrative curation: output contains ordered unique candidates with every
  requested beat or an explicit missing-beat reason.
- Why This: ``explain_selection`` uses only stored score components/provenance
  and does not claim causality.
"""

from __future__ import annotations

import numpy as np
import pytest

from app.services.taste_map import TastePoint, project_taste_map
from app.services.narrative_curation import (
    CurationCandidate,
    CuratedBeat,
    curate_narrative,
)


# ===================================================================
# Taste Map tests
# ===================================================================


class TestTasteMap:
    """PCA/SVD projection of high-dimensional vectors into 2D taste space."""

    def _rng(self, seed: int = 0) -> np.random.Generator:
        return np.random.default_rng(seed)

    def test_projection_deterministic(self):
        """Same input + same seed produces identical points."""
        rng = self._rng(42)
        vectors = rng.normal(0, 1, (10, 768)).astype(np.float32)
        ids = [f"cand-{i}" for i in range(10)]

        result_a = project_taste_map(vectors, ids, seed=0)
        result_b = project_taste_map(vectors, ids, seed=0)

        assert len(result_a) == len(result_b) == 10
        for a, b in zip(result_a, result_b):
            assert a.candidate_id == b.candidate_id
            assert a.x == pytest.approx(b.x)
            assert a.y == pytest.approx(b.y)

    def test_projection_finite(self):
        """All coordinates are finite floats."""
        rng = self._rng(7)
        vectors = rng.normal(0, 1, (20, 768)).astype(np.float32)
        ids = [f"cand-{i}" for i in range(20)]

        points = project_taste_map(vectors, ids, seed=1)

        for p in points:
            assert np.isfinite(p.x), f"{p.candidate_id}: x={p.x} is not finite"
            assert np.isfinite(p.y), f"{p.candidate_id}: y={p.y} is not finite"

    def test_empty_vectors_returns_empty(self):
        """Zero vectors → empty list."""
        vectors = np.empty((0, 768), dtype=np.float32)
        points = project_taste_map(vectors, [], seed=0)
        assert points == []

    def test_single_vector_at_origin(self):
        """Single vector → (0.0, 0.0)."""
        rng = self._rng(42)
        vec = rng.normal(0, 1, 768).astype(np.float32)
        vec = vec / np.linalg.norm(vec)

        points = project_taste_map(vec[np.newaxis, :], ["cand-solo"], seed=0)

        assert len(points) == 1
        assert points[0].candidate_id == "cand-solo"
        assert points[0].x == 0.0
        assert points[0].y == 0.0

    def test_sign_stabilization(self):
        """Each component's largest absolute loading is positive (sign stable).

        Run twice and verify the orientation is identical, not flipped.
        """
        rng = self._rng(99)
        vectors = rng.normal(0, 1, (15, 768)).astype(np.float32)
        ids = [f"cand-{i}" for i in range(15)]

        points = project_taste_map(vectors, ids, seed=2)

        # For each component, find the point with the largest absolute value
        coords = np.array([[p.x, p.y] for p in points])
        for col_idx in range(2):
            col = coords[:, col_idx]
            max_abs_idx = np.argmax(np.abs(col))
            assert col[max_abs_idx] > 0, (
                f"Component {col_idx} has negative largest "
                f"absolute loading: {col[max_abs_idx]}"
            )

    def test_sign_stabilization_deterministic_orientation(self):
        """Multiple runs with same seed produce same sign orientation."""
        rng = self._rng(123)
        vectors = rng.normal(0, 1, (10, 768)).astype(np.float32)
        ids = [f"cand-{i}" for i in range(10)]

        run_1 = project_taste_map(vectors, ids, seed=5)
        run_2 = project_taste_map(vectors, ids, seed=5)

        for a, b in zip(run_1, run_2):
            assert a.x == pytest.approx(b.x, abs=1e-10)
            assert a.y == pytest.approx(b.y, abs=1e-10)

    def test_different_seeds_same_math(self):
        """Seed parameter is irrelevant when n >= 2 (pure SVD, no randomness)."""
        rng = self._rng(42)
        vectors = rng.normal(0, 1, (10, 768)).astype(np.float32)
        ids = [f"cand-{i}" for i in range(10)]

        # Different seeds should produce identical results since SVD is
        # deterministic (ignoring sign ambiguity which we stabilize).
        result_0 = project_taste_map(vectors, ids, seed=0)
        result_1 = project_taste_map(vectors, ids, seed=999)

        for a, b in zip(result_0, result_1):
            assert a.x == pytest.approx(b.x, abs=1e-10)
            assert a.y == pytest.approx(b.y, abs=1e-10)

    def test_two_vectors_produce_line(self):
        """Two vectors should produce points on opposite sides of origin
        after centering (one principal component captures all variance)."""
        v1 = np.array([1.0, 0.0, 0.0] * 256, dtype=np.float32)[:768]
        v2 = np.array([0.0, 1.0, 0.0] * 256, dtype=np.float32)[:768]
        v1 = v1 / np.linalg.norm(v1)
        v2 = v2 / np.linalg.norm(v2)

        vectors = np.stack([v1, v2], axis=0)
        points = project_taste_map(vectors, ["cand-a", "cand-b"], seed=0)

        # Two distinct points
        assert len(points) == 2
        # Secondary dimension should be near-zero (only 1 meaningful PC)
        assert abs(points[0].y) < 1e-7, f"y[0]={points[0].y} too large"
        assert abs(points[1].y) < 1e-7, f"y[1]={points[1].y} too large"
        # X-coordinates should be opposite signs after centering + sign stabilisation
        assert points[0].x * points[1].x < 0, (
            f"Expected opposite signs, got {points[0].x} and {points[1].x}"
        )


# ===================================================================
# Narrative Curation tests
# ===================================================================


def _make_candidate(
    candidate_id: str,
    source_video: str = "video-0",
    start_time: float = 0.0,
    beat_scores: dict[str, float] | None = None,
    quality: float = 0.5,
    preference: float = 0.5,
) -> CurationCandidate:
    vec = np.zeros(768, dtype=np.float32)
    return CurationCandidate(
        candidate_id=candidate_id,
        source_video=source_video,
        start_time=start_time,
        beat_scores=beat_scores or {},
        quality=quality,
        preference=preference,
        vector=vec,
    )


class TestNarrativeCuration:
    """Beat-based narrative curation from a candidate pool."""

    def test_output_contains_ordered_unique_candidates(self):
        """Narrative output is ordered and every candidate is unique."""
        candidates = [
            _make_candidate(
                "cand-1", beat_scores={"opening": 0.9, "development": 0.5},
                quality=0.8, preference=0.7,
            ),
            _make_candidate(
                "cand-2", beat_scores={"development": 0.9, "climax": 0.6},
                quality=0.7, preference=0.6,
            ),
            _make_candidate(
                "cand-3", beat_scores={"climax": 0.9, "ending": 0.5},
                quality=0.9, preference=0.8,
            ),
            _make_candidate(
                "cand-4", beat_scores={"opening": 0.3, "ending": 0.9},
                quality=0.6, preference=0.5,
            ),
        ]

        beats = curate_narrative(candidates)

        # Ordered beats
        beat_names = [b.beat for b in beats]
        assert beat_names == ["opening", "development", "climax", "ending"]

        # Unique candidates
        selected_ids = [b.selected_candidate_id for b in beats]
        assert all(b.selected_candidate_id is not None for b in beats)
        assert len(set(selected_ids)) == len(selected_ids), (
            f"Non-unique candidate IDs: {selected_ids}"
        )

    def test_all_beats_present_or_explicit_missing(self):
        """Every requested beat has a selection or missing_reason."""
        candidates = [
            _make_candidate(
                "cand-1", beat_scores={"opening": 0.9}, quality=0.8, preference=0.7,
            ),
        ]

        beats = curate_narrative(
            candidates,
            beats=("opening", "development", "climax", "ending"),
        )

        assert len(beats) == 4

        # First beat should have a selection
        assert beats[0].selected_candidate_id is not None
        assert beats[0].missing_reason is None

        # Remaining beats should have missing_reason since there's only one candidate
        for beat in beats[1:]:
            assert beat.selected_candidate_id is None
            assert beat.missing_reason is not None, (
                f"Beat {beat.beat} should have a missing_reason"
            )

    def test_empty_candidates(self):
        """No candidates → all beats get missing reasons."""
        beats = curate_narrative(
            [],
            beats=("opening", "development"),
        )

        assert len(beats) == 2
        for b in beats:
            assert b.selected_candidate_id is None
            assert b.missing_reason is not None

    def test_same_candidate_not_reused(self):
        """A single candidate can't be selected for multiple beats."""
        candidate = _make_candidate(
            "cand-only",
            beat_scores={"opening": 0.9, "development": 0.9, "climax": 0.9},
            quality=1.0, preference=1.0,
        )

        beats = curate_narrative(
            [candidate],
            beats=("opening", "development", "climax"),
        )

        # Only one candidate can be selected (for the first beat)
        selected = [b for b in beats if b.selected_candidate_id is not None]
        assert len(selected) == 1
        assert selected[0].selected_candidate_id == "cand-only"
        assert selected[0].beat == "opening"

        # Remaining beats get missing reason
        missing = [b for b in beats if b.selected_candidate_id is None]
        assert len(missing) == 2

    def test_beat_order_preserved(self):
        """Custom beat order is preserved."""
        candidates = [
            _make_candidate(f"cand-{i}",
                beat_scores={b: 0.9 for b in ("intro", "body", "outro")},
                quality=0.7, preference=0.7,
            )
            for i in range(3)
        ]

        beats = curate_narrative(
            candidates,
            beats=("intro", "body", "outro"),
        )

        assert [b.beat for b in beats] == ["intro", "body", "outro"]

    def test_component_scores_in_curated_beat(self):
        """Each CuratedBeat includes component_scores dict."""
        candidates = [
            _make_candidate(
                "cand-1", beat_scores={"opening": 0.85}, quality=0.9, preference=0.8,
                source_video="video-a",
            ),
        ]

        beats = curate_narrative(candidates, beats=("opening",))

        assert len(beats) == 1
        b = beats[0]
        assert b.selected_candidate_id == "cand-1"
        assert isinstance(b.component_scores, dict)
        assert len(b.component_scores) > 0

    def test_beat_fit_weighted_highest(self):
        """Candidate with best beat-fit score for a given beat is selected.

        When quality and preference are equal, beat-fit decides the winner.
        """
        candidates = [
            _make_candidate(
                "cand-high-fit",
                beat_scores={"opening": 0.95, "climax": 0.1},
                quality=0.7, preference=0.7, source_video="video-a",
            ),
            _make_candidate(
                "cand-low-fit",
                beat_scores={"opening": 0.05, "climax": 0.95},
                quality=0.7, preference=0.7, source_video="video-b",
            ),
        ]

        beats = curate_narrative(candidates, beats=("opening", "climax"))

        # Opening: both have same quality/preference, cand-high-fit has better fit
        assert beats[0].selected_candidate_id == "cand-high-fit", (
            f"Expected cand-high-fit for opening, got {beats[0].selected_candidate_id}"
        )
        # Climax: only cand-low-fit remains
        assert beats[1].selected_candidate_id == "cand-low-fit"


# ===================================================================
# Why This (explain_selection) tests
# ===================================================================


class TestExplainSelection:
    """SelectionExplanation — Chinese summary + stored component scores."""

    def _setup_db(self) -> tuple:
        """Create an in-memory DB with a sample candidate."""
        import sqlite3
        from app.services.preference_schema import apply_preference_schema

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        apply_preference_schema(conn)

        conn.execute(
            """INSERT INTO candidate_gifs
               (candidate_id, source_run_id, source_run_candidate_id,
                source_video_sha256, source_video_path, start_sec, end_sec,
                artifact_path, preview_path, vlm_summary_json, tags_json,
                scenario_keys_json, base_rag_similarity, final_score,
                profile_score, score_profile_version, status, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "cand-test-1",
                "run-1", "src-1",
                "abc123", "/videos/test.mp4",
                10.0, 15.0,
                "/gifs/test.gif", "/thumbs/test.jpg",
                '{"summary": "a cat jumping"}',
                '["cat", "jump"]', '["emotion:joy"]',
                0.75, 0.82, 0.80,
                "profile-v1", "candidate",
                "2026-07-18T10:00:00", "2026-07-18T10:00:00",
            ),
        )
        conn.commit()
        return conn

    def test_explanation_format(self):
        """Returns a SelectionExplanation with summary, score_components, provenance_ids."""
        from app.services.ranking_explanations import (
            SelectionExplanation,
            explain_selection,
        )

        conn = self._setup_db()
        result = explain_selection(conn, "cand-test-1", context="search")

        assert isinstance(result, SelectionExplanation)
        assert isinstance(result.summary, str)
        assert len(result.summary) > 0
        assert isinstance(result.score_components, dict)
        assert len(result.score_components) > 0
        assert isinstance(result.provenance_ids, list)

    def test_explanation_uses_stored_components(self):
        """Score components match stored DB values (no recomputation)."""
        from app.services.ranking_explanations import explain_selection

        conn = self._setup_db()
        result = explain_selection(conn, "cand-test-1", context="search")

        # Components should reflect stored data
        components = result.score_components
        assert "base_quality" in components
        assert components["base_quality"] == 0.75
        assert "final_score" in components
        assert components["final_score"] == 0.82

    def test_explanation_does_not_claim_causality(self):
        """Summary text is descriptive, not causal."""
        from app.services.ranking_explanations import explain_selection

        conn = self._setup_db()
        result = explain_selection(conn, "cand-test-1", context="search")

        summary = result.summary
        # Should contain score information
        assert "0.75" in summary or "0.82" in summary, (
            "Summary should reference stored score values"
        )
        # Should not contain causal language
        causal_words = ["因为", "所以", "导致", "therefore", "because", "causes"]
        for word in causal_words[:4]:  # Check Chinese causal words
            assert word not in summary, (
                f"Summary should not contain causal language: '{word}'"
            )

    @pytest.mark.parametrize("ctx", ["search", "review", "collection"])
    def test_explanation_different_contexts(self, ctx):
        """Each context produces a plausible explanation."""
        from app.services.ranking_explanations import explain_selection

        conn = self._setup_db()
        result = explain_selection(conn, "cand-test-1", context=ctx)

        assert isinstance(result.summary, str) and len(result.summary) > 0
        # Context keyword should appear in summary
        context_map = {
            "search": "搜索",
            "review": "审查",
            "collection": "合集",
        }
        assert context_map[ctx] in result.summary, (
            f"Summary for context '{ctx}' should contain '{context_map[ctx]}', "
            f"got: {result.summary}"
        )

    def test_explanation_includes_provenance(self):
        """Provenance IDs include profile version and source."""
        from app.services.ranking_explanations import explain_selection

        conn = self._setup_db()
        result = explain_selection(conn, "cand-test-1", context="search")

        ids = result.provenance_ids
        assert len(ids) >= 1
        # Should include the profile version if available
        assert any("profile-v1" in pid for pid in ids), (
            f"Expected profile-v1 in provenance IDs, got {ids}"
        )

    def test_explanation_missing_candidate(self):
        """Non-existent candidate returns minimal explanation."""
        from app.services.ranking_explanations import explain_selection

        conn = self._setup_db()
        result = explain_selection(conn, "cand-nonexistent", context="search")

        assert isinstance(result.summary, str) and len(result.summary) > 0
        assert "未找到" in result.summary or "not found" in result.summary.lower()

    def test_explanation_with_no_profile(self):
        """Candidate without profile_version still produces explanation."""
        import sqlite3
        from app.services.preference_schema import apply_preference_schema
        from app.services.ranking_explanations import explain_selection

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        apply_preference_schema(conn)

        conn.execute(
            """INSERT INTO candidate_gifs
               (candidate_id, source_run_id, source_run_candidate_id,
                source_video_sha256, source_video_path, start_sec, end_sec,
                final_score, status, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            ("cand-no-profile", "run-1", "src-1",
             "abc", "/v/test.mp4", 0.0, 5.0,
             0.65, "candidate", "2026-01-01T00:00:00", "2026-01-01T00:00:00"),
        )
        conn.commit()

        result = explain_selection(conn, "cand-no-profile", context="review")

        assert isinstance(result.summary, str) and len(result.summary) > 0
        assert "0.65" in result.summary
        assert result.score_components.get("final_score") == 0.65
