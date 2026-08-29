"""Tests for W1 ``validate_shard.py``: schema, refusals, sanitization heuristic.

Key locked behaviours:
- a sanitized row (explicit input, output with zero marker/stem retention) is a
  fidelity failure and lands in ``suspected_sanitized``;
- pure refusal rows count into ``refuse_rate`` and never drag ``schema_ok_rate``
  down;
- invalid schemas lower ``schema_ok_rate``;
- p1b subset-checks against ``tag_vocab_v1.json`` and marks ``vocab: missing``
  when the vocab file does not exist yet;
- T0 gates are computed; ``--t0-gate`` turns a failed gate into exit 3.
"""

from __future__ import annotations

import json
from pathlib import Path

from scripts.token_sprint.validate_shard import (
    retains_explicit_marker,
    validate_shard,
)

# ── helpers ──────────────────────────────────────────────────────────────────


def _input_row(
    candidate_id: str,
    *,
    caption: str = "dance performance",
    stratum: str = "low",
    tags: list[str] | None = None,
) -> dict:
    return {
        "candidate_id": candidate_id,
        "vlm_summary_json": json.dumps({"caption": caption}),
        "tags_json": json.dumps(tags or ["dance"]),
        "scenario_keys_json": json.dumps([]),
        "final_score": 0.4,
        "stratum": stratum,
    }


def _write_jsonl(path: Path, rows: list[dict | str]) -> Path:
    lines = [r if isinstance(r, str) else json.dumps(r, ensure_ascii=False) for r in rows]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _p3_row(candidate_id: str, keywords: list[str]) -> dict:
    return {"candidate_id": candidate_id, "keywords": keywords}


def _validate(tmp_path: Path, input_rows: list[dict], out_rows: list[dict | str], **kw) -> dict:
    input_shard = _write_jsonl(tmp_path / "shards" / "in.input.jsonl", input_rows)
    shard = _write_jsonl(tmp_path / "shards" / "out.jsonl", out_rows)
    return validate_shard(
        shard=shard,
        input_shard=input_shard,
        pass_id=kw.pop("pass_id", "p3"),
        prompt_version=kw.pop("prompt_version", "v1"),
        sprint_root=kw.pop("sprint_root", tmp_path / "sprint"),
        **kw,
    )


# ── unit: marker retention ───────────────────────────────────────────────────


class TestRetention:
    def test_retains_marker_and_snake_case_compounds(self):
        assert retains_explicit_marker("nude figure study")
        assert retains_explicit_marker("nude_pose")
        assert retains_explicit_marker("HOT_SEX_SCENE")

    def test_retains_synonym_stems(self):
        assert retains_explicit_marker("unclothed figure")
        assert retains_explicit_marker("sensual dance")

    def test_zero_retention(self):
        assert not retains_explicit_marker("artistic portrait, figure study, studio light")
        assert not retains_explicit_marker("")
        assert not retains_explicit_marker(None)


# ── fidelity / sanitization ──────────────────────────────────────────────────


class TestFidelity:
    def test_sanitized_explicit_row_is_flagged(self, tmp_path):
        inputs = [_input_row("c1", caption="explicit nude scene", stratum="high")]
        outs = [_p3_row("c1", ["artistic", "portrait", "studio"])]

        qc = _validate(tmp_path, inputs, outs)

        assert qc["suspected_sanitized"] == ["c1"]
        assert qc["fidelity_rate_high_stratum"] == 0.0
        assert qc["t0_gates"]["fidelity_rate_high_stratum_ge_90pct"] is False

    def test_marker_retained_row_passes(self, tmp_path):
        inputs = [_input_row("c1", caption="explicit nude scene", stratum="high")]
        outs = [_p3_row("c1", ["nude", "studio", "portrait"])]

        qc = _validate(tmp_path, inputs, outs)

        assert qc["suspected_sanitized"] == []
        assert qc["fidelity_rate_high_stratum"] == 1.0

    def test_marker_in_input_text_counts_even_without_high_stratum(self, tmp_path):
        inputs = [_input_row("c1", caption="naked torso visible", stratum="low")]
        outs = [_p3_row("c1", ["artistic", "soft", "light"])]

        qc = _validate(tmp_path, inputs, outs)

        assert qc["suspected_sanitized"] == ["c1"]

    def test_low_stratum_clean_input_not_in_fidelity_denominator(self, tmp_path):
        inputs = [_input_row("c1", caption="cooking tutorial", stratum="low")]
        outs = [_p3_row("c1", ["cooking", "kitchen", "recipe"])]

        qc = _validate(tmp_path, inputs, outs)

        assert qc["fidelity_rate_high_stratum"] is None
        assert qc["suspected_sanitized"] == []


# ── refusals / schema rates ──────────────────────────────────────────────────


class TestRates:
    def test_pure_refusals_count_into_refuse_only(self, tmp_path):
        inputs = [_input_row("c1"), _input_row("c2"), _input_row("c3")]
        outs = [
            {"candidate_id": "c1", "refused": True},
            {"candidate_id": "c2", "refused": True},
            {"candidate_id": "c3", "refused": True},
        ]

        qc = _validate(tmp_path, inputs, outs)

        assert qc["n"] == 3
        assert qc["refuse_rate"] == 1.0
        assert qc["schema_ok_rate"] == 1.0  # refusals never lower schema_ok
        assert qc["t0_gates"]["refuse_rate_le_15pct"] is False

    def test_invalid_schema_lowers_schema_ok(self, tmp_path):
        inputs = [_input_row(f"c{i}") for i in range(1, 5)]
        outs = [
            _p3_row("c1", ["one", "two"]),  # too few keywords
            _p3_row("c2", ["Bad", "token", "words"]),  # uppercase
            _p3_row("c3", ["good", "tokens", "here"]),
            _p3_row("c4", ["good", "tokens", "here"]),
        ]

        qc = _validate(tmp_path, inputs, outs)

        assert qc["n"] == 4
        assert qc["refuse_rate"] == 0.0
        assert qc["schema_ok_rate"] == 0.5
        assert any("keywords" in err for err in qc["errors"])
        assert any("Bad" in err for err in qc["errors"])

    def test_mixed_refusals_and_valid_rows(self, tmp_path):
        inputs = [_input_row(f"c{i}") for i in range(1, 5)]
        outs = [
            _p3_row("c1", ["good", "tokens", "here"]),
            _p3_row("c2", ["bad"]),  # schema fail
            {"candidate_id": "c3", "refused": True},
            {"candidate_id": "c4", "refused": True},
        ]

        qc = _validate(tmp_path, inputs, outs)

        assert qc["n"] == 4
        assert qc["refuse_rate"] == 0.5
        assert qc["schema_ok_rate"] == 0.5  # 1 valid / 2 non-refusal

    def test_non_json_line_is_error_and_schema_failure(self, tmp_path):
        inputs = [_input_row("c1"), _input_row("c2")]
        outs = [
            _p3_row("c1", ["good", "tokens", "here"]),
            "this is not json",
        ]

        qc = _validate(tmp_path, inputs, outs)

        assert qc["n"] == 2
        assert qc["errors"]
        assert "不是合法 JSON" in qc["errors"][0]
        assert qc["schema_ok_rate"] == 0.5

    def test_unknown_candidate_id_is_error(self, tmp_path):
        inputs = [_input_row("c1")]
        outs = [_p3_row("ghost", ["good", "tokens", "here"])]

        qc = _validate(tmp_path, inputs, outs)

        assert any("不在 input 分片" in err for err in qc["errors"])
        assert qc["schema_ok_rate"] == 0.0
        assert qc["coverage_rate"] == 0.0


# ── pass schemas ─────────────────────────────────────────────────────────────


class TestPassSchemas:
    def test_p1b_subset_of_vocab(self, tmp_path):
        sprint_root = tmp_path / "sprint"
        sprint_root.mkdir()
        (sprint_root / "tag_vocab_v1.json").write_text(
            json.dumps({"tags": ["dance", "studio", "night"]}), encoding="utf-8"
        )
        inputs = [_input_row("c1"), _input_row("c2")]
        outs = [
            {"candidate_id": "c1", "tags": ["dance", "studio"]},
            {"candidate_id": "c2", "tags": ["unknown-tag"]},
        ]

        qc = _validate(tmp_path, inputs, outs, pass_id="p1b", sprint_root=sprint_root)

        assert qc["vocab"] is None
        assert qc["schema_ok_rate"] == 0.5
        assert any("不在词表内" in err for err in qc["errors"])

    def test_p1b_marks_vocab_missing_and_skips_subset(self, tmp_path):
        inputs = [_input_row("c1")]
        outs = [{"candidate_id": "c1", "tags": ["anything-goes"]}]

        qc = _validate(tmp_path, inputs, outs, pass_id="p1b")

        assert qc["vocab"] == "missing"
        assert qc["schema_ok_rate"] == 1.0

    def test_p2_scene_schema(self, tmp_path):
        inputs = [_input_row("c1"), _input_row("c2")]
        outs = [
            {"candidate_id": "c1", "scene": {"setting": "beach", "act": "dance", "finish": None}},
            {"candidate_id": "c2", "scene": {"setting": "room"}},  # missing act/finish
        ]

        qc = _validate(tmp_path, inputs, outs, pass_id="p2")

        assert qc["schema_ok_rate"] == 0.5
        assert any("scene 缺少键" in err for err in qc["errors"])

    def test_p1a_word_forms(self, tmp_path):
        inputs = [_input_row("trial-1"), _input_row("trial-2")]
        outs = [
            {"words": ["beach_dance", "night_club", "summer"]},
            {"words": ["BadWord", "ok_word"]},
        ]

        qc = _validate(tmp_path, inputs, outs, pass_id="p1a")

        assert qc["schema_ok_rate"] == 0.5
        assert any("词形非法" in err for err in qc["errors"])


# ── T0 gate enforcement + QC file ────────────────────────────────────────────


class TestGatesAndQcFile:
    def _files(self, tmp_path: Path):
        sprint_root = tmp_path / "sprint"
        sprint_root.mkdir(parents=True, exist_ok=True)
        inputs = [_input_row("c1", caption="explicit nude scene", stratum="high")]
        outs = [_p3_row("c1", ["artistic", "portrait", "studio"])]  # sanitized
        input_shard = _write_jsonl(tmp_path / "shards" / "in.input.jsonl", inputs)
        shard = _write_jsonl(tmp_path / "shards" / "out.jsonl", outs)
        return shard, input_shard

    def test_gate_failure_exit_3_only_with_flag(self, tmp_path, capsys):
        shard, input_shard = self._files(tmp_path)
        from scripts.token_sprint.validate_shard import main

        base = [
            "--sprint-root", str(tmp_path / "sprint"),
            "--shard", str(shard),
            "--pass-id", "p3",
            "--prompt-version", "v1",
            "--input-shard", str(input_shard),
        ]

        assert main(base) == 0  # gates reported, exit 0
        assert main(base + ["--t0-gate"]) == 3  # enforced

    def test_qc_json_written_next_to_shard(self, tmp_path, capsys):
        shard, input_shard = self._files(tmp_path)
        from scripts.token_sprint.validate_shard import main

        rc = main([
            "--sprint-root", str(tmp_path / "sprint"),
            "--shard", str(shard),
            "--pass-id", "p3",
            "--prompt-version", "v1",
            "--input-shard", str(input_shard),
        ])

        assert rc == 0
        qc_path = shard.parent / (shard.name + ".qc.json")
        assert qc_path.is_file()
        qc = json.loads(qc_path.read_text(encoding="utf-8"))
        assert qc["n"] == 1
        assert qc["suspected_sanitized"] == ["c1"]
        # stdout carries the same QC JSON
        stdout_qc = json.loads(capsys.readouterr().out)
        assert stdout_qc["pass_id"] == "p3"
        assert "prompt_sha256" in stdout_qc
        assert "t0_gates" in stdout_qc

    def test_missing_input_shard_exit_2(self, tmp_path, capsys):
        from scripts.token_sprint.validate_shard import main

        (tmp_path / "sprint").mkdir(parents=True, exist_ok=True)
        shard = _write_jsonl(tmp_path / "shards" / "out.jsonl", [])
        rc = main([
            "--sprint-root", str(tmp_path / "sprint"),
            "--shard", str(shard),
            "--pass-id", "p3",
            "--prompt-version", "v1",
            "--input-shard", str(tmp_path / "shards" / "nope.jsonl"),
        ])

        assert rc == 2
        assert capsys.readouterr().err
