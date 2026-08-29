"""Tests for the W3 relink inventory / dry-run proposal tool.

Covers (work order, 2026-08-29): metadata-only source inventory, basename
matching (matched / ambiguous / unmatched / unknown tiers), dry-run-only
behavior (no --apply), manifest SHA fail-closed, and zero mutation of inputs.
"""

from __future__ import annotations

import builtins
import json
import sqlite3
from pathlib import Path

import pytest

from scripts.token_sprint import relink_proposals as rp


# ---------------------------------------------------------------------------
# Fixtures: a fake relink source dir, fake findings JSON, fake snapshot DB
# ---------------------------------------------------------------------------


def make_source_dir(root: Path) -> Path:
    source = root / "source"
    (source / "SceneA").mkdir(parents=True)
    (source / "TitleA").mkdir(parents=True)
    (source / "DupName").mkdir(parents=True)
    (source / "stemonly").mkdir(parents=True)
    (source / "SceneA" / "unique_clip.gif").write_bytes(b"")
    (source / "DupName" / "twin.gif").write_bytes(b"")
    (source / "SceneA" / "twin.gif").write_bytes(b"")
    (source / "TitleA" / "renamed_after_download.mp4").write_bytes(b"")
    (source / "stemonly" / "reencoded.mp4").write_bytes(b"")
    (source / "SceneA" / "poster.jpg").write_bytes(b"")
    (source / "kept.gif").write_bytes(b"")
    return source


def make_findings(root: Path, data_root: Path) -> Path:
    findings = {
        "schema": "gifagent.token_sprint.w3_audit.v1",
        "data_root": str(data_root),
        "findings": [
            {
                "rule": "broken_artifact",
                "source": "library_snapshot",
                "record_type": "media",
                "record_ref": "media#1 (media_b52cdfad44ef)",
                "candidate_id": "media_b52cdfad44ef",
                "field": "file_path",
                "detail": ("path does not exist (stat only, absolute): "
                           "E:/data/originals\\0cZKnP5.gif"),
            },
        ],
    }
    path = root / "w3" / "w3-audit-findings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(findings), encoding="utf-8")
    return path


def make_snapshot(root: Path, data_root: Path) -> Path:
    db_path = root / "library_snapshot.db"
    connection = sqlite3.connect(db_path)
    try:
        connection.executescript(
            """
            CREATE TABLE candidate_gifs (
                candidate_id TEXT, artifact_path TEXT, preview_path TEXT);
            CREATE TABLE favorite_gifs (
                favorite_id TEXT, candidate_id TEXT, full_path TEXT,
                created_at TEXT);
            """
        )
        # matched + broken: basename exists in the source dir exactly once
        connection.execute(
            "INSERT INTO candidate_gifs VALUES (?, ?, ?)",
            ("cand_match", "data/exports/adaptive_test/SceneA/unique_clip.gif",
             "data/exports/adaptive_test/SceneA/unique_clip.gif"),
        )
        # ambiguous: same basename present in two source dirs
        connection.execute(
            "INSERT INTO candidate_gifs VALUES (?, ?, ?)",
            ("cand_ambig", "data/exports/adaptive_test/Other/twin.gif", None),
        )
        # unmatched: no basename, no parent dir, no stem hit
        connection.execute(
            "INSERT INTO candidate_gifs VALUES (?, ?, ?)",
            ("cand_gone", "data/exports/adaptive_test/Nowhere/gone.gif", None),
        )
        # unknown tier 1: parent dir present in source, basenames differ
        connection.execute(
            "INSERT INTO candidate_gifs VALUES (?, ?, ?)",
            ("cand_renamed",
             "data/exports/adaptive_test/TitleA/old_name.gif", None),
        )
        # unknown tier 2: stem matches with a different extension
        connection.execute(
            "INSERT INTO candidate_gifs VALUES (?, ?, ?)",
            ("cand_reenc",
             "data/exports/adaptive_test/Elsewhere/reencoded.gif", None),
        )
        # matched but healthy: old path still exists under data_root
        healthy = data_root / "exports" / "kept.gif"
        healthy.parent.mkdir(parents=True, exist_ok=True)
        healthy.write_bytes(b"")
        connection.execute(
            "INSERT INTO candidate_gifs VALUES (?, ?, ?)",
            ("cand_kept", "exports/kept.gif", None),
        )
        # favorite: matched, broken
        connection.execute(
            "INSERT INTO favorite_gifs VALUES (?, ?, ?, ?)",
            ("fav_match", "cand_x",
             str(data_root / "exports" / "adaptive_test" / "SceneA"
                 / "unique_clip.gif"), "2026-01-01T00:00:00"),
        )
        connection.commit()
    finally:
        connection.close()
    return db_path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_inventory_is_metadata_only_and_never_opens_media(tmp_path, monkeypatch):
    opened: list[str] = []

    real_open = builtins.open

    def guarded_open(file, *args, **kwargs):
        text = str(file)
        if text.lower().endswith(rp.MEDIA_SUFFIXES):
            raise AssertionError(f"media byte read attempted: {text}")
        opened.append(text)
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)
    source = make_source_dir(tmp_path)
    inventory = rp.inventory_source(source)
    assert inventory["by_extension"] == {
        "gif": 4, "jpg": 1, "mp4": 2, "webm": 0, "other": 0,
    }
    assert inventory["total_dirs"] == 5  # root + 4 subdirectories
    paths = {f["path"] for f in inventory["files"]}
    assert "SceneA/unique_clip.gif" in paths
    assert all(f["size"] == 0 for f in inventory["files"])
    assert all(isinstance(f["mtime"], int) for f in inventory["files"])
    # writing the inventory must not open media either
    rp.write_inventory_json(tmp_path / "inv.json", source, inventory, {}, "t")
    assert not any(str(p).lower().endswith(rp.MEDIA_SUFFIXES) for p in opened)


def test_match_tiers(tmp_path):
    source = make_source_dir(tmp_path)
    data_root = tmp_path / "data_root"
    findings = make_findings(tmp_path, data_root)
    snapshot = make_snapshot(tmp_path, data_root)
    inventory_out = tmp_path / "w3" / "inv.json"
    proposals_out = tmp_path / "w3" / "props.jsonl"
    code = rp.main([
        "--sprint-root", str(tmp_path),
        "--relink-source", str(source),
        "--findings", str(findings),
        "--snapshot", str(snapshot),
        "--inventory-out", str(inventory_out),
        "--proposals-out", str(proposals_out),
    ])
    assert code == 0
    summary = json.loads(inventory_out.read_text(encoding="utf-8"))["match_summary"]

    candidates = summary["candidate_gifs"]
    assert candidates["total"] == 7  # 6 rows x artifact_path (+ preview on one)
    assert candidates["matched"] == 3  # unique_clip (x2 cols) + kept.gif
    assert candidates["ambiguous"] == 1  # twin.gif
    assert candidates["unmatched"] == 1  # gone.gif
    assert candidates["unknown"] == 2  # renamed + reencoded

    favorites = summary["favorites"]
    assert favorites["total"] == 1 and favorites["matched"] == 1

    media = summary["media_e_volume"]
    assert media["total"] == 1 and media["unmatched"] == 1

    proposals = [json.loads(line) for line in
                 proposals_out.read_text(encoding="utf-8").splitlines()]
    by_row = {p["row_ref"]: p for p in proposals}
    assert by_row["candidate_gifs#1 (cand_match)"]["match"] == "basename"
    assert by_row["candidate_gifs#1 (cand_match)"]["old_path_status"] == "broken"
    assert by_row["candidate_gifs#1 (cand_match)"]["apply_recommendation"] is True
    assert by_row["candidate_gifs#6 (cand_kept)"]["old_path_status"] == "exists"
    assert by_row["candidate_gifs#6 (cand_kept)"]["apply_recommendation"] is False
    # ambiguous twin.gif must not produce a proposal
    assert "candidate_gifs#2 (cand_ambig)" not in by_row
    # media finding unmatched -> no proposal either
    assert not any(p["table"] == "media" for p in proposals)
    # new_path points into the source dir
    assert str(source) in by_row["candidate_gifs#1 (cand_match)"]["new_path"]


def test_ambiguous_detail_lists_candidates(tmp_path):
    source = make_source_dir(tmp_path)
    data_root = tmp_path / "data_root"
    findings = make_findings(tmp_path, data_root)
    snapshot = make_snapshot(tmp_path, data_root)
    inventory_out = tmp_path / "w3" / "inv.json"
    proposals_out = tmp_path / "w3" / "props.jsonl"
    rp.main([
        "--sprint-root", str(tmp_path),
        "--relink-source", str(source),
        "--findings", str(findings),
        "--snapshot", str(snapshot),
        "--inventory-out", str(inventory_out),
        "--proposals-out", str(proposals_out),
    ])
    data = json.loads(inventory_out.read_text(encoding="utf-8"))
    detail = data["ambiguous_unknown_detail"]
    ambiguous = [d for d in detail if d["status"] == "ambiguous"]
    assert len(ambiguous) == 1
    assert sorted(ambiguous[0]["candidates"]) == ["DupName/twin.gif", "SceneA/twin.gif"]
    unknown_reasons = {d["unknown_reason"] for d in detail
                       if d["status"] == "unknown"}
    assert unknown_reasons == {"parent_dir_present_basenames_differ",
                               "stem_match_extension_differs"}


def test_apply_flag_is_refused(tmp_path):
    source = make_source_dir(tmp_path)
    data_root = tmp_path / "data_root"
    findings = make_findings(tmp_path, data_root)
    code = rp.main([
        "--apply",
        "--sprint-root", str(tmp_path),
        "--relink-source", str(source),
        "--findings", str(findings),
        "--skip-snapshot",
    ])
    assert code == 2


def test_run_does_not_mutate_inputs(tmp_path):
    source = make_source_dir(tmp_path)
    data_root = tmp_path / "data_root"
    findings = make_findings(tmp_path, data_root)
    snapshot = make_snapshot(tmp_path, data_root)
    before = [(p, p.stat().st_size, p.stat().st_mtime_ns)
              for p in (findings, snapshot)]
    code = rp.main([
        "--sprint-root", str(tmp_path),
        "--relink-source", str(source),
        "--findings", str(findings),
        "--snapshot", str(snapshot),
        "--inventory-out", str(tmp_path / "w3" / "inv.json"),
        "--proposals-out", str(tmp_path / "w3" / "props.jsonl"),
    ])
    assert code == 0
    after = [(p, p.stat().st_size, p.stat().st_mtime_ns)
             for p in (findings, snapshot)]
    assert before == after


def test_manifest_sha_mismatch_fails_closed(tmp_path):
    source = make_source_dir(tmp_path)
    data_root = tmp_path / "data_root"
    findings = make_findings(tmp_path, data_root)
    manifest = {
        "artifacts": {
            "x": {"artifact_path": str(findings),
                  "artifact_sha256": "0" * 64},
        },
    }
    (tmp_path / "sprint_manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8")
    code = rp.main([
        "--sprint-root", str(tmp_path),
        "--relink-source", str(source),
        "--findings", str(findings),
        "--skip-snapshot",
    ])
    assert code == 2


def test_findings_and_snapshot_not_listed_in_manifest_still_runs(tmp_path):
    source = make_source_dir(tmp_path)
    data_root = tmp_path / "data_root"
    findings = make_findings(tmp_path, data_root)
    snapshot = make_snapshot(tmp_path, data_root)
    (tmp_path / "sprint_manifest.json").write_text(
        json.dumps({"artifacts": {}}), encoding="utf-8")
    inventory_out = tmp_path / "w3" / "inv.json"
    proposals_out = tmp_path / "w3" / "props.jsonl"
    code = rp.main([
        "--sprint-root", str(tmp_path),
        "--relink-source", str(source),
        "--findings", str(findings),
        "--snapshot", str(snapshot),
        "--inventory-out", str(inventory_out),
        "--proposals-out", str(proposals_out),
    ])
    assert code == 0
    data = json.loads(inventory_out.read_text(encoding="utf-8"))
    assert data["manifest_verification"] == {
        "findings": "not_listed", "snapshot_db": "not_listed"}
    assert proposals_out.exists()
