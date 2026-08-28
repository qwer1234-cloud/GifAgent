"""W3 audit_data tests: rule scanner over T-1 immutable input copies.

All fixtures are built in ``tmp_path`` (design global constraint 9); the shared
sprint root is never touched. Media bytes are never opened -- the media-open
guard test monkeypatches ``builtins.open`` and fails the test if any
.gif/.jpg/.mp4 path is opened.
"""

from __future__ import annotations

import builtins
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from scripts.token_sprint.audit_data import (
    SHARD_CAP,
    shard_filename,
    scan_text_for_mojibake,
    shard_sizes,
    write_hard_shards,
)
import scripts.token_sprint.audit_data as audit_module

MEDIA_SUFFIXES = (".gif", ".jpg", ".jpeg", ".png", ".mp4", ".webm", ".mov")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_manifest(sprint_root: Path, files: list[dict], **extra) -> Path:
    manifest = {
        "schema": "gifagent.token_sprint.inputs.v1",
        "as_of": "2026-08-28T00:00:00.000Z",
        "source_db_abs": "C:/prod/dist/GifAgentUI/data/library.db",
        "source_db_sha256": "0" * 64,
        "files": files,
        "skipped_media": [],
        **extra,
    }
    path = sprint_root / "inputs_manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return path


def _copied_entry(sprint_root: Path, key: str, name: str, payload: dict | str,
                  required: bool = False) -> dict:
    inputs = sprint_root / "inputs"
    inputs.mkdir(parents=True, exist_ok=True)
    copy_path = inputs / name
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    copy_path.write_text(text, encoding="utf-8")
    return {
        "key": key,
        "source_abs": f"C:/prod/data/{name}",
        "copy_rel": f"inputs/{name}",
        "sha256": _sha256(copy_path),
        "required": required,
        "status": "copied",
    }


def _absent_entry(key: str = "export_results") -> dict:
    return {
        "key": key,
        "source_abs": "C:/prod/data/exports/*.json",
        "copy_rel": None,
        "sha256": None,
        "required": False,
        "status": "absent",
    }


def _clean_adaptive_payload() -> dict:
    return {
        "video": "data/media/movie.mp4",
        "top_clips": [
            {
                "candidate_id": "cand_0001",
                "rank": 1,
                "timestamp": 10.0,
                "start_ts": 8.0,
                "end_ts": 12.0,
                "gif_worthiness": 0.9,
                "final_score": 0.8,
                "sex_act": 0.0,
                "export_status": "OK",
                "export_path": "data/exports/movie/clip_001.gif",
            }
        ],
        "gif_exports": [
            {
                "index": 1,
                "path": "data/exports/movie/clip_001.gif",
                "status": "OK",
                "start_ts": 8.0,
                "end_ts": 12.0,
                "quality_assessment": {"candidate_id": "cand_0001", "confidence": 0.7},
            }
        ],
    }


def _clean_checkpoint_payload() -> dict:
    return {
        "completed": {
            "data/media/movie.mp4": {
                "status": "ok",
                "elapsed_s": 10,
                "finished_at": "2026-08-24T22:30:00.000000",
            }
        },
        "started_at": "2026-08-24T21:48:34.567401",
        "updated_at": "2026-08-25T00:33:45.261025",
        "retryable": {},
        "last_run": {
            "status": "complete",
            "started_at": "2026-08-24T21:48:48.553254",
            "dir": "data/media",
        },
    }


def _seed_data_root(tmp_path: Path) -> Path:
    """Create the files/dirs referenced by the clean payloads (stat targets only)."""
    data_root = tmp_path / "data_root"
    (data_root / "data" / "exports" / "movie").mkdir(parents=True, exist_ok=True)
    (data_root / "data" / "media").mkdir(parents=True, exist_ok=True)
    (data_root / "data" / "media" / "movie.mp4").write_bytes(b"\x00\x00\x00\x18ftyp")
    return data_root


def _standard_manifest(sprint_root: Path, *, adaptive=None, checkpoint=None) -> list[dict]:
    files = [
        _copied_entry(sprint_root, "adaptive_test_result", "adaptive_test_result.json",
                      adaptive if adaptive is not None else _clean_adaptive_payload()),
        _copied_entry(sprint_root, "batch_checkpoint", "batch_checkpoint.json",
                      checkpoint if checkpoint is not None else _clean_checkpoint_payload()),
        _absent_entry(),
    ]
    _write_manifest(sprint_root, files)
    return files


def _run(tmp_path: Path, *extra_args: str) -> tuple[int, dict]:
    args = ["--sprint-root", str(tmp_path), *extra_args]
    code = audit_module.main(args)
    findings_path = tmp_path / "w3" / "w3-audit-findings.json"
    result = json.loads(findings_path.read_text(encoding="utf-8"))
    return code, result


# ---------------------------------------------------------------------------
# Manifest fail-closed gates
# ---------------------------------------------------------------------------


def test_missing_inputs_manifest_exits_2(tmp_path):
    with pytest.raises(audit_module.AuditError):
        audit_module.validate_inputs_manifest(tmp_path)


def test_tampered_copy_fails_closed(tmp_path):
    files = [_copied_entry(tmp_path, "adaptive_test_result", "adaptive_test_result.json",
                           _clean_adaptive_payload())]
    _write_manifest(tmp_path, files)
    # Tamper AFTER the manifest pinned the sha256.
    (tmp_path / "inputs" / "adaptive_test_result.json").write_text(
        json.dumps({"tampered": True}), encoding="utf-8")
    with pytest.raises(audit_module.AuditError, match="sha256 mismatch"):
        audit_module.validate_inputs_manifest(tmp_path)


def test_required_input_not_copied_fails_closed(tmp_path):
    _write_manifest(tmp_path, [
        {"key": "adaptive_test_result", "source_abs": "C:/prod/x.json",
         "copy_rel": None, "sha256": None, "required": True, "status": "absent"},
    ])
    with pytest.raises(audit_module.AuditError, match="required input"):
        audit_module.validate_inputs_manifest(tmp_path)


def test_absent_optional_input_is_reported_and_scan_continues(tmp_path):
    _standard_manifest(tmp_path)
    # The absent source points at a worktree-relative data/exports path that
    # must never be touched -- give the scanner a hostile cwd.
    code, result = _run(tmp_path)
    assert code == 0
    absent_keys = {entry["key"] for entry in result["missing_optional"]}
    assert absent_keys == {"export_results"}
    coverage_keys = {item["key"]: item["status"] for item in result["coverage"]}
    assert coverage_keys["export_results"] == "absent_fallback"
    assert coverage_keys["adaptive_test_result"] == "copied"


def test_copy_rel_paths_are_resolved_under_sprint_root_only(tmp_path):
    _standard_manifest(tmp_path)
    code, result = _run(tmp_path)
    assert code == 0
    # No relative data/exports lookups: relative artifact paths are unchecked
    # without --data-root and recorded as observations instead.
    unchecked = [o for o in result["observations"]
                 if o.get("kind") == "relative_path_unchecked"]
    assert unchecked, "relative artifact paths should be counted as unchecked"
    assert result["data_root"] is None


# ---------------------------------------------------------------------------
# Rule engine on adaptive-like JSON
# ---------------------------------------------------------------------------


def test_rules_fire_on_defective_adaptive_payload(tmp_path):
    data_root = _seed_data_root(tmp_path)
    payload = _clean_adaptive_payload()
    payload["top_clips"][0].pop("candidate_id")  # missing_field (agreed key)
    payload["top_clips"][0]["gif_worthiness"] = 1.5  # score_oob
    payload["top_clips"][0]["end_ts"] = 4.0  # end_ts < start_ts -> ts_contradiction
    payload["top_clips"][0]["export_path"] = "data/exports/movie/missing.gif"
    payload["gif_exports"][0]["path"] = "data/exports/movie/missing.gif"
    payload["top_clips"].append({
        "candidate_id": "cand_0002",
        "rank": 2,
        "timestamp": 20.0,
        "start_ts": 18.0,
        "end_ts": 22.0,
        "gif_worthiness": 0.5,
        "caption": "cafÃ© rÃ©sumÃ© garbled",  # strong mojibake signature
    })
    _standard_manifest(tmp_path, adaptive=payload)
    code, result = _run(tmp_path, "--data-root", str(data_root))
    assert code == 0
    rules = {}
    for finding in result["findings"]:
        rules.setdefault(finding["rule"], []).append(finding)
    assert any(f["field"] == "candidate_id" for f in rules.get("missing_field", []))
    assert any(f["field"] == "gif_worthiness" for f in rules.get("score_oob", []))
    broken = rules.get("broken_artifact", [])
    assert len(broken) == 2 and all("does not exist" in f["detail"] for f in broken)
    assert rules.get("ts_contradiction") and rules["ts_contradiction"][0]["field"] == \
        "end_ts<start_ts"
    mojibake = rules.get("mojibake", [])
    assert len(mojibake) == 1
    assert mojibake[0]["confidence"] == "high"
    assert mojibake[0]["hard_case"] is False
    assert result["rule_counts"]["score_oob"] == 1
    assert result["rule_counts"]["missing_field"] >= 1


def test_existing_export_paths_and_valid_scores_produce_no_findings(tmp_path):
    data_root = _seed_data_root(tmp_path)
    gif_path = data_root / "data" / "exports" / "movie" / "clip_001.gif"
    gif_path.write_bytes(b"GIF89a")  # media file exists but is never opened
    _standard_manifest(tmp_path)
    opened: list[str] = []
    real_open = builtins.open

    def guarded_open(file, *args, **kwargs):
        name = str(file)
        if name.lower().endswith(MEDIA_SUFFIXES):
            opened.append(name)
            raise AssertionError(f"scanner opened a media file: {name}")
        return real_open(file, *args, **kwargs)

    code, result = _run(tmp_path, "--data-root", str(data_root))
    assert code == 0
    assert opened == []
    assert result["findings"] == [], result["findings"]
    # The .gif file was stat-ed (exists) but never read: bytes untouched.
    assert gif_path.read_bytes() == b"GIF89a"


def test_never_opens_media_even_when_broken_artifact_is_media(tmp_path, monkeypatch):
    data_root = _seed_data_root(tmp_path)
    payload = _clean_adaptive_payload()
    payload["top_clips"][0]["export_path"] = "data/exports/movie/ghost.gif"
    payload["gif_exports"][0]["path"] = "data/exports/movie/ghost.gif"
    _standard_manifest(tmp_path, adaptive=payload)

    opened: list[str] = []
    real_open = builtins.open

    def guarded_open(file, *args, **kwargs):
        name = str(file)
        if name.lower().endswith(MEDIA_SUFFIXES):
            opened.append(name)
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)
    code, result = _run(tmp_path, "--data-root", str(data_root))
    monkeypatch.undo()
    assert code == 0
    assert opened == [], "no .gif/.jpg/.mp4 may ever be opened"
    broken = [f for f in result["findings"] if f["rule"] == "broken_artifact"]
    ghost = [f for f in broken if "ghost.gif" in f["detail"]]
    assert len(ghost) == 2  # top_clips + gif_exports reference the same ghost path
    assert broken == ghost  # nothing else in the seeded payload is broken


def test_mixed_tz_timestamp_pair_is_observation_not_contradiction(tmp_path):
    payload = _clean_checkpoint_payload()
    payload["updated_at"] = "2026-01-01T00:00:00+00:00"  # aware, before naive started_at
    payload["started_at"] = "2026-08-24T21:48:34.567401"
    _standard_manifest(tmp_path, checkpoint=payload)
    code, result = _run(tmp_path)
    assert code == 0
    assert not any(f["rule"] == "ts_contradiction" for f in result["findings"])
    mixed = [o for o in result["observations"]
             if o.get("kind") == "ts_pair_mixed_tz_uncomparable"]
    assert mixed


# ---------------------------------------------------------------------------
# Snapshot scanning
# ---------------------------------------------------------------------------


def _build_snapshot(db_path: Path, data_root: Path) -> None:
    connection = sqlite3.connect(db_path)
    try:
        connection.executescript(
            """
            CREATE TABLE candidate_gifs (
                candidate_id TEXT PRIMARY KEY,
                artifact_path TEXT,
                preview_path TEXT,
                start_sec REAL,
                end_sec REAL,
                final_score REAL,
                profile_score REAL,
                base_rag_similarity REAL,
                status TEXT,
                created_at TEXT,
                updated_at TEXT
            );
            CREATE TABLE frames (
                frame_id TEXT PRIMARY KEY,
                media_id TEXT,
                frame_path TEXT,
                timestamp REAL,
                vlm_status TEXT,
                vlm_error TEXT
            );
            CREATE TABLE candidate_search_fts (candidate_id, summary);
            """
        )
        ok_artifact = data_root / "data" / "exports" / "ok.gif"
        ok_artifact.parent.mkdir(parents=True, exist_ok=True)
        ok_artifact.write_bytes(b"GIF89a")
        connection.executemany(
            "INSERT INTO candidate_gifs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [
                # clean row
                ("cand_ok", ok_artifact.as_posix(), ok_artifact.as_posix(),
                 10.0, 20.0, 0.5, 0.6, 0.7, "liked",
                 "2026-06-28T02:10:50.911396", "2026-07-08T14:03:18.234545+00:00"),
                # end < start + score oob + broken artifact
                ("cand_bad", "data/exports/ghost.gif", None,
                 30.0, 20.0, 1.5, None, None, "liked",
                 "2026-06-28T02:10:50.911396", "2026-07-08T14:03:18.234545+00:00"),
                # null candidate_id + naive/aware mixed pair (observation only)
                (None, None, None, 1.0, 2.0, None, None, None, "liked",
                 "2026-06-28T02:10:50", "2026-06-01T00:00:00+00:00"),
            ],
        )
        connection.executemany(
            "INSERT INTO frames VALUES (?,?,?,?,?,?)",
            [
                ("f_ok", "m1", "data/frames/f_ok.jpg", 1.0, "done", None),
                ("f_nots", "m1", "data/frames/f_nots.jpg", None, "done", None),
                ("f_mojibake", "m2", "data/frames/f_moj.jpg", 2.0, "failed",
                 "cafÃ© decode error"),  # strong mojibake in vlm_error
            ],
        )
        connection.commit()
    finally:
        connection.close()


def _write_sprint_manifest_with_snapshot(sprint_root: Path, db_path: Path) -> None:
    manifest = {
        "schema": "gifagent.token_sprint.manifest.v1",
        "artifacts": {
            "w1-snapshot": {
                "artifact_path": db_path.as_posix(),
                "artifact_sha256": _sha256(db_path),
                "workstream": "w1",
            }
        },
    }
    (sprint_root / "sprint_manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8")


def test_snapshot_scan_rules_and_sha_gate(tmp_path):
    sprint_root = tmp_path / "sprint"
    sprint_root.mkdir()
    data_root = tmp_path / "data_root"
    db_path = sprint_root / "library_snapshot.db"
    _build_snapshot(db_path, data_root)
    _write_sprint_manifest_with_snapshot(sprint_root, db_path)
    _standard_manifest(sprint_root)

    code, result = _run(sprint_root, "--use-snapshot", "--data-root", str(data_root))
    assert code == 0
    rules = {}
    for finding in result["findings"]:
        if finding["source"] == "library_snapshot":
            rules.setdefault(finding["rule"], []).append(finding)
    missing = rules.get("missing_field", [])
    assert any(f["field"] == "candidate_id" and "cand" not in str(f["candidate_id"])
               for f in missing) or any(f["field"] == "candidate_id" for f in missing)
    assert any(f["field"] == "timestamp" for f in missing)  # frames.f_nots
    assert any(f["field"] == "final_score" for f in rules.get("score_oob", []))
    assert any(f["field"] == "end_sec<start_sec"
               for f in rules.get("ts_contradiction", []))
    broken = rules.get("broken_artifact", [])
    assert any("ghost.gif" in f["detail"] for f in broken)
    mojibake = rules.get("mojibake", [])
    assert len(mojibake) == 1 and mojibake[0]["field"] == "vlm_error"
    # mixed-tz row produced an observation, not a contradiction
    mixed = [o for o in result["observations"]
             if o.get("kind") == "ts_pair_mixed_tz_uncomparable"]
    assert mixed
    # FTS shadow table skipped explicitly
    skipped_tables = {t["table"] for o in result["observations"]
                      if o.get("kind") == "snapshot_tables_skipped"
                      for t in o["tables"]}
    assert "candidate_search_fts" in skipped_tables
    # all-null score column observation (candidate_gifs.profile_score nulls)
    null_obs = [o for o in result["observations"]
                if o.get("kind") == "score_column_nulls"]
    assert any("profile_score" in o["counts"] for o in null_obs)

    # Tamper with the snapshot -> sha gate must fail closed.
    connection = sqlite3.connect(db_path)
    connection.execute(
        "INSERT INTO frames VALUES ('f_extra','m9','data/frames/x.jpg',0,'done',NULL)")
    connection.commit()
    connection.close()
    with pytest.raises(audit_module.AuditError, match="snapshot sha256 mismatch"):
        audit_module.validate_snapshot(sprint_root)


def test_use_snapshot_without_canonical_artifact_fails_closed(tmp_path):
    sprint_root = tmp_path / "sprint"
    sprint_root.mkdir()
    db_path = sprint_root / "library_snapshot.db"
    db_path.write_bytes(b"SQLite format 3\x00")
    (sprint_root / "sprint_manifest.json").write_text(
        json.dumps({"schema": "gifagent.token_sprint.manifest.v1", "artifacts": {}}),
        encoding="utf-8")
    _standard_manifest(sprint_root)
    with pytest.raises(audit_module.AuditError, match="canonical W1 snapshot"):
        audit_module.validate_snapshot(sprint_root)


# ---------------------------------------------------------------------------
# Hard-case shard packaging
# ---------------------------------------------------------------------------


def test_shard_sizes_respects_cap_and_floor():
    assert shard_sizes(0) == []
    assert shard_sizes(60) == [60]
    assert shard_sizes(100) == [100]
    assert shard_sizes(140) == [70, 70]
    assert shard_sizes(101) == [51, 50]
    sizes = shard_sizes(250)
    assert len(sizes) == 3
    assert all(size <= SHARD_CAP for size in sizes)
    assert all(size >= 50 for size in sizes)
    assert sum(sizes) == 250


def test_write_hard_shards_single_shard_uses_plan_filename(tmp_path):
    findings = [
        {"rule": "mojibake", "source": "library_snapshot", "record_type": "frames",
         "record_ref": "frames#1", "candidate_id": None, "field": "vlm_error",
         "detail": "d", "confidence": "low", "hard_case": True, "excerpt": "cafÃ©"},
        {"rule": "mojibake", "source": "library_snapshot", "record_type": "frames",
         "record_ref": "frames#2", "candidate_id": None, "field": "vlm_error",
         "detail": "d", "confidence": "high", "hard_case": False, "excerpt": "x"},
    ]
    shards = write_hard_shards(findings, tmp_path)
    assert len(shards) == 1
    name = shards[0]["name"]
    assert name.startswith("w3-hard.") and name.endswith(".jsonl")
    assert name.count(".") == 2  # w3-hard.{attempt_id}.jsonl
    lines = (tmp_path / name).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1  # only the hard_case finding
    record = json.loads(lines[0])
    assert record["attempt_id"] == shards[0]["attempt_id"]
    assert record["record_ref"] == "frames#1"
    assert "model triage deferred" in record["purpose"]


def test_write_hard_shards_multi_shard_never_overwrites(tmp_path):
    findings = []
    for index in range(140):
        findings.append({
            "rule": "mojibake", "source": "s", "record_type": "t",
            "record_ref": f"r{index}", "candidate_id": None, "field": "f",
            "detail": "d", "confidence": "low", "hard_case": True,
            "excerpt": f"text-{index}",
        })
    shards = write_hard_shards(findings, tmp_path)
    assert len(shards) == 2
    line_counts = [
        len((tmp_path / shard["name"]).read_text(encoding="utf-8").splitlines())
        for shard in shards
    ]
    assert line_counts == [70, 70]
    assert all(shard["lines"] <= SHARD_CAP for shard in shards)
    # a second batch gets fresh attempt_ids and never reuses file names
    shards_again = write_hard_shards(findings, tmp_path)
    first_names = {shard["name"] for shard in shards}
    second_names = {shard["name"] for shard in shards_again}
    assert first_names.isdisjoint(second_names)


def test_shard_filename_numbering():
    assert shard_filename(1, 1, "abc") == "w3-hard.abc.jsonl"
    assert shard_filename(2, 3, "abc") == "w3-hard.002.abc.jsonl"


# ---------------------------------------------------------------------------
# Mojibake classifier unit checks
# ---------------------------------------------------------------------------


def test_mojibake_classifier_levels():
    assert scan_text_for_mojibake("plain english text")[0] == "clean"
    assert scan_text_for_mojibake("中文文本是干净的")[0] == "clean"
    assert scan_text_for_mojibake("cafÃ© rÃ©sumÃ©")[0] == "high"  # 0xC3 lead pair
    assert scan_text_for_mojibake("bad \x9d control char")[0] == "high"
    assert scan_text_for_mojibake("naïve café")[0] == "low"  # legit accents -> triage
    assert scan_text_for_mojibake("17м=BDRip")[0] == "clean"  # legit Cyrillic


# ---------------------------------------------------------------------------
# Publishing (.ready) and refusal to modify published artifacts
# ---------------------------------------------------------------------------


def test_publish_ready_and_refuse_republish(tmp_path):
    _standard_manifest(tmp_path)
    code, _ = _run(tmp_path, "--publish-ready")
    assert code == 0
    findings_path = tmp_path / "w3" / "w3-audit-findings.json"
    ready_path = findings_path.with_name(findings_path.name + ".ready")
    assert ready_path.is_file()
    ready = json.loads(ready_path.read_text(encoding="utf-8"))
    assert ready["producer"] == "w3-executor"
    assert ready["artifact_sha256"] == _sha256(findings_path)
    shard_count_before = len(list((tmp_path / "shards").glob("*.jsonl")))
    # republish must refuse (exit 2) instead of touching a published artifact,
    # and must not emit extra shard files on the refused run
    code_again, _ = _run(tmp_path, "--publish-ready")
    assert code_again == 2
    shard_count_after = len(list((tmp_path / "shards").glob("*.jsonl")))
    assert shard_count_after == shard_count_before
    assert _sha256(findings_path) == ready["artifact_sha256"]


def test_report_render_written(tmp_path):
    _standard_manifest(tmp_path)
    report_path = tmp_path / "report" / "w3-data-audit.md"
    code, _ = _run(tmp_path, "--report", str(report_path))
    assert code == 0
    text = report_path.read_text(encoding="utf-8")
    assert "# W3 历史数据体检报告" in text
    assert "absent 项 fallback 说明" in text
    assert "`missing_field`" in text


# ---------------------------------------------------------------------------
# Task 2: dry-run repair suggestions (print only, zero mutation)
# ---------------------------------------------------------------------------


from scripts.token_sprint.repair_data_dry_run import (  # noqa: E402
    parse_record_ref,
    suggest_patch,
)


def _findings_fixture() -> dict:
    return {
        "findings": [
            {
                "rule": "missing_field", "source": "adaptive_test_result",
                "record_type": "top_clips", "record_ref": "top_clips[0]",
                "candidate_id": None, "field": "candidate_id",
                "detail": "agreed key 'candidate_id' is missing or null",
                "confidence": "high", "hard_case": False, "excerpt": None,
            },
            {
                "rule": "score_oob", "source": "library_snapshot",
                "record_type": "candidate_gifs", "record_ref": "candidate_gifs#17 (cand_x)",
                "candidate_id": "cand_x", "field": "final_score",
                "detail": "value 1.5 outside [0,1]", "confidence": "high",
                "hard_case": False, "excerpt": None,
            },
            {
                "rule": "broken_artifact", "source": "library_snapshot",
                "record_type": "candidate_gifs", "record_ref": "candidate_gifs#21 (cand_y)",
                "candidate_id": "cand_y", "field": "artifact_path",
                "detail": "path does not exist (stat only, relative_to_data_root): "
                          "data\\exports\\ghost.gif",
                "confidence": "high", "hard_case": False, "excerpt": None,
            },
            {
                "rule": "mojibake", "source": "library_snapshot",
                "record_type": "annotations", "record_ref": "annotations#5 (ann_1)",
                "candidate_id": None, "field": "summary",
                "detail": "mojibake signature (low confidence), 2 suspicious chars",
                "confidence": "low", "hard_case": True,
                "excerpt": "cafÃ© texture summary",
            },
            {
                "rule": "ts_contradiction", "source": "batch_checkpoint",
                "record_type": "checkpoint", "record_ref": "$",
                "candidate_id": None, "field": "updated_at<started_at",
                "detail": "updated_at before started_at", "confidence": "high",
                "hard_case": False, "excerpt": None,
            },
        ]
    }


def test_repair_dry_run_prints_without_mutating_inputs(tmp_path):
    sprint_root = tmp_path / "sprint"
    (sprint_root / "w3").mkdir(parents=True)
    findings_path = sprint_root / "w3" / "w3-audit-findings.json"
    findings_path.write_text(json.dumps(_findings_fixture(), ensure_ascii=False),
                             encoding="utf-8")
    input_copy = sprint_root / "inputs" / "adaptive_test_result.json"
    input_copy.parent.mkdir(parents=True)
    input_copy.write_text("{}", encoding="utf-8")

    def fingerprint(path: Path):
        stat = path.stat()
        return (path.name, stat.st_size, stat.st_mtime_ns)

    before = [fingerprint(findings_path), fingerprint(input_copy)]
    from scripts.token_sprint.repair_data_dry_run import main as repair_main
    import io
    import contextlib
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        rc = repair_main([
            "--sprint-root", str(sprint_root),
        ])
    output = buffer.getvalue()
    after = [fingerprint(findings_path), fingerprint(input_copy)]
    assert rc == 0
    assert before == after, "dry run must not touch any input file"
    assert "DRY RUN ONLY" in output
    assert "UPDATE candidate_gifs SET final_score" in output
    assert "rowid = 21" in output
    assert '"path": "/top_clips/0/candidate_id"' in output
    assert "No file, database, or manifest was modified (dry run)." in output


def test_repair_suggests_mojibake_round_trip_and_refuses_apply(tmp_path):
    finding = next(f for f in _findings_fixture()["findings"]
                   if f["rule"] == "mojibake")
    suggestion = suggest_patch(finding)
    assert "café" in suggestion  # round-trip preview shows the repaired text
    finding_bad = dict(finding, excerpt="ÃÂ½ unrepairable \x9d")
    assert "Manual review" in suggest_patch(finding_bad)

    sprint_root = tmp_path / "sprint"
    (sprint_root / "w3").mkdir(parents=True)
    (sprint_root / "w3" / "w3-audit-findings.json").write_text(
        json.dumps(_findings_fixture(), ensure_ascii=False), encoding="utf-8")
    from scripts.token_sprint.repair_data_dry_run import main as repair_main
    import contextlib
    import io
    buffer, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(err):
        rc = repair_main(["--sprint-root", str(sprint_root), "--apply"])
    assert rc == 2
    assert "--apply" in err.getvalue()


def test_parse_record_ref():
    assert parse_record_ref("candidate_gifs#21 (cand_y)") == ("candidate_gifs", 21)
    assert parse_record_ref("top_clips[3]") == (None, None)
    assert parse_record_ref("") == (None, None)
