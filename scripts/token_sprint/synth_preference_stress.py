"""W2 deterministic stress harness for the preference profile build gates.

Stresses ``app.services.preference_memory.evaluate_profile_gates`` against all
seven build gates on in-memory SQLite (or an explicit tmp file via ``--db``).
Production databases are never touched: the shared root is only validated via
the standard ``--sprint-root`` protocol argument; nothing is written there.

Gate coverage (one just-failing and one just-passing fixture each, 14 checks):

1. ``effective_feedback_count < MIN_EFFECTIVE_FEEDBACK`` (30)
2. ``like_count < MIN_LIKE_COUNT`` (15)
3. ``dislike_count < MIN_DISLIKE_COUNT`` (10)
4. ``source_video_count < MIN_SOURCE_VIDEOS`` (3)
5. ``max_single_video_share > MAX_SINGLE_VIDEO_SHARE`` (0.40)
6. no vectors at all / embedding_model mismatch in ``candidate_vectors``
7. ``candidate_vector_count < effective_feedback_count``

The test asserts each gate reason string prefix stays stable (exact strings
are fully determined by the fixture counts).  This tool only *reads* the
preference module; the preference-memory enable flag and every config file
stay untouched (flipping the flag is an explicit non-goal).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np

# Add project root to path so direct script execution works (same pattern as
# scripts/token_sprint/coordinator/preflight.py).
_project_root = Path(__file__).resolve().parents[2]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from app.services.preference_memory import (  # noqa: E402
    MAX_SINGLE_VIDEO_SHARE,
    MIN_DISLIKE_COUNT,
    MIN_EFFECTIVE_FEEDBACK,
    MIN_LIKE_COUNT,
    MIN_SOURCE_VIDEOS,
    REQUIRED_EMBEDDING_DIM,
    REQUIRED_EMBEDDING_MODEL,
    evaluate_profile_gates,
)
from app.services.preference_schema import apply_preference_schema  # noqa: E402
from scripts.token_sprint.coordinator.common import (  # noqa: E402
    add_sprint_root_argument,
    atomic_write_text,
    resolve_sprint_root,
)

REPORT_SCHEMA = "gifagent.token_sprint.synth_preference_stress.report.v1"

# Stable reason prefixes per gate (locked via tests).
GATE_PREFIXES: dict[str, str] = {
    "1": "effective_feedback_count=",
    "2": "like_count=",
    "3": "dislike_count=",
    "4": "source_video_count=",
    "5": "max_single_video_share=",
    "6a": "no_vectors_found in candidate_vectors",
    "6b": "embedding_model mismatch: required=",
    "7": "candidate_vector_count=",
}

_EMBEDDING_DIM = REQUIRED_EMBEDDING_DIM
_BASE_TS = datetime(2026, 8, 1, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Fixture specification
# ---------------------------------------------------------------------------


@dataclass
class FixtureSpec:
    name: str
    gate: str  # "1".."7" (gate 6 has variants "6a"/"6b")
    likes: int
    dislikes: int
    video_sizes: list[int]  # events per source video; sum == likes + dislikes
    vector_count: int | None  # targets with correct-model vectors; None = all
    vector_model: str = REQUIRED_EMBEDDING_MODEL
    expected_reasons: tuple[str, ...] = field(default=())

    @property
    def n_events(self) -> int:
        return self.likes + self.dislikes

    @property
    def n_targets(self) -> int:
        return self.n_events


def _video_shas(spec: FixtureSpec) -> list[str]:
    shas: list[str] = []
    for index, size in enumerate(spec.video_sizes):
        shas.extend([f"vid{index:02d}sha"] * size)
    if len(shas) != spec.n_events:
        raise ValueError(
            f"fixture {spec.name}: video_sizes sums to {len(shas)}, "
            f"expected {spec.n_events}"
        )
    return shas


def build_fixtures() -> list[FixtureSpec]:
    """Ten fixtures covering all 7 gates (fail + pass evidence each)."""
    return [
        FixtureSpec(
            name="all_pass_baseline",
            gate="0",
            likes=15,
            dislikes=15,
            video_sizes=[6, 6, 6, 6, 6],
            vector_count=None,
            expected_reasons=(),
        ),
        FixtureSpec(
            name="gate1_fail_29_effective",
            gate="1",
            likes=15,
            dislikes=14,  # 29 effective events: just below the 30 minimum
            video_sizes=[8, 7, 7, 7],
            vector_count=None,
            expected_reasons=(f"effective_feedback_count=29 < {MIN_EFFECTIVE_FEEDBACK}",),
        ),
        FixtureSpec(
            name="gate2_fail_14_likes",
            gate="2",
            likes=14,  # just below the 15-like minimum
            dislikes=16,
            video_sizes=[6, 6, 6, 6, 6],
            vector_count=None,
            expected_reasons=(f"like_count=14 < {MIN_LIKE_COUNT}",),
        ),
        FixtureSpec(
            name="gate3_fail_9_dislikes",
            gate="3",
            likes=21,
            dislikes=9,  # just below the 10-dislike minimum; 30 events total
            video_sizes=[6, 6, 6, 6, 6],
            vector_count=None,
            expected_reasons=(f"dislike_count=9 < {MIN_DISLIKE_COUNT}",),
        ),
        FixtureSpec(
            name="gate4_fail_2_videos",
            gate="4",
            likes=20,
            dislikes=10,
            video_sizes=[15, 15],  # only 2 source videos; share 0.50 trips gate 5 too
            vector_count=None,
            expected_reasons=(
                f"source_video_count=2 < {MIN_SOURCE_VIDEOS}",
                f"max_single_video_share=0.50 > {MAX_SINGLE_VIDEO_SHARE}",
            ),
        ),
        FixtureSpec(
            name="gate5_fail_share_043",
            gate="5",
            likes=20,
            dislikes=10,
            video_sizes=[13, 9, 8],  # 13/30 = 0.4333 just above 0.40
            vector_count=None,
            expected_reasons=(f"max_single_video_share=0.43 > {MAX_SINGLE_VIDEO_SHARE}",),
        ),
        FixtureSpec(
            name="gate5_pass_share_exact_040",
            gate="5",
            likes=20,
            dislikes=10,
            video_sizes=[12, 9, 9],  # 12/30 = exactly 0.40 -> passes (strict >)
            vector_count=None,
            expected_reasons=(),
        ),
        FixtureSpec(
            name="gate6a_fail_no_vectors",
            gate="6a",
            likes=15,
            dislikes=15,
            video_sizes=[6, 6, 6, 6, 6],
            vector_count=0,
            expected_reasons=(GATE_PREFIXES["6a"],),
        ),
        FixtureSpec(
            name="gate6b_fail_model_mismatch",
            gate="6b",
            likes=15,
            dislikes=15,
            video_sizes=[6, 6, 6, 6, 6],
            vector_count=None,
            vector_model="other-model:latest",
            expected_reasons=(
                "embedding_model mismatch: required="
                f"{REQUIRED_EMBEDDING_MODEL} dim={REQUIRED_EMBEDDING_DIM} coverage=0",
            ),
        ),
        FixtureSpec(
            name="gate7_fail_29_of_30_vectors",
            gate="7",
            likes=15,
            dislikes=15,
            video_sizes=[6, 6, 6, 6, 6],
            vector_count=29,  # one target without a vector: 29 < 30
            expected_reasons=(
                "candidate_vector_count=29 < effective_feedback_count=30",
            ),
        ),
    ]


# ---------------------------------------------------------------------------
# Fixture database construction (in-memory or tmp file; never production)
# ---------------------------------------------------------------------------


def _vector_blob(index: int) -> bytes:
    raw = np.linspace(-1.0, 1.0, _EMBEDDING_DIM, dtype=np.float64)
    raw = raw + (index % 7) * 0.001  # per-candidate variation, deterministic
    normalized = raw / np.linalg.norm(raw)
    return normalized.astype(np.float32).tobytes()


def build_fixture_conn(spec: FixtureSpec, db: str = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:" if db == ":memory:" else db)
    conn.row_factory = sqlite3.Row
    apply_preference_schema(conn)

    shas = _video_shas(spec)
    ratings = ["like"] * spec.likes + ["dislike"] * spec.dislikes

    for index in range(spec.n_targets):
        target_id = f"cand_{index:04d}"
        created_at = (_BASE_TS + timedelta(minutes=index)).isoformat()
        conn.execute(
            """INSERT INTO candidate_gifs
               (candidate_id, source_run_id, source_run_candidate_id,
                source_video_sha256, source_video_path, start_sec, end_sec,
                artifact_path, preview_path, vlm_summary_json, tags_json,
                scenario_keys_json, base_rag_similarity, final_score, status,
                created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                target_id,
                "stress-run",
                f"rc-{target_id}",
                shas[index],
                "/videos/stress.mp4",
                0.0,
                5.0,
                "data/exports/stress.gif",
                "data/thumbs/stress.jpg",
                "{}",
                "[]",
                "[]",
                None,
                None,
                "candidate",
                created_at,
                created_at,
            ),
        )
        conn.execute(
            """INSERT INTO preference_events
               (event_id, target_type, target_id, rating,
                source_video_sha256, scenario_keys_json, note, created_at,
                previous_status, event_kind, supersedes_event_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                f"evt_{index:04d}",
                "candidate_gif",
                target_id,
                ratings[index],
                shas[index],
                "[]",
                None,
                created_at,
                None,
                "feedback",
                None,
            ),
        )

    vector_targets = spec.n_targets if spec.vector_count is None else spec.vector_count
    for index in range(vector_targets):
        conn.execute(
            """INSERT OR REPLACE INTO candidate_vectors
               (candidate_id, vector_type, embedding_model, embedding_dim, vector_blob)
               VALUES (?,?,?,?,?)""",
            (
                f"cand_{index:04d}",
                "clip",
                spec.vector_model,
                _EMBEDDING_DIM,
                _vector_blob(index),
            ),
        )
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Matrix execution
# ---------------------------------------------------------------------------


def run_matrix(db: str = ":memory:") -> dict[str, Any]:
    """Run every fixture on a fresh connection; return the full report dict."""
    fixtures_report: list[dict[str, Any]] = []
    db_path = Path(db) if db != ":memory:" else None
    for spec in build_fixtures():
        if db_path is not None and db_path.exists():
            db_path.unlink()  # each fixture starts from a clean file db
        conn = build_fixture_conn(spec, db)
        try:
            snapshot = evaluate_profile_gates(conn)
        finally:
            conn.close()
        actual = list(snapshot.gate_reasons)
        passed = actual == list(spec.expected_reasons)
        fixtures_report.append(
            {
                "name": spec.name,
                "gate": spec.gate,
                "expected_gate_reasons": list(spec.expected_reasons),
                "actual_gate_reasons": actual,
                "metrics": {
                    "effective_feedback_count": snapshot.effective_count,
                    "like_count": snapshot.like_count,
                    "dislike_count": snapshot.dislike_count,
                    "source_video_count": snapshot.source_video_count,
                    "max_single_video_share": round(
                        snapshot.max_single_video_share, 6
                    ),
                    "candidate_vector_count": snapshot.candidate_vector_count,
                },
                "pass": passed,
            }
        )

    by_name = {entry["name"]: entry for entry in fixtures_report}
    gate_matrix: dict[str, dict[str, Any]] = {}
    gate_pairs = {
        "1": ("gate1_fail_29_effective", "all_pass_baseline"),
        "2": ("gate2_fail_14_likes", "all_pass_baseline"),
        "3": ("gate3_fail_9_dislikes", "all_pass_baseline"),
        "4": ("gate4_fail_2_videos", "all_pass_baseline"),
        "5": ("gate5_fail_share_043", "gate5_pass_share_exact_040"),
        "6": ("gate6a_fail_no_vectors", "all_pass_baseline"),
        "6b": ("gate6b_fail_model_mismatch", "all_pass_baseline"),
        "7": ("gate7_fail_29_of_30_vectors", "all_pass_baseline"),
    }
    for gate, (fail_name, pass_name) in gate_pairs.items():
        fail_entry = by_name[fail_name]
        pass_entry = by_name[pass_name]
        fail_prefix = GATE_PREFIXES["6a" if gate == "6" else gate]
        if gate == "6":
            model_entry = by_name["gate6b_fail_model_mismatch"]
            fail_ok = (
                any(
                    reason.startswith(fail_prefix)
                    for reason in fail_entry["actual_gate_reasons"]
                )
                and any(
                    reason.startswith(GATE_PREFIXES["6b"])
                    for reason in model_entry["actual_gate_reasons"]
                )
            )
            fail_fixture_names = [fail_name, "gate6b_fail_model_mismatch"]
        else:
            fail_ok = any(
                reason.startswith(fail_prefix)
                for reason in fail_entry["actual_gate_reasons"]
            )
            fail_fixture_names = [fail_name]
        gate_matrix[gate] = {
            "fail_fixtures": fail_fixture_names,
            "pass_fixture": pass_name,
            "fail_reason_prefix_present": fail_ok,
            "pass_reason_prefix_absent": not any(
                reason.startswith(fail_prefix)
                for reason in pass_entry["actual_gate_reasons"]
            ),
            "exact_expected_reasons": fail_entry["pass"] and pass_entry["pass"],
        }

    all_met = all(entry["pass"] for entry in fixtures_report) and all(
        entry["fail_reason_prefix_present"]
        and entry["pass_reason_prefix_absent"]
        and entry["exact_expected_reasons"]
        for entry in gate_matrix.values()
    )
    return {
        "schema": REPORT_SCHEMA,
        "db": db,
        "gate_constants": {
            "MIN_EFFECTIVE_FEEDBACK": MIN_EFFECTIVE_FEEDBACK,
            "MIN_LIKE_COUNT": MIN_LIKE_COUNT,
            "MIN_DISLIKE_COUNT": MIN_DISLIKE_COUNT,
            "MIN_SOURCE_VIDEOS": MIN_SOURCE_VIDEOS,
            "MAX_SINGLE_VIDEO_SHARE": MAX_SINGLE_VIDEO_SHARE,
            "REQUIRED_EMBEDDING_MODEL": REQUIRED_EMBEDDING_MODEL,
            "REQUIRED_EMBEDDING_DIM": REQUIRED_EMBEDDING_DIM,
        },
        "fixtures": fixtures_report,
        "gate_matrix": gate_matrix,
        "all_expectations_met": all_met,
    }


def report_to_json(report: dict[str, Any]) -> str:
    return json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Stress all 7 preference profile build gates on in-memory SQLite "
            "(never touches production databases; --sprint-root is required "
            "only for the shared protocol)."
        )
    )
    add_sprint_root_argument(parser)
    parser.add_argument(
        "--db",
        default=":memory:",
        help="SQLite file for fixture databases (default :memory:; the file is "
        "reset per fixture and must not be a production database).",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Optional report JSON output path (atomically written).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    sprint_root = resolve_sprint_root(args.sprint_root)

    report = run_matrix(db=args.db)
    report["sprint_root"] = sprint_root.as_posix()
    payload = report_to_json(report)

    if args.output:
        output = Path(args.output)
        if not output.is_absolute():
            output = sprint_root / output
        atomic_write_text(output, payload)

    sys.stdout.write(payload)
    return 0 if report["all_expectations_met"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
