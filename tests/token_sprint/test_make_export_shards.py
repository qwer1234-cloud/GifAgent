"""Tests for W1 ``make_export_shards.py``: export-corpus text shards + probe.

Covers: probe is exactly 60 rows from a 70-caption corpus; clips without a
caption never appear; clips whose GIF is missing never appear; candidate_id is
``exp_`` + sha256(resolved export_path)[:12]; tags/scenario mapping with
lowercase dedupe; ``final_score`` falls back to ``gif_worthiness``; explicit
captions land in the ``high`` stratum; relative export_path resolves against
``--repo-root``; existing outputs are never overwritten without ``--force``;
missing corpus dir exits 2. Fake GIFs are 0-byte files -- no image bytes.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from scripts.token_sprint.make_export_shards import main

PROBE_NAME = "shard-probe-export.input.jsonl"


def _sha12(resolved_posix: str) -> str:
    return hashlib.sha256(resolved_posix.encode("utf-8")).hexdigest()[:12]


def _clip(i: int, *, caption: str | None, score=0.5, worthiness=None, extra=None) -> dict:
    clip = {
        "rank": i,
        "caption": caption if caption is not None else f"clip {i} dancing on stage",
        "emotional_core": f"energetic mood {i}",
        "reason": "good loop",
        "aesthetic_notes": ["Neon Lighting", "neon lighting", "Stage"],
        "final_score": score,
        "export_path": f"data/exports/run_a/clip_{i:03d}.gif",
    }
    if worthiness is not None:
        clip["gif_worthiness"] = worthiness
    if extra:
        clip.update(extra)
    return clip


def _build_corpus(
    sprint_root: Path,
    repo_root: Path,
    *,
    n_clips: int = 70,
    with_gif: bool = True,
    add_explicit: bool = True,
) -> None:
    """Write n_clips captioned clips (with 0-byte gifs) into the corpus copy tree."""
    clips = []
    for i in range(n_clips):
        clips.append(_clip(i, caption=f"clip {i} dancing on stage", score=round(0.2 + 0.008 * i, 3)))
    # one explicit clip -> high stratum
    if add_explicit:
        clips.append(
            _clip(900, caption="explicit nude scene on the bed", score=0.1)
        )
    result = {"synthesis": {"tags": ["Cinematic"], "scene_type": "bedroom"}, "top_clips": clips}
    corpus_file = sprint_root / "inputs" / "exports" / "run_a" / "result.json"
    corpus_file.parent.mkdir(parents=True, exist_ok=True)
    corpus_file.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")

    for i in range(n_clips):
        if with_gif:
            gif = repo_root / "data" / "exports" / "run_a" / f"clip_{i:03d}.gif"
            gif.parent.mkdir(parents=True, exist_ok=True)
            gif.write_bytes(b"")  # 0 bytes, never image content
    # explicit clip gets a gif too
    if add_explicit:
        gif = repo_root / "data" / "exports" / "run_a" / "clip_900.gif"
        gif.parent.mkdir(parents=True, exist_ok=True)
        gif.write_bytes(b"")


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


class TestMakeExportShardsCli:
    def test_probe_exactly_60_rows_layers_and_fields(self, tmp_path):
        sprint_root = tmp_path / "sprint"
        repo_root = tmp_path / "repo"
        sprint_root.mkdir()
        repo_root.mkdir()
        _build_corpus(sprint_root, repo_root, n_clips=70)

        rc = main(
            [
                "--sprint-root", str(sprint_root),
                "--repo-root", str(repo_root),
                "--probe",
            ]
        )

        assert rc == 0
        rows = _read_jsonl(sprint_root / "shards" / PROBE_NAME)
        assert len(rows) == 60
        per = {s: sum(1 for r in rows if r["stratum"] == s) for s in ("high", "mid", "low")}
        assert per["high"] == 1  # only one explicit clip exists in this corpus
        assert per["mid"] >= 20
        assert per["low"] >= 20
        for row in rows:
            assert set(row) == {
                "candidate_id",
                "vlm_summary_json",
                "tags_json",
                "scenario_keys_json",
                "final_score",
                "stratum",
                "source_result_json",
                "export_path",
                "gif_exists",
            }
            assert row["gif_exists"] is True
            assert row["source_result_json"] == "inputs/exports/run_a/result.json"
            assert row["candidate_id"].startswith("exp_")
            assert len(row["candidate_id"]) == 4 + 12

    def test_candidate_id_is_sha_of_resolved_path_and_gif_only_exists_checked(self, tmp_path):
        sprint_root = tmp_path / "sprint"
        repo_root = tmp_path / "repo"
        sprint_root.mkdir()
        repo_root.mkdir()
        _build_corpus(sprint_root, repo_root, n_clips=3, add_explicit=False)

        assert main(["--sprint-root", str(sprint_root), "--repo-root", str(repo_root), "--probe"]) == 0

        rows = _read_jsonl(sprint_root / "shards" / PROBE_NAME)
        assert len(rows) == 3
        for row in rows:
            expected = "exp_" + _sha12(row["export_path"])
            assert row["candidate_id"] == expected
            # resolved absolute path under the fake repo root
            assert row["export_path"].startswith(repo_root.as_posix())
            # summary text carries caption/emotional_core/reason
            summary = json.loads(row["vlm_summary_json"])
            assert summary["caption"].startswith("clip ")
            assert summary["emotional_core"].startswith("energetic")
            # tags = aesthetic_notes + synthesis.tags, lowercased + deduped
            tags = json.loads(row["tags_json"])
            assert tags == ["neon lighting", "stage", "cinematic"]
            scenario = json.loads(row["scenario_keys_json"])
            assert scenario[0].startswith("energetic")
            assert scenario[-1] == "bedroom"

    def test_caption_empty_and_gif_missing_clips_excluded(self, tmp_path):
        sprint_root = tmp_path / "sprint"
        repo_root = tmp_path / "repo"
        sprint_root.mkdir()
        repo_root.mkdir()
        _build_corpus(sprint_root, repo_root, n_clips=3, add_explicit=False)
        # extend the corpus with an empty-caption clip and a gif-less clip
        corpus_file = sprint_root / "inputs" / "exports" / "run_a" / "result.json"
        data = json.loads(corpus_file.read_text(encoding="utf-8"))
        data["top_clips"].append(_clip(950, caption="   ", score=0.9))  # blank caption
        data["top_clips"].append(_clip(960, caption="great wave", score=0.9))  # no gif on disk
        corpus_file.write_text(json.dumps(data), encoding="utf-8")

        assert main(["--sprint-root", str(sprint_root), "--repo-root", str(repo_root), "--probe"]) == 0

        rows = _read_jsonl(sprint_root / "shards" / PROBE_NAME)
        captions = [json.loads(r["vlm_summary_json"])["caption"] for r in rows]
        assert all(cap.strip() for cap in captions)
        assert all(r["gif_exists"] is True for r in rows)
        assert len(rows) == 3
        assert not any("great wave" in cap for cap in captions)

    def test_final_score_falls_back_to_gif_worthiness(self, tmp_path):
        sprint_root = tmp_path / "sprint"
        repo_root = tmp_path / "repo"
        sprint_root.mkdir()
        repo_root.mkdir()
        clips = [_clip(0, caption="sunset walk", score=None, worthiness=0.87)]
        result = {"top_clips": clips}
        corpus_file = sprint_root / "inputs" / "exports" / "run_a" / "result.json"
        corpus_file.parent.mkdir(parents=True, exist_ok=True)
        corpus_file.write_text(json.dumps(result), encoding="utf-8")
        gif = repo_root / "data" / "exports" / "run_a" / "clip_000.gif"
        gif.parent.mkdir(parents=True, exist_ok=True)
        gif.write_bytes(b"")

        assert main(["--sprint-root", str(sprint_root), "--repo-root", str(repo_root), "--probe"]) == 0

        row = _read_jsonl(sprint_root / "shards" / PROBE_NAME)[0]
        assert row["final_score"] == 0.87

    def test_missing_corpus_dir_exit_2(self, tmp_path, capsys):
        sprint_root = tmp_path / "sprint"
        sprint_root.mkdir()

        rc = main(["--sprint-root", str(sprint_root), "--probe"])

        assert rc == 2
        assert "ingest_export_corpus" in capsys.readouterr().err

    def test_probe_refuses_existing_probe_without_force(self, tmp_path):
        sprint_root = tmp_path / "sprint"
        repo_root = tmp_path / "repo"
        sprint_root.mkdir()
        repo_root.mkdir()
        _build_corpus(sprint_root, repo_root, n_clips=5)
        assert main(["--sprint-root", str(sprint_root), "--repo-root", str(repo_root), "--probe"]) == 0
        before = (sprint_root / "shards" / PROBE_NAME).read_text(encoding="utf-8")

        rc = main(["--sprint-root", str(sprint_root), "--repo-root", str(repo_root), "--probe"])

        assert rc == 2
        assert (sprint_root / "shards" / PROBE_NAME).read_text(encoding="utf-8") == before

    def test_probe_declares_shortfall_below_60(self, tmp_path, capsys):
        sprint_root = tmp_path / "sprint"
        repo_root = tmp_path / "repo"
        sprint_root.mkdir()
        repo_root.mkdir()
        _build_corpus(sprint_root, repo_root, n_clips=10)

        rc = main(["--sprint-root", str(sprint_root), "--repo-root", str(repo_root), "--probe"])

        assert rc == 0
        assert "< 60" in capsys.readouterr().err
        rows = _read_jsonl(sprint_root / "shards" / PROBE_NAME)
        assert len(rows) == 11  # pool is 10 captions + 1 explicit = 11 rows total

    def test_regular_shards_round_robin_50_200(self, tmp_path):
        sprint_root = tmp_path / "sprint"
        repo_root = tmp_path / "repo"
        sprint_root.mkdir()
        repo_root.mkdir()
        _build_corpus(sprint_root, repo_root, n_clips=149)

        rc = main(
            [
                "--sprint-root", str(sprint_root),
                "--repo-root", str(repo_root),
                "--size", "50",
            ]
        )

        assert rc == 0
        shards = sorted((sprint_root / "shards").glob("shard-export-*.input.jsonl"))
        assert shards
        for shard in shards:
            rows = _read_jsonl(shard)
            assert 50 <= len(rows) <= 200
        total = sum(len(_read_jsonl(s)) for s in shards)
        assert total == 150  # 149 + 1 explicit

    def test_relative_default_repo_root_assumption(self, tmp_path):
        # default repo root = sprint_root.parent.parent (shared root inside <repo>/data)
        repo = tmp_path / "repo"
        sprint_root = repo / "data" / "token_sprint"
        sprint_root.mkdir(parents=True)
        _build_corpus(sprint_root, repo, n_clips=2, add_explicit=False)

        rc = main(["--sprint-root", str(sprint_root), "--probe"])

        assert rc == 0
        rows = _read_jsonl(sprint_root / "shards" / PROBE_NAME)
        assert len(rows) == 2
        assert all(row["export_path"].startswith(repo.as_posix()) for row in rows)
