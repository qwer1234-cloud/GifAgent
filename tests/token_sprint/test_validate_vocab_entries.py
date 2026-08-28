"""p1b vocab subset check must read the frozen W1 ``entries``-format vocabulary.

The frozen ``tag_vocab_v1.json`` published by Stage A is
``{"entries": [{"term": ..., "group": ...}, ...]}`` — not a bare tag list.
Before the ``entries`` branch existed, ``_load_vocab`` fell back to the JSON
top-level keys, so every legitimate tag failed the subset check (0% schema_ok).
This regression test locks the fail-closed correct behaviour for both vocab
layouts: in-vocab tags pass, out-of-vocab tags fail.
"""

from __future__ import annotations

import json

from scripts.token_sprint.validate_shard import _load_vocab


def _write_vocab(sprint_root, payload) -> None:
    sprint_root.mkdir(parents=True, exist_ok=True)
    (sprint_root / "tag_vocab_v1.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def test_load_vocab_reads_frozen_entries_format(tmp_path):
    sprint_root = tmp_path / "sprint"
    _write_vocab(
        sprint_root,
        {
            "schema": "w1.tag_vocab_v1",
            "n_terms": 3,
            "entries": [
                {"term": "nude", "group": "explicit", "count": 9},
                {"term": "bedroom", "group": "setting", "count": 5},
                {"term": "smiling", "group": "mood", "count": 2},
            ],
        },
    )
    vocab, status = _load_vocab(sprint_root)
    assert status is None
    assert vocab == {"nude", "bedroom", "smiling"}


def test_p1b_subset_against_entries_format_vocab(tmp_path):
    from scripts.token_sprint.validate_shard import validate_shard

    sprint_root = tmp_path / "sprint"
    _write_vocab(
        sprint_root,
        {
            "entries": [
                {"term": "nude", "group": "explicit"},
                {"term": "bedroom", "group": "setting"},
            ]
        },
    )

    input_rows = [
        {
            "candidate_id": f"c{i}",
            "vlm_summary_json": json.dumps({"caption": "nude in bedroom"}),
            "tags_json": json.dumps([]),
            "scenario_keys_json": json.dumps([]),
            "final_score": 0.5,
            "stratum": "high",
        }
        for i in (1, 2, 3)
    ]
    out_rows = [
        {"candidate_id": "c1", "tags": ["nude", "bedroom"]},
        {"candidate_id": "c2", "tags": ["bedroom"]},
        {"candidate_id": "c3", "tags": ["nudity"]},  # not in frozen vocab
    ]

    input_shard = tmp_path / "in.jsonl"
    shard = tmp_path / "out.jsonl"
    input_shard.write_text(
        "\n".join(json.dumps(r) for r in input_rows) + "\n", encoding="utf-8"
    )
    shard.write_text(
        "\n".join(json.dumps(r) for r in out_rows) + "\n", encoding="utf-8"
    )

    qc = validate_shard(
        shard=shard,
        input_shard=input_shard,
        pass_id="p1b",
        prompt_version="v1",
        sprint_root=sprint_root,
    )
    assert qc["vocab"] is None
    assert qc["schema_ok_rate"] == 2.0 / 3.0
    assert any("不在词表内: ['nudity']" in err for err in qc["errors"])
