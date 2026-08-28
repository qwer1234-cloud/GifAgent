"""Tests for W1 ``make_shards.py``: stratified text-only export and T0 probe.

Covers: probe is exactly 60 rows with all three strata; deficit fill from
adjacent layers with stderr notice; explicit shortfall declaration below 60;
shard sizes bounded to 50-200; missing snapshot exits 2; existing input shards
are never overwritten without ``--force``; no media/path fields are exported.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app.services.workbench_schema import apply_search_schema
from scripts.token_sprint.make_shards import (
    EXPLICIT_MARKERS,
    classify_strata,
    main,
    text_has_explicit_marker,
)

MINIMAL_CANDIDATE_GIFS_DDL = """
CREATE TABLE IF NOT EXISTS candidate_gifs (
    candidate_id TEXT PRIMARY KEY,
    vlm_summary_json TEXT NOT NULL DEFAULT '{}',
    tags_json TEXT NOT NULL DEFAULT '[]',
    scenario_keys_json TEXT NOT NULL DEFAULT '[]',
    source_video_path TEXT,
    artifact_path TEXT,
    preview_path TEXT,
    final_score REAL,
    status TEXT NOT NULL DEFAULT 'candidate',
    created_at TEXT NOT NULL
);
"""

EXPORT_KEYS = {
    "candidate_id",
    "vlm_summary_json",
    "tags_json",
    "scenario_keys_json",
    "final_score",
    "stratum",
}


def _build_snapshot(path: Path, rows: list[dict]) -> Path:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(MINIMAL_CANDIDATE_GIFS_DDL)
        for i, row in enumerate(rows):
            conn.execute(
                """INSERT INTO candidate_gifs
                   (candidate_id, vlm_summary_json, tags_json, scenario_keys_json,
                    source_video_path, artifact_path, preview_path, final_score, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    row["candidate_id"],
                    row.get("vlm_summary_json", json.dumps({"caption": "clip"})),
                    row.get("tags_json", json.dumps([])),
                    row.get("scenario_keys_json", json.dumps([])),
                    "/videos/clip.mp4",
                    "data/exports/full.gif",
                    "data/thumbs/preview.jpg",
                    row.get("final_score"),
                    f"2026-07-{i + 1:02d}T00:00:00+00:00",
                ),
            )
        apply_search_schema(conn)
        conn.commit()
    finally:
        conn.close()
    return path


def _seed_mix(n_explicit: int, n_mid: int, n_low: int) -> list[dict]:
    rows: list[dict] = []
    for i in range(n_explicit):
        # Explicit rows drive the 'high' layer regardless of score, so give
        # them middling scores; the score tertile then splits the rest.
        rows.append(
            {
                "candidate_id": f"expl-{i}",
                "vlm_summary_json": json.dumps({"caption": f"explicit nude scene {i}"}),
                "final_score": 0.2,
            }
        )
    for i in range(n_mid):
        rows.append(
            {
                "candidate_id": f"mid-{i}",
                "vlm_summary_json": json.dumps({"caption": f"dance performance {i}"}),
                "final_score": 0.85,
            }
        )
    for i in range(n_low):
        rows.append(
            {
                "candidate_id": f"low-{i}",
                "vlm_summary_json": json.dumps({"caption": f"cooking tutorial {i}"}),
                "final_score": 0.1,
            }
        )
    return rows


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


# ── unit: explicit marker matching ───────────────────────────────────────────


class TestExplicitMarkers:
    def test_marker_constant_has_common_adult_words(self):
        for marker in ("nude", "sex", "fuck", "porn", "nsfw"):
            assert marker in EXPLICIT_MARKERS

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("a NUDE scene", True),
            ("sexy pose", True),
            ("fucking fast motion", True),
            ("documentary about bees", False),
            ("assistant works", False),
            ("", False),
            (None, False),
        ],
    )
    def test_text_has_explicit_marker(self, text, expected):
        assert text_has_explicit_marker(text) is expected


# ── stratification ───────────────────────────────────────────────────────────


class TestClassifyStrata:
    def test_explicit_dominates_and_tertile_splits_rest(self):
        rows = _seed_mix(3, 3, 3)
        counts = classify_strata(rows)
        assert counts == {"high": 3, "mid": 3, "low": 3}
        strata = {row["candidate_id"]: row["stratum"] for row in rows}
        assert strata["expl-0"] == "high"
        assert strata["mid-0"] == "mid"
        assert strata["low-0"] == "low"

    def test_null_score_goes_to_low(self):
        rows = [{"candidate_id": "a", "final_score": None, "vlm_summary_json": "{}"}]
        counts = classify_strata(rows)
        assert counts["low"] == 1


# ── CLI-level behaviour ──────────────────────────────────────────────────────


class TestMakeShardsCli:
    def _sprint_root_with_snapshot(self, tmp_path: Path, rows: list[dict]) -> Path:
        sprint_root = tmp_path / "sprint"
        sprint_root.mkdir()
        _build_snapshot(sprint_root / "library_snapshot.db", rows)
        return sprint_root

    def test_missing_snapshot_exit_2(self, tmp_path, capsys):
        sprint_root = tmp_path / "sprint"
        sprint_root.mkdir()

        rc = main(["--sprint-root", str(sprint_root)])

        assert rc == 2
        assert "library_snapshot.db" in capsys.readouterr().err

    def test_probe_exactly_60_rows_with_all_three_strata(self, tmp_path):
        sprint_root = self._sprint_root_with_snapshot(tmp_path, _seed_mix(30, 30, 30))

        rc = main(["--sprint-root", str(sprint_root), "--probe"])

        assert rc == 0
        probe = sprint_root / "shards" / "shard-probe.input.jsonl"
        rows = _read_jsonl(probe)
        assert len(rows) == 60
        per_stratum = {s: sum(1 for r in rows if r["stratum"] == s) for s in ("high", "mid", "low")}
        assert per_stratum == {"high": 20, "mid": 20, "low": 20}

    def test_probe_rows_are_text_only_with_locked_fields(self, tmp_path):
        sprint_root = self._sprint_root_with_snapshot(tmp_path, _seed_mix(30, 30, 30))

        assert main(["--sprint-root", str(sprint_root), "--probe"]) == 0

        rows = _read_jsonl(sprint_root / "shards" / "shard-probe.input.jsonl")
        assert rows
        for row in rows:
            assert set(row) == EXPORT_KEYS
            assert "artifact_path" not in row
            assert "preview_path" not in row
            assert "source_video_path" not in row
            assert "/videos/" not in json.dumps(row)
            assert row["stratum"] in ("high", "mid", "low")

    def test_probe_fills_deficit_from_adjacent_layers_with_notice(self, tmp_path, capsys):
        sprint_root = self._sprint_root_with_snapshot(tmp_path, _seed_mix(5, 60, 60))

        rc = main(["--sprint-root", str(sprint_root), "--probe"])

        assert rc == 0
        err = capsys.readouterr().err
        assert "note:" in err
        rows = _read_jsonl(sprint_root / "shards" / "shard-probe.input.jsonl")
        assert len(rows) == 60
        # The first 20 lines form the 'high' section: its 5 genuinely explicit
        # rows plus 15 borrowed from adjacent layers (which keep their true
        # stratum). Mid and low sections each hold 20 of their own rows.
        assert sum(1 for r in rows if r["stratum"] == "high") == 5
        assert sum(1 for r in rows[20:] if r["stratum"] == "mid") == 20
        assert sum(1 for r in rows[20:] if r["stratum"] == "low") == 20

    def test_probe_declares_shortfall_below_60(self, tmp_path, capsys):
        sprint_root = self._sprint_root_with_snapshot(tmp_path, _seed_mix(4, 3, 3))

        rc = main(["--sprint-root", str(sprint_root), "--probe"])

        assert rc == 0
        err = capsys.readouterr().err
        assert "< 60" in err
        rows = _read_jsonl(sprint_root / "shards" / "shard-probe.input.jsonl")
        assert len(rows) == 10

    def test_regular_shards_are_50_to_200_rows(self, tmp_path):
        sprint_root = self._sprint_root_with_snapshot(tmp_path, _seed_mix(50, 50, 50))

        rc = main(["--sprint-root", str(sprint_root), "--size", "50"])

        assert rc == 0
        shard_paths = sorted((sprint_root / "shards").glob("shard-*.input.jsonl"))
        assert shard_paths
        for shard in shard_paths:
            rows = _read_jsonl(shard)
            assert 50 <= len(rows) <= 200

    def test_regular_round_robin_mixes_strata(self, tmp_path):
        sprint_root = self._sprint_root_with_snapshot(tmp_path, _seed_mix(25, 25, 25))

        assert main(["--sprint-root", str(sprint_root), "--size", "50"]) == 0

        first = _read_jsonl(sprint_root / "shards" / "shard-001.input.jsonl")
        assert {row["stratum"] for row in first} == {"high", "mid", "low"}

    def test_refuses_existing_input_shard_without_force(self, tmp_path, capsys):
        sprint_root = self._sprint_root_with_snapshot(tmp_path, _seed_mix(50, 50, 50))
        assert main(["--sprint-root", str(sprint_root), "--size", "50"]) == 0
        existing = {
            p.name: p.read_text(encoding="utf-8")
            for p in (sprint_root / "shards").glob("*.jsonl")
        }

        capsys.readouterr()
        rc = main(["--sprint-root", str(sprint_root), "--size", "50"])
        err = capsys.readouterr().err

        assert rc == 2
        assert "--force" in err
        after = {
            p.name: p.read_text(encoding="utf-8")
            for p in (sprint_root / "shards").glob("*.jsonl")
        }
        assert after == existing

        capsys.readouterr()
        assert main(["--sprint-root", str(sprint_root), "--size", "50", "--force"]) == 0

    def test_probe_refuses_existing_probe_without_force(self, tmp_path):
        sprint_root = self._sprint_root_with_snapshot(tmp_path, _seed_mix(20, 20, 20))
        assert main(["--sprint-root", str(sprint_root), "--probe"]) == 0

        rc = main(["--sprint-root", str(sprint_root), "--probe"])

        assert rc == 2

    def test_size_out_of_bounds_exit_2(self, tmp_path, capsys):
        sprint_root = self._sprint_root_with_snapshot(tmp_path, _seed_mix(50, 50, 50))

        rc = main(["--sprint-root", str(sprint_root), "--size", "10"])

        assert rc == 2
        assert "size" in capsys.readouterr().err.lower()

    def test_missing_sprint_root_exit_2(self):
        with pytest.raises(SystemExit) as excinfo:
            main(["--sprint-root", "Z:/definitely/not/here"])
        assert excinfo.value.code == 2
