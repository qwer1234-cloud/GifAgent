"""Tests for the W2 synth preference stress harness (token sprint Task 3).

Contract under test (implementation plan 2026-08-28-token-sprint-w2-eval-harness):

- All 7 build gates of ``evaluate_profile_gates`` get a just-failing and a
  just-passing fixture (14 gate-level checks) on in-memory / tmp SQLite.
- ``gate_reasons`` string prefixes stay stable (exact strings locked).
- The tool never writes the shared root or any production database and never
  flips ``preference_memory.enabled``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.token_sprint import synth_preference_stress as sps


@pytest.fixture(scope="module")
def matrix() -> dict:
    return sps.run_matrix(db=":memory:")


# ---------------------------------------------------------------------------
# 7 gates x (fail, pass) = 14 boundary checks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("gate", ["1", "2", "3", "4", "5", "6", "7"])
def test_gate_fail_and_pass_boundaries(matrix: dict, gate: str):
    entry = matrix["gate_matrix"][gate]
    assert entry["fail_reason_prefix_present"] is True, entry
    assert entry["pass_reason_prefix_absent"] is True, entry
    assert entry["exact_expected_reasons"] is True, entry


def test_all_fixtures_exact_expected_reasons(matrix: dict):
    assert all(fixture["pass"] for fixture in matrix["fixtures"])
    assert matrix["all_expectations_met"] is True


def test_gate6_has_both_variants(matrix: dict):
    gate6 = matrix["gate_matrix"]["6"]
    assert gate6["fail_fixtures"] == [
        "gate6a_fail_no_vectors",
        "gate6b_fail_model_mismatch",
    ]


# ---------------------------------------------------------------------------
# Exact reason strings per fixture (prefix stability locked)
# ---------------------------------------------------------------------------

EXPECTED_REASONS = {
    "all_pass_baseline": [],
    "gate1_fail_29_effective": ["effective_feedback_count=29 < 30"],
    "gate2_fail_14_likes": ["like_count=14 < 15"],
    "gate3_fail_9_dislikes": ["dislike_count=9 < 10"],
    "gate4_fail_2_videos": [
        "source_video_count=2 < 3",
        "max_single_video_share=0.50 > 0.4",
    ],
    "gate5_fail_share_043": ["max_single_video_share=0.43 > 0.4"],
    "gate5_pass_share_exact_040": [],
    "gate6a_fail_no_vectors": ["no_vectors_found in candidate_vectors"],
    "gate6b_fail_model_mismatch": [
        "embedding_model mismatch: required=nomic-embed-text:latest dim=768 coverage=0"
    ],
    "gate7_fail_29_of_30_vectors": [
        "candidate_vector_count=29 < effective_feedback_count=30"
    ],
}

GATE_PREFIX_BY_FIXTURE = {
    "gate1_fail_29_effective": "effective_feedback_count=",
    "gate2_fail_14_likes": "like_count=",
    "gate3_fail_9_dislikes": "dislike_count=",
    "gate4_fail_2_videos": "source_video_count=",
    "gate5_fail_share_043": "max_single_video_share=",
    "gate6a_fail_no_vectors": "no_vectors_found in candidate_vectors",
    "gate6b_fail_model_mismatch": "embedding_model mismatch: required=",
    "gate7_fail_29_of_30_vectors": "candidate_vector_count=",
}


def test_fixture_reason_map_is_complete():
    names = {spec.name for spec in sps.build_fixtures()}
    assert names == set(EXPECTED_REASONS)


@pytest.mark.parametrize("name", sorted(EXPECTED_REASONS))
def test_fixture_reasons_exact_and_prefix_stable(matrix: dict, name: str):
    entry = next(item for item in matrix["fixtures"] if item["name"] == name)
    assert entry["actual_gate_reasons"] == EXPECTED_REASONS[name]
    prefix = GATE_PREFIX_BY_FIXTURE.get(name)
    if prefix is not None:
        assert entry["expected_gate_reasons"], f"fixture {name} must expect reasons"
        # Prefix stability: the fixture's own gate prefix must appear among the
        # fired reasons (multi-reason fixtures may also trip neighbouring gates,
        # e.g. two source videos force share >= 0.5 and trip gate 5 as well).
        assert any(
            reason.startswith(prefix) for reason in entry["actual_gate_reasons"]
        ), entry["actual_gate_reasons"]


def test_gate5_boundary_share_exact_040_passes(matrix: dict):
    entry = next(
        item for item in matrix["fixtures"]
        if item["name"] == "gate5_pass_share_exact_040"
    )
    assert entry["metrics"]["max_single_video_share"] == 0.4
    assert entry["actual_gate_reasons"] == []  # strict > keeps 0.40 passing


def test_gate4_fail_also_trips_share_gate(matrix: dict):
    # Two source videos force max share >= 0.5: the fixture documents that
    # gate 4 failure is not isolable from gate 5 at 30 events.
    entry = next(
        item for item in matrix["fixtures"] if item["name"] == "gate4_fail_2_videos"
    )
    assert entry["actual_gate_reasons"] == [
        "source_video_count=2 < 3",
        "max_single_video_share=0.50 > 0.4",
    ]


# ---------------------------------------------------------------------------
# CLI behaviour
# ---------------------------------------------------------------------------


def test_cli_exit_0_report_and_no_sprint_root_writes(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("GIFAGENT_SPRINT_ROOT", raising=False)
    code = sps.main(["--sprint-root", str(tmp_path)])
    captured = capsys.readouterr()
    assert code == 0
    report = json.loads(captured.out)
    assert report["all_expectations_met"] is True
    assert report["sprint_root"] == tmp_path.resolve().as_posix()
    assert report["db"] == ":memory:"
    # In-memory default: nothing may appear in the sprint root.
    assert list(tmp_path.iterdir()) == []


def test_cli_missing_sprint_root_exits_2(capsys, monkeypatch):
    monkeypatch.delenv("GIFAGENT_SPRINT_ROOT", raising=False)
    with pytest.raises(SystemExit) as excinfo:
        sps.main([])
    assert excinfo.value.code == 2


def test_cli_file_db_runs_and_writes_only_db(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("GIFAGENT_SPRINT_ROOT", raising=False)
    db_path = tmp_path / "stress.db"
    code = sps.main(["--sprint-root", str(tmp_path), "--db", str(db_path)])
    captured = capsys.readouterr()
    assert code == 0
    report = json.loads(captured.out)
    assert report["all_expectations_met"] is True
    assert db_path.is_file()  # tmp db used, not the sprint root itself
    # Only the requested db file was created in the tmp directory.
    assert [p.name for p in tmp_path.iterdir()] == ["stress.db"]


def test_two_runs_identical():
    first = sps.report_to_json(sps.run_matrix(db=":memory:"))
    second = sps.report_to_json(sps.run_matrix(db=":memory:"))
    assert first == second


def test_cli_output_flag_writes_report(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("GIFAGENT_SPRINT_ROOT", raising=False)
    out_path = tmp_path / "sub" / "stress-report.json"
    code = sps.main(["--sprint-root", str(tmp_path), "--output", str(out_path)])
    captured = capsys.readouterr()
    assert code == 0
    assert json.loads(out_path.read_text(encoding="utf-8")) == json.loads(captured.out)


def test_tool_source_never_touches_config_or_flag():
    # Structural guard: the stress tool only drives evaluate_profile_gates on
    # fixture databases; it never consults config files nor the
    # preference_memory.enabled flag (explicit non-goal).
    source = Path(sps.__file__).read_text(encoding="utf-8")
    assert "models.yaml" not in source
    assert "preference_memory.enabled" not in source
    assert "configs" not in source
