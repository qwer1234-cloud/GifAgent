"""Regression tests for the W1 T0 rerun pool filter in ``make_shards.py``.

The enriched v2 snapshot rerun requires the probe pool to contain only rows
with a non-empty ``caption`` in ``vlm_summary_json`` AND a still-existing GIF
artifact (``Path.is_file()`` boolean on ``artifact_path`` -- media bytes are
never opened). ``--pool-filter caption+gif`` is opt-in: the default behaviour
(export every snapshot row) must stay unchanged for other sprint roots.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from scripts.token_sprint.make_shards import (
    POOL_FILTER_CAPTION_GIF,
    ShardExportError,
    caption_from_summary,
    classify_strata,
    filter_pool,
    load_rows,
    main,
    row_content_text,
)
from scripts.token_sprint.validate_shard import validate_shard

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
                    row.get("vlm_summary_json", "{}"),
                    row.get("tags_json", "[]"),
                    row.get("scenario_keys_json", "[]"),
                    "/videos/clip.mp4",
                    row.get("artifact_path"),
                    "data/thumbs/preview.jpg",
                    row.get("final_score"),
                    f"2026-08-{i + 1:02d}T00:00:00+00:00",
                ),
            )
        conn.commit()
    finally:
        conn.close()
    return path


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


@pytest.fixture()
def gif_dir(tmp_path: Path) -> Path:
    """Empty placeholder .gif files: zero bytes, never opened by any test."""
    gifs = tmp_path / "gifs"
    gifs.mkdir()
    (gifs / "present_a.gif").touch()
    (gifs / "present_b.gif").touch()
    (gifs / "present_explicit.gif").touch()
    return gifs


def _seed_rows(gif_dir: Path) -> list[dict]:
    return [
        {
            "candidate_id": "ok-plain",
            "vlm_summary_json": json.dumps({"caption": "a woman dances in a kitchen"}),
            "artifact_path": str(gif_dir / "present_a.gif"),
        },
        {
            "candidate_id": "ok-explicit",
            "vlm_summary_json": json.dumps({"caption": "nude woman poses on a bed"}),
            "artifact_path": str(gif_dir / "present_explicit.gif"),
        },
        {
            "candidate_id": "ok-second-plain",
            "vlm_summary_json": json.dumps({"caption": "a man jogs along a beach"}),
            "artifact_path": str(gif_dir / "present_b.gif"),
        },
        {
            "candidate_id": "caption-empty",
            "vlm_summary_json": "{}",
            "artifact_path": str(gif_dir / "present_a.gif"),
        },
        {
            "candidate_id": "caption-malformed",
            "vlm_summary_json": "not-json-at-all",
            "artifact_path": str(gif_dir / "present_a.gif"),
        },
        {
            "candidate_id": "gif-missing",
            "vlm_summary_json": json.dumps({"caption": "a cat naps on a windowsill"}),
            "artifact_path": str(gif_dir / "absent.gif"),
        },
        {
            "candidate_id": "path-null",
            "vlm_summary_json": json.dumps({"caption": "a dog swims in a lake"}),
            "artifact_path": None,
        },
    ]


# ── unit: caption extraction ─────────────────────────────────────────────────


class TestCaptionFromSummary:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (json.dumps({"caption": "hello world"}), "hello world"),
            ('"  spaced caption  "', ""),  # valid JSON but not an object
            ("  spaced caption  ", ""),  # malformed JSON: fail closed
            ("{}", ""),
            (json.dumps({"caption": ""}), ""),
            (json.dumps({"caption": None}), ""),
            (json.dumps({"caption": 42}), ""),
            ("not json", ""),
            (None, ""),
            ("", ""),
            (json.dumps([1, 2, 3]), ""),
        ],
    )
    def test_caption_extraction_fails_closed(self, raw, expected):
        assert caption_from_summary(raw) == expected


# ── unit: pool filter ────────────────────────────────────────────────────────


class TestFilterPool:
    def test_filter_keeps_caption_and_gif_rows_only(self, gif_dir):
        rows = _seed_rows(gif_dir)

        kept, stats = filter_pool(rows, require_caption_gif=True)

        assert [row["candidate_id"] for row in kept] == [
            "ok-plain",
            "ok-explicit",
            "ok-second-plain",
        ]
        assert stats == {
            "rows_total": 7,
            "caption_empty": 2,  # caption-empty + caption-malformed
            "gif_missing": 2,  # gif-missing + path-null (caption pass first)
            "pool": 3,
        }

    def test_filter_off_returns_rows_unchanged(self, gif_dir):
        rows = _seed_rows(gif_dir)

        kept, stats = filter_pool(rows, require_caption_gif=False)

        assert kept is rows
        assert stats == {}

    def test_is_file_boolean_never_reads_bytes(self, gif_dir):
        # The GIF fixture files are zero bytes; existence is a stat() only.
        assert (gif_dir / "present_a.gif").stat().st_size == 0
        rows = [
            {
                "candidate_id": "x",
                "vlm_summary_json": json.dumps({"caption": "dance"}),
                "artifact_path": str(gif_dir / "present_a.gif"),
            }
        ]
        kept, _ = filter_pool(rows, require_caption_gif=True)
        assert len(kept) == 1


# ── regression: content-text explicit matching (v2 sex_act artifact) ────────


class TestContentTextMatching:
    """Marker matching must read JSON string values, never field names.

    The enriched v2 snapshot writes a numeric ``"sex_act"`` metadata field on
    every row; matching over the raw serialized JSON made the ``\\bsex``
    word-start marker classify 12,827/12,827 pool rows as ``high``.
    """

    def test_numeric_field_name_is_not_content(self):
        row = {
            "vlm_summary_json": json.dumps(
                {"caption": "a woman walks down a hallway", "sex_act": 45, "gif_worthiness": 68}
            ),
            "tags_json": json.dumps(["woman", "hallway", "walking"]),
            "scenario_keys_json": json.dumps(["emotion:dread", "scene:hallway_walk"]),
        }
        assert "sex_act" in row["vlm_summary_json"]  # raw serialized text HAS the field name
        assert "sex_act" not in row_content_text(row)  # content text does not
        assert "gif_worthiness" not in row_content_text(row)

    def test_row_content_text_excludes_field_names(self):
        row = {
            "vlm_summary_json": json.dumps(
                {"caption": "a woman walks down a hallway", "sex_act": 45}
            ),
            "tags_json": json.dumps(["woman", "hallway"]),
            "scenario_keys_json": json.dumps(["scene:hallway_walk"]),
        }
        text = row_content_text(row)
        assert "a woman walks down a hallway" in text
        assert "sex_act" not in text
        assert "45" not in text

    def test_clean_drama_row_classifies_low_despite_sex_act_field(self, gif_dir):
        rows = [
            {
                "candidate_id": "drama",
                "vlm_summary_json": json.dumps(
                    {"caption": "a man walks through a doorway", "sex_act": 12}
                ),
                "artifact_path": str(gif_dir / "present_a.gif"),
            }
        ]
        counts = classify_strata(rows)
        assert counts == {"high": 0, "mid": 0, "low": 1}

    def test_explicit_content_values_still_classify_high(self):
        rows = [
            {
                "candidate_id": "explicit",
                "vlm_summary_json": json.dumps(
                    {"caption": "nude woman poses", "sex_act": 90}
                ),
            },
            {
                "candidate_id": "explicit-tag",
                "vlm_summary_json": json.dumps({"caption": "a scene"}),
                "tags_json": json.dumps(["lingerie", "bed"]),
            },
        ]
        counts = classify_strata(rows)
        assert counts["high"] == 2

    def test_malformed_json_falls_back_to_raw_text_conservatively(self):
        row = {"vlm_summary_json": "broken json with nude word", "tags_json": "[]", "scenario_keys_json": "[]"}
        assert "nude" in row_content_text(row)

    def test_string_values_with_markers_still_match(self):
        row = {
            "vlm_summary_json": json.dumps(
                {"caption": "lingerie scene", "emotional_core": "desire"}
            ),
            "tags_json": "[]",
            "scenario_keys_json": "[]",
        }
        text = row_content_text(row)
        assert "lingerie" in text


# ── regression: validate_shard denominator uses the same content text ───────


class TestValidateDenominatorContentBased:
    def _write_jsonl(self, path: Path, rows: list[dict]) -> Path:
        path.write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
            encoding="utf-8",
        )
        return path

    def test_clean_row_with_sex_act_field_not_flagged_as_sanitized(self, tmp_path):
        sprint_root = tmp_path / "sprint"
        sprint_root.mkdir()
        input_shard = self._write_jsonl(
            tmp_path / "input.jsonl",
            [
                {
                    "candidate_id": "drama-1",
                    "vlm_summary_json": json.dumps(
                        {"caption": "a man eats at a kitchen table", "sex_act": 3}
                    ),
                    "tags_json": "[]",
                    "scenario_keys_json": "[]",
                    "final_score": None,
                    "stratum": "low",
                }
            ],
        )
        shard = self._write_jsonl(
            tmp_path / "out.jsonl",
            [{"candidate_id": "drama-1", "keywords": ["man", "kitchen", "eating"]}],
        )

        qc = validate_shard(
            shard=shard,
            input_shard=input_shard,
            pass_id="p3",
            prompt_version="v1",
            sprint_root=sprint_root,
        )

        assert qc["suspected_sanitized"] == []
        assert qc["fidelity_rate_high_stratum"] is None  # empty denominator

    def test_explicit_input_row_without_marker_output_is_flagged(self, tmp_path):
        sprint_root = tmp_path / "sprint"
        sprint_root.mkdir()
        input_shard = self._write_jsonl(
            tmp_path / "input.jsonl",
            [
                {
                    "candidate_id": "expl-1",
                    "vlm_summary_json": json.dumps(
                        {"caption": "nude woman on a bed", "sex_act": 90}
                    ),
                    "tags_json": "[]",
                    "scenario_keys_json": "[]",
                    "final_score": None,
                    "stratum": "high",
                }
            ],
        )
        shard = self._write_jsonl(
            tmp_path / "out.jsonl",
            [{"candidate_id": "expl-1", "keywords": ["woman", "bed", "pose"]}],
        )

        qc = validate_shard(
            shard=shard,
            input_shard=input_shard,
            pass_id="p3",
            prompt_version="v1",
            sprint_root=sprint_root,
        )

        assert qc["suspected_sanitized"] == ["expl-1"]
        assert qc["fidelity_rate_high_stratum"] == 0.0


# ── CLI-level behaviour ──────────────────────────────────────────────────────


class TestPoolFilterCli:
    def _sprint_root_with_snapshot(self, tmp_path: Path, rows: list[dict]) -> Path:
        sprint_root = tmp_path / "sprint"
        sprint_root.mkdir()
        _build_snapshot(sprint_root / "library_snapshot.db", rows)
        return sprint_root

    def test_probe_with_filter_exports_pool_rows_only(self, tmp_path, gif_dir, capsys):
        sprint_root = self._sprint_root_with_snapshot(tmp_path, _seed_rows(gif_dir))

        rc = main(
            [
                "--sprint-root",
                str(sprint_root),
                "--probe",
                "--pool-filter",
                POOL_FILTER_CAPTION_GIF,
            ]
        )

        assert rc == 0
        probe = sprint_root / "shards" / "shard-probe.input.jsonl"
        rows = _read_jsonl(probe)
        # Probe layout: picked['high'] bucket first (explicit row plus the
        # adjacent-layer fill), then mid (empty), then low.
        assert [row["candidate_id"] for row in rows] == [
            "ok-explicit",
            "ok-plain",
            "ok-second-plain",
        ]
        # Text-only locked fields; the artifact path never leaks into output.
        for row in rows:
            assert set(row) == EXPORT_KEYS
            assert "artifact_path" not in row
            assert str(gif_dir) not in json.dumps(row)
        # Strata on the pool: one explicit caption -> high; no scores -> mid
        # deficit filled from adjacent layers.
        strata = {row["candidate_id"]: row["stratum"] for row in rows}
        assert strata["ok-explicit"] == "high"
        captured = capsys.readouterr()
        assert "layer 'mid' short" in captured.err
        assert "pool_filter: rows_total=7 caption_empty=2 gif_missing=2 pool=3" in captured.out

    def test_default_without_filter_unchanged(self, tmp_path, gif_dir):
        sprint_root = self._sprint_root_with_snapshot(tmp_path, _seed_rows(gif_dir))

        rc = main(["--sprint-root", str(sprint_root), "--probe"])

        assert rc == 0
        rows = _read_jsonl(sprint_root / "shards" / "shard-probe.input.jsonl")
        # All 7 snapshot rows are exportable without the filter (unchanged
        # pre-existing behaviour for other sprint roots).
        assert len(rows) == 7

    def test_invalid_pool_filter_value_rejected(self, tmp_path, gif_dir, capsys):
        sprint_root = self._sprint_root_with_snapshot(tmp_path, _seed_rows(gif_dir))

        # argparse rejects the unknown value before main() runs.
        with pytest.raises(SystemExit) as excinfo:
            main(["--sprint-root", str(sprint_root), "--probe", "--pool-filter", "gif-only"])

        assert excinfo.value.code == 2
        assert "invalid choice" in capsys.readouterr().err

    def test_load_rows_selects_artifact_path_but_export_hides_it(self, tmp_path, gif_dir):
        sprint_root = self._sprint_root_with_snapshot(tmp_path, _seed_rows(gif_dir))

        rows = load_rows(sprint_root / "library_snapshot.db")

        assert all("artifact_path" in row for row in rows)
        # The projection used for shard lines keeps only the locked fields.
        assert "artifact_path" not in EXPORT_KEYS
