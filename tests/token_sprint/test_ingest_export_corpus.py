"""Tests for W1 ``ingest_export_corpus.py``: immutable JSON-only corpus copies.

Covers: only result JSONs are copied (media and other files skipped, media
bytes never read); copies preserve the exports subdirectory structure; the
manifest records source_abs/copy_rel/sha256/as_of with correct SHA-256; re-runs
are idempotent without overwriting; a changed source after the first copy is an
immutability conflict (exit 2, target untouched); the frozen coordinator
``inputs_manifest.json`` is never modified.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from scripts.token_sprint.ingest_export_corpus import main

MANIFEST_NAME = "exports_corpus_manifest.json"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _build_exports(exports_root: Path) -> dict[str, bytes]:
    """Create a fake exports tree; .gif files are 0 bytes (never image bytes)."""
    files: dict[str, bytes] = {
        "run_a/result.json": json.dumps(
            {"top_clips": [{"caption": "a", "export_path": "run_a/a.gif"}]}
        ).encode("utf-8"),
        "run_a/deep/clip_result.json": json.dumps({"top_clips": []}).encode("utf-8"),
        "run_b/x_result.json": json.dumps({"exported": 3}).encode("utf-8"),
        "run_a/preview.gif": b"",  # media: never copied, never read
        "run_b/movie.mp4": b"",  # media: never copied
        "run_b/notes.txt": b"not a result json",
        "run_b/summary.json": json.dumps({"misc": True}).encode("utf-8"),
    }
    for rel, data in files.items():
        target = exports_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return files


class TestIngestCorpusCli:
    def test_copies_only_result_jsons_and_writes_manifest(self, tmp_path):
        exports_root = tmp_path / "exports"
        _build_exports(exports_root)
        sprint_root = tmp_path / "sprint"
        sprint_root.mkdir()

        rc = main(["--sprint-root", str(sprint_root), "--exports-root", str(exports_root)])

        assert rc == 0
        corpus = sprint_root / "inputs" / "exports"
        copied = sorted(p.relative_to(corpus).as_posix() for p in corpus.rglob("*.json"))
        assert copied == [
            "run_a/deep/clip_result.json",
            "run_a/result.json",
            "run_b/x_result.json",
        ]
        # media and non-result files never enter the corpus
        assert not list(corpus.rglob("*.gif"))
        assert not list(corpus.rglob("*.mp4"))
        assert not list(corpus.rglob("*.txt"))
        assert not list(corpus.rglob("summary.json"))
        # 0-byte gifs stay 0-byte on the source side too (nothing written into them)
        assert (exports_root / "run_a/preview.gif").read_bytes() == b""

        manifest = json.loads((sprint_root / MANIFEST_NAME).read_text(encoding="utf-8"))
        assert manifest["schema"] == "gifagent.token_sprint.w1.exports_corpus_manifest.v1"
        assert manifest["counts"]["result_json_candidates"] == 3
        assert manifest["counts"]["copied"] == 3
        assert manifest["counts"]["media_files_seen"] == 2
        assert manifest["skipped_media"] == []
        by_rel = {f["copy_rel"]: f for f in manifest["files"] if f["copy_rel"]}
        entry = by_rel["inputs/exports/run_a/result.json"]
        assert entry["source_abs"] == (exports_root / "run_a/result.json").resolve().as_posix()
        assert entry["sha256"] == _sha256(corpus / "run_a/result.json")
        assert entry["as_of"]
        assert entry["status"] == "copied"

    def test_rerun_is_idempotent_and_never_overwrites(self, tmp_path):
        exports_root = tmp_path / "exports"
        _build_exports(exports_root)
        sprint_root = tmp_path / "sprint"
        sprint_root.mkdir()
        assert main(["--sprint-root", str(sprint_root), "--exports-root", str(exports_root)]) == 0
        first = json.loads((sprint_root / MANIFEST_NAME).read_text(encoding="utf-8"))
        target = sprint_root / "inputs" / "exports" / "run_a" / "result.json"
        before = target.read_bytes()

        assert main(["--sprint-root", str(sprint_root), "--exports-root", str(exports_root)]) == 0

        second = json.loads((sprint_root / MANIFEST_NAME).read_text(encoding="utf-8"))
        assert second["counts"]["copied"] == 0
        assert second["counts"]["already_present"] == 3
        assert target.read_bytes() == before
        statuses = {f["status"] for f in second["files"]}
        assert statuses == {"already_present"}

    def test_changed_source_after_copy_is_conflict_exit_2(self, tmp_path, capsys):
        exports_root = tmp_path / "exports"
        _build_exports(exports_root)
        sprint_root = tmp_path / "sprint"
        sprint_root.mkdir()
        assert main(["--sprint-root", str(sprint_root), "--exports-root", str(exports_root)]) == 0
        target = sprint_root / "inputs" / "exports" / "run_a" / "result.json"
        immutable_bytes = target.read_bytes()

        # source drifts after the first immutable copy
        (exports_root / "run_a/result.json").write_bytes(b'{"top_clips": ["changed"]}')

        rc = main(["--sprint-root", str(sprint_root), "--exports-root", str(exports_root)])

        assert rc == 2
        assert "conflict" in capsys.readouterr().err
        assert target.read_bytes() == immutable_bytes  # never overwritten

    def test_missing_exports_root_exit_2(self, tmp_path, capsys):
        sprint_root = tmp_path / "sprint"
        sprint_root.mkdir()

        rc = main(
            [
                "--sprint-root", str(sprint_root),
                "--exports-root", str(tmp_path / "nope"),
            ]
        )

        assert rc == 2
        assert "exports root" in capsys.readouterr().err

    def test_missing_sprint_root_exit_2(self, tmp_path):
        exports_root = tmp_path / "exports"
        _build_exports(exports_root)

        try:
            main(["--sprint-root", str(tmp_path / "missing"), "--exports-root", str(exports_root)])
        except SystemExit as exc:
            assert exc.code == 2
        else:
            raise AssertionError("expected SystemExit(2)")

    def test_does_not_touch_coordinator_inputs_manifest(self, tmp_path):
        exports_root = tmp_path / "exports"
        _build_exports(exports_root)
        sprint_root = tmp_path / "sprint"
        sprint_root.mkdir()
        frozen = sprint_root / "inputs_manifest.json"
        frozen.write_text('{"schema": "gifagent.token_sprint.inputs.v1"}', encoding="utf-8")

        assert main(["--sprint-root", str(sprint_root), "--exports-root", str(exports_root)]) == 0

        assert json.loads(frozen.read_text(encoding="utf-8")) == {
            "schema": "gifagent.token_sprint.inputs.v1"
        }
