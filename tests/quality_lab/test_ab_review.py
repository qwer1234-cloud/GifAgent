"""Tests for blind A/B review (Phase 2 Task 5)."""
from __future__ import annotations

import json
import sqlite3
import uuid

import pytest

from app.quality_lab import (
    apply_quality_schema,
)
from app.quality_lab.models import Choice
from app.quality_lab.ab_review import BlindReviewService


# ===================================================================
# Factory helpers
# ===================================================================


def _seed_manifest(db: sqlite3.Connection, items: list[dict]) -> str:
    """Insert a manifest and its items into the quality-lab DB."""
    manifest_id = f"m_{uuid.uuid4().hex[:8]}"
    db.execute(
        "INSERT INTO benchmark_manifests (manifest_id, version, item_count, created_at) "
        "VALUES (?, ?, ?, ?)",
        (manifest_id, 1, len(items), "2026-07-18T00:00:00"),
    )
    for item in items:
        db.execute(
            """INSERT INTO benchmark_items
               (item_id, manifest_id, source_path, video_fingerprint,
                duration_bucket, resolution_bucket, pace_bucket,
                difficulty_tags, split)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                item["item_id"],
                manifest_id,
                item.get("source_path", f"/videos/{item['item_id']}.mp4"),
                item["video_fingerprint"],
                item.get("duration_bucket", "short"),
                item.get("resolution_bucket", "hd"),
                item.get("pace_bucket", "medium"),
                "|".join(item.get("difficulty_tags", ("action",))),
                item.get("split", "tune"),
            ),
        )
    db.commit()
    return manifest_id


def _seed_config(db: sqlite3.Connection) -> str:
    """Insert a minimal experiment config."""
    config_id = f"cfg_{uuid.uuid4().hex[:8]}"
    db.execute(
        "INSERT INTO experiment_configs (config_id, config_json, provenance_json, created_at) "
        "VALUES (?, ?, ?, ?)",
        (config_id, json.dumps({"vlm": {"model": "test"}}), "{}", "2026-07-18T00:00:00"),
    )
    db.commit()
    return config_id


def _seed_run(
    db: sqlite3.Connection, manifest_id: str, config_id: str, *, split: str = "tune",
) -> str:
    """Insert an experiment run and return its run_id."""
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    db.execute(
        "INSERT INTO experiment_runs (run_id, manifest_id, config_id, split, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 'completed', ?, ?)",
        (run_id, manifest_id, config_id, split, "2026-07-18T00:00:00", "2026-07-18T00:00:00"),
    )
    db.commit()
    return run_id


# ===================================================================
# Tests
# ===================================================================


class TestBlindReviewService:
    """``BlindReviewService`` — blind A/B review sessions."""

    # -- fixtures --------------------------------------------------------

    @pytest.fixture
    def db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        apply_quality_schema(conn)
        return conn

    @pytest.fixture
    def service(self, db: sqlite3.Connection) -> BlindReviewService:
        return BlindReviewService(db)

    # -- helpers ---------------------------------------------------------

    def _make_session(
        self,
        db: sqlite3.Connection,
        service: BlindReviewService,
        *,
        n_videos: int = 2,
        clips_per_video: int = 4,
        seed: int = 42,
    ) -> tuple[str, str, str]:
        """Create a manifest, two configs, two runs, and an A/B session.

        Returns ``(session_id, run_a, run_b)``.
        """
        # Use a shared counter so item IDs are unique across test calls
        if not hasattr(self, "_item_seq"):
            self._item_seq = 0
        items: list[dict] = []
        for v in range(n_videos):
            fp = f"fp_video{v:04d}"
            for c in range(clips_per_video):
                self._item_seq += 1
                items.append({
                    "item_id": f"vid{v:04d}_c{c:04d}_s{self._item_seq}",
                    "video_fingerprint": fp,
                })
        mid = _seed_manifest(db, items)
        cid_a = _seed_config(db)
        cid_b = _seed_config(db)
        run_a = _seed_run(db, mid, cid_a)
        run_b = _seed_run(db, mid, cid_b)
        session = service.create_session(run_a=run_a, run_b=run_b, seed=seed)
        return session.session_id, run_a, run_b

    # -- create_session --------------------------------------------------

    def test_session_created_with_active_status(
        self, db: sqlite3.Connection, service: BlindReviewService,
    ) -> None:
        """``create_session`` returns an ``ABSession`` with ``active`` status."""
        items = [
            {"item_id": "clip_001", "video_fingerprint": "fp_video1"},
            {"item_id": "clip_002", "video_fingerprint": "fp_video1"},
        ]
        mid = _seed_manifest(db, items)
        cid_a = _seed_config(db)
        cid_b = _seed_config(db)
        run_a = _seed_run(db, mid, cid_a)
        run_b = _seed_run(db, mid, cid_b)

        session = service.create_session(run_a=run_a, run_b=run_b, seed=7)
        assert session.run_a == run_a
        assert session.run_b == run_b
        assert session.seed == 7
        assert session.status == "active"

    def test_side_assignment_is_seeded_and_balanced(
        self, db: sqlite3.Connection, service: BlindReviewService,
    ) -> None:
        """Same seed produces identical side assignments; assignment is not all-left or all-right."""
        # Create 2 videos x 4 clips = 8 items = 4 pairs
        sid1, _, _ = self._make_session(db, service, n_videos=2, clips_per_video=4, seed=42)
        sid2, _, _ = self._make_session(db, service, n_videos=2, clips_per_video=4, seed=42)
        sid3, _, _ = self._make_session(db, service, n_videos=2, clips_per_video=4, seed=99)

        def _left_run_a_counts(sid: str) -> list[int]:
            rows = db.execute(
                "SELECT left_is_run_a FROM ab_pairs WHERE session_id=? ORDER BY pair_index",
                (sid,),
            ).fetchall()
            return [r["left_is_run_a"] for r in rows]

        sides1 = _left_run_a_counts(sid1)
        sides2 = _left_run_a_counts(sid2)
        sides3 = _left_run_a_counts(sid3)

        # Same seed -> identical assignments
        assert sides1 == sides2

        # Different seed -> different assignments (not guaranteed but extremely likely)
        assert sides1 != sides3, "Expected different side assignment with different seed"

        # Balanced: at least one pair has left=run_a and one has left=run_b
        assert 1 <= sum(sides1) < len(sides1), "Expected at least one each of left=run_a and left=run_b"

    # -- next_pair -------------------------------------------------------

    def test_next_pair_returns_opaque_tokens_not_run_ids(
        self, db: sqlite3.Connection, service: BlindReviewService,
    ) -> None:
        """``next_pair`` returns a ``BlindPair`` with opaque tokens, not run/config IDs."""
        sid, run_a, run_b = self._make_session(db, service, clips_per_video=2)

        pair = service.next_pair(sid)
        assert pair is not None

        # Tokens should not contain run IDs or config IDs
        assert run_a not in pair.left_token
        assert run_a not in pair.right_token
        assert run_b not in pair.left_token
        assert run_b not in pair.right_token

        # Tokens should look like base64 URL-safe strings
        assert "+" not in pair.left_token and "/" not in pair.left_token
        assert "+" not in pair.right_token and "/" not in pair.right_token

    def test_next_pair_returns_first_unjudged_pair(
        self, db: sqlite3.Connection, service: BlindReviewService,
    ) -> None:
        """``next_pair`` returns the first pair without a judgment."""
        sid, _, _ = self._make_session(db, service, n_videos=1, clips_per_video=4)

        pair0 = service.next_pair(sid)
        assert pair0 is not None
        assert pair0.pair_index == 0

        service.record(sid, str(pair0.pair_index), "left")

        pair1 = service.next_pair(sid)
        assert pair1 is not None
        assert pair1.pair_index == 1

    def test_next_pair_returns_none_when_all_judged(
        self, db: sqlite3.Connection, service: BlindReviewService,
    ) -> None:
        """``next_pair`` returns ``None`` when every pair has a judgment."""
        sid, _, _ = self._make_session(db, service, n_videos=1, clips_per_video=2)  # 1 pair

        assert service.next_pair(sid) is not None
        service.record(sid, "0", "tie")
        assert service.next_pair(sid) is None

    # -- pairing strategy ------------------------------------------------

    def test_clips_paired_by_source_video(
        self, db: sqlite3.Connection, service: BlindReviewService,
    ) -> None:
        """Pairs only contain items from the same source video (fingerprint)."""
        items = [
            {"item_id": "clip_001", "video_fingerprint": "fp_video_A"},
            {"item_id": "clip_002", "video_fingerprint": "fp_video_A"},
            {"item_id": "clip_003", "video_fingerprint": "fp_video_B"},
            {"item_id": "clip_004", "video_fingerprint": "fp_video_B"},
        ]
        mid = _seed_manifest(db, items)
        cid_a = _seed_config(db)
        cid_b = _seed_config(db)
        run_a = _seed_run(db, mid, cid_a)
        run_b = _seed_run(db, mid, cid_b)

        session = service.create_session(run_a=run_a, run_b=run_b, seed=1)

        rows = db.execute(
            "SELECT item_a_id, item_b_id FROM ab_pairs WHERE session_id=? ORDER BY pair_index",
            (session.session_id,),
        ).fetchall()
        assert len(rows) == 2

        # Both pairs must contain items from the same video fingerprint
        for r in rows:
            fp_a = next(it["video_fingerprint"] for it in items if it["item_id"] == r["item_a_id"])
            fp_b = next(it["video_fingerprint"] for it in items if it["item_id"] == r["item_b_id"])
            assert fp_a == fp_b, f"Items {r['item_a_id']} and {r['item_b_id']} have different source videos"

    def test_temporal_pairing_within_video(
        self, db: sqlite3.Connection, service: BlindReviewService,
    ) -> None:
        """Adjacent clips (by item_id) from the same video are paired together."""
        items = [
            {"item_id": "clip_001", "video_fingerprint": "fp_video"},
            {"item_id": "clip_002", "video_fingerprint": "fp_video"},
            {"item_id": "clip_003", "video_fingerprint": "fp_video"},
            {"item_id": "clip_004", "video_fingerprint": "fp_video"},
            {"item_id": "clip_005", "video_fingerprint": "fp_video"},
            {"item_id": "clip_006", "video_fingerprint": "fp_video"},
        ]
        mid = _seed_manifest(db, items)
        cid_a = _seed_config(db)
        cid_b = _seed_config(db)
        run_a = _seed_run(db, mid, cid_a)
        run_b = _seed_run(db, mid, cid_b)

        session = service.create_session(run_a=run_a, run_b=run_b, seed=1)

        rows = db.execute(
            "SELECT item_a_id, item_b_id FROM ab_pairs WHERE session_id=? ORDER BY pair_index",
            (session.session_id,),
        ).fetchall()
        # 6 items = 3 pairs: (clip_001, clip_002), (clip_003, clip_004), (clip_005, clip_006)
        assert len(rows) == 3
        assert rows[0]["item_a_id"] == "clip_001"
        assert rows[0]["item_b_id"] == "clip_002"
        assert rows[1]["item_a_id"] == "clip_003"
        assert rows[1]["item_b_id"] == "clip_004"
        assert rows[2]["item_a_id"] == "clip_005"
        assert rows[2]["item_b_id"] == "clip_006"

    def test_clip_appears_at_most_once(
        self, db: sqlite3.Connection, service: BlindReviewService,
    ) -> None:
        """No clip appears in more than one pair."""
        items = [
            {"item_id": f"c_{i:03d}", "video_fingerprint": "fp_video"}
            for i in range(10)
        ]
        mid = _seed_manifest(db, items)
        cid_a = _seed_config(db)
        cid_b = _seed_config(db)
        run_a = _seed_run(db, mid, cid_a)
        run_b = _seed_run(db, mid, cid_b)

        service.create_session(run_a=run_a, run_b=run_b, seed=1)

        used: set[str] = set()
        rows = db.execute(
            "SELECT item_a_id, item_b_id FROM ab_pairs",
        ).fetchall()
        for r in rows:
            assert r["item_a_id"] not in used, f"Duplicate: {r['item_a_id']}"
            assert r["item_b_id"] not in used, f"Duplicate: {r['item_b_id']}"
            used.add(r["item_a_id"])
            used.add(r["item_b_id"])

    # -- record ----------------------------------------------------------

    def test_record_duplicate_rejected(
        self, db: sqlite3.Connection, service: BlindReviewService,
    ) -> None:
        """Recording a judgment for an already-judged pair raises ``ValueError``."""
        sid, _, _ = self._make_session(db, service, n_videos=1, clips_per_video=2)

        service.record(sid, "0", "left")
        with pytest.raises(ValueError, match="already judged|duplicate"):
            service.record(sid, "0", "right")

    def test_record_all_choices_accepted(
        self, db: sqlite3.Connection, service: BlindReviewService,
    ) -> None:
        """All four ``Choice`` values are accepted."""
        sid, _, _ = self._make_session(db, service, n_videos=2, clips_per_video=4)

        choices: list[Choice] = ["left", "right", "tie", "both_bad"]
        for i, ch in enumerate(choices):
            pair = service.next_pair(sid)
            assert pair is not None
            service.record(sid, str(pair.pair_index), ch)

        # Verify all recorded
        rows = db.execute(
            "SELECT choice FROM ab_judgments WHERE session_id=? ORDER BY pair_index",
            (sid,),
        ).fetchall()
        assert [r["choice"] for r in rows] == choices

    # -- reveal ----------------------------------------------------------

    def test_reveal_computes_counts(
        self, db: sqlite3.Connection, service: BlindReviewService,
    ) -> None:
        """``reveal`` returns correct win/tie counts after all pairs judged."""
        sid, run_a, run_b = self._make_session(db, service, n_videos=2, clips_per_video=4)

        # Check pairing assignment for known pairs
        pair_rows = db.execute(
            "SELECT pair_index, left_is_run_a FROM ab_pairs "
            "WHERE session_id=? ORDER BY pair_index",
            (sid,),
        ).fetchall()

        # Judge each pair: always choose "left".
        # When left_is_run_a=1, left = run_a output → run_a wins.
        # When left_is_run_a=0, left = run_b output → run_b wins.
        expected = {"run_a_wins": 0, "run_b_wins": 0, "ties": 0, "both_bad": 0}
        for pr in pair_rows:
            service.record(sid, str(pr["pair_index"]), "left")
            if pr["left_is_run_a"]:
                expected["run_a_wins"] += 1
            else:
                expected["run_b_wins"] += 1

        result = service.reveal(sid)
        assert result.run_a == run_a
        assert result.run_b == run_b
        assert result.run_a_wins == expected["run_a_wins"]
        assert result.run_b_wins == expected["run_b_wins"]
        assert result.ties == 0
        assert result.both_bad == 0

    def test_reveal_requires_all_pairs_judged(
        self, db: sqlite3.Connection, service: BlindReviewService,
    ) -> None:
        """``reveal`` raises ``ValueError`` when not all pairs are judged."""
        sid, _, _ = self._make_session(db, service, n_videos=1, clips_per_video=4)

        # Judge only the first pair
        service.record(sid, "0", "left")

        with pytest.raises(ValueError, match="remain unjudged"):
            service.reveal(sid)

    def test_reveal_includes_config_ids(
        self, db: sqlite3.Connection, service: BlindReviewService,
    ) -> None:
        """``reveal`` returns config IDs for both runs."""
        sid, run_a, run_b = self._make_session(db, service, n_videos=1, clips_per_video=2)
        service.record(sid, "0", "tie")

        result = service.reveal(sid)

        # Get expected config IDs from the runs
        row_a = db.execute(
            "SELECT config_id FROM experiment_runs WHERE run_id=?", (run_a,)
        ).fetchone()
        row_b = db.execute(
            "SELECT config_id FROM experiment_runs WHERE run_id=?", (run_b,)
        ).fetchone()
        assert result.config_a == row_a["config_id"]
        assert result.config_b == row_b["config_id"]

    def test_reveal_includes_tie_and_both_bad(
        self, db: sqlite3.Connection, service: BlindReviewService,
    ) -> None:
        """``reveal`` correctly counts ties and both_bad."""
        sid, _, _ = self._make_session(db, service, n_videos=1, clips_per_video=8)

        # 4 pairs: left, tie, right, both_bad
        service.record(sid, "0", "left")
        service.record(sid, "1", "tie")
        service.record(sid, "2", "right")
        service.record(sid, "3", "both_bad")

        result = service.reveal(sid)
        assert result.run_a_wins + result.run_b_wins + result.ties + result.both_bad == 4
        assert result.ties == 1
        assert result.both_bad == 1
