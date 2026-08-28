"""Protocol tests for the token-sprint coordinator control plane.

Covers (design v4.1 T-1 gate): missing/invalid ``--sprint-root``, two-process
nonce preflight, four-way hash-chain rejection, duplicate-receipt idempotence,
crash replay, and Windows-safe receipt filenames.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts.token_sprint.coordinator.common import (
    atomic_write_text,
    receipt_filename,
    resolve_sprint_root,
    sha256_bytes,
    sha256_file,
    windows_safe_ts,
)
from scripts.token_sprint.coordinator.protocol import (
    HashChainError,
    verify_hash_chain,
    write_done,
    write_ready,
    write_receipt,
)
from scripts.token_sprint.coordinator import reconcile_receipts as rr

REPO_ROOT = Path(__file__).resolve().parents[2]
PREFLIGHT_SCRIPT = REPO_ROOT / "scripts" / "token_sprint" / "coordinator" / "preflight.py"
RECONCILE_SCRIPT = REPO_ROOT / "scripts" / "token_sprint" / "coordinator" / "reconcile_receipts.py"


# ---------------------------------------------------------------------------
# sprint-root resolution
# ---------------------------------------------------------------------------


def test_missing_sprint_root_exits(monkeypatch):
    monkeypatch.delenv("GIFAGENT_SPRINT_ROOT", raising=False)
    with pytest.raises(SystemExit):
        resolve_sprint_root(None)


def test_empty_cli_sprint_root_falls_back_to_env(tmp_path, monkeypatch):
    monkeypatch.setenv("GIFAGENT_SPRINT_ROOT", str(tmp_path))
    assert resolve_sprint_root("") == tmp_path.resolve()


def test_nonexistent_sprint_root_exits(tmp_path):
    with pytest.raises(SystemExit):
        resolve_sprint_root(str(tmp_path / "nope"))


def test_sprint_root_file_not_dir_exits(tmp_path):
    not_a_dir = tmp_path / "plain.txt"
    not_a_dir.write_text("x", encoding="utf-8")
    with pytest.raises(SystemExit):
        resolve_sprint_root(str(not_a_dir))


def test_cli_wins_over_env(tmp_path, monkeypatch):
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setenv("GIFAGENT_SPRINT_ROOT", str(other))
    assert resolve_sprint_root(str(tmp_path)) == tmp_path.resolve()


def test_env_fallback_when_cli_omitted(tmp_path, monkeypatch):
    monkeypatch.setenv("GIFAGENT_SPRINT_ROOT", str(tmp_path))
    assert resolve_sprint_root(None) == tmp_path.resolve()


def test_windows_drive_path_resolves(tmp_path):
    # cmd-style absolute path with backslashes must be accepted as-is.
    resolved = resolve_sprint_root(str(tmp_path))
    assert resolved.is_dir()
    assert str(resolved).startswith(str(tmp_path.drive))


# ---------------------------------------------------------------------------
# atomic write
# ---------------------------------------------------------------------------


def test_atomic_write_same_directory(tmp_path):
    target = tmp_path / "manifest.json"
    atomic_write_text(target, '{"ok": true}')
    leftovers = [p.name for p in tmp_path.iterdir() if ".tmp." in p.name]
    assert leftovers == []
    assert target.read_text(encoding="utf-8") == '{"ok": true}'


def test_atomic_write_overwrites_existing(tmp_path):
    target = tmp_path / "manifest.json"
    target.write_text("old", encoding="utf-8")
    atomic_write_text(target, "new")
    assert target.read_text(encoding="utf-8") == "new"


def test_atomic_write_creates_missing_parent(tmp_path):
    target = tmp_path / "nested" / "dir" / "file.json"
    atomic_write_text(target, "{}")
    assert target.read_text(encoding="utf-8") == "{}"


# ---------------------------------------------------------------------------
# hashing
# ---------------------------------------------------------------------------


def test_sha256_bytes_matches_known_vector():
    assert (
        sha256_bytes(b"abc")
        == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )


def test_sha256_file_matches_bytes_hash(tmp_path):
    payload = tmp_path / "blob.bin"
    data = b"token-sprint" * 100
    payload.write_bytes(data)
    assert sha256_file(payload) == sha256_bytes(data)


# ---------------------------------------------------------------------------
# timestamps + receipt filenames
# ---------------------------------------------------------------------------


def test_windows_safe_ts_format_and_utc():
    dt = datetime(2026, 8, 28, 9, 30, 5, 123456, tzinfo=timezone.utc)
    assert windows_safe_ts(dt) == "20260828T093005123Z"


def test_windows_safe_ts_has_no_colon():
    assert ":" not in windows_safe_ts()


def test_receipt_filename_is_windows_safe():
    name = receipt_filename("w1")
    assert ":" not in name
    assert name.endswith(".json")
    assert name.startswith("w1_")


def test_receipt_filename_with_explicit_parts():
    dt = datetime(2026, 8, 28, 0, 0, 0, tzinfo=timezone.utc)
    name = receipt_filename("w8-shadow", dt=dt, receipt_id="deadbeef")
    assert name == "w8-shadow_20260828T000000000Z_deadbeef.json"


def test_receipt_filename_rejects_bad_workstream():
    with pytest.raises(ValueError):
        receipt_filename("W1_Bad:Name")


# ---------------------------------------------------------------------------
# preflight: two independent processes
# ---------------------------------------------------------------------------


def test_preflight_two_processes(tmp_path):
    write = subprocess.run(
        [sys.executable, str(PREFLIGHT_SCRIPT), "--sprint-root", str(tmp_path), "--write-nonce"],
        check=True,
        capture_output=True,
        text=True,
    )
    nonce_path = tmp_path / "_preflight_nonce.txt"
    assert nonce_path.is_file()
    assert str(nonce_path) in write.stdout
    verify = subprocess.run(
        [sys.executable, str(PREFLIGHT_SCRIPT), "--sprint-root", str(tmp_path), "--verify-nonce"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "preflight ok" in verify.stdout


def test_preflight_write_then_verify_again_is_stable(tmp_path):
    for _ in range(2):
        subprocess.run(
            [sys.executable, str(PREFLIGHT_SCRIPT), "--sprint-root", str(tmp_path), "--write-nonce"],
            check=True,
            capture_output=True,
            text=True,
        )
    verify = subprocess.run(
        [sys.executable, str(PREFLIGHT_SCRIPT), "--sprint-root", str(tmp_path), "--verify-nonce"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "preflight ok" in verify.stdout


def test_preflight_rejects_missing_sprint_root():
    proc = subprocess.run(
        [sys.executable, str(PREFLIGHT_SCRIPT), "--write-nonce"],
        capture_output=True,
        text=True,
        env={**os.environ, "GIFAGENT_SPRINT_ROOT": ""},
    )
    assert proc.returncode != 0
    assert "error:" in proc.stderr


def test_preflight_rejects_nonexistent_sprint_root(tmp_path):
    proc = subprocess.run(
        [
            sys.executable,
            str(PREFLIGHT_SCRIPT),
            "--sprint-root",
            str(tmp_path / "nope"),
            "--verify-nonce",
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0


def test_preflight_verify_without_nonce_file_exits_nonzero(tmp_path):
    proc = subprocess.run(
        [sys.executable, str(PREFLIGHT_SCRIPT), "--sprint-root", str(tmp_path), "--verify-nonce"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 2


def test_preflight_verify_rejects_malformed_nonce(tmp_path):
    (tmp_path / "_preflight_nonce.txt").write_text("not-hex-at-all\n", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(PREFLIGHT_SCRIPT), "--sprint-root", str(tmp_path), "--verify-nonce"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 2


def test_preflight_flags_are_mutually_exclusive(tmp_path):
    proc = subprocess.run(
        [
            sys.executable,
            str(PREFLIGHT_SCRIPT),
            "--sprint-root",
            str(tmp_path),
            "--write-nonce",
            "--verify-nonce",
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 2


def test_preflight_requires_a_mode(tmp_path):
    proc = subprocess.run(
        [sys.executable, str(PREFLIGHT_SCRIPT), "--sprint-root", str(tmp_path)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 2


# ---------------------------------------------------------------------------
# protocol: ready / done / receipt hash chain
# ---------------------------------------------------------------------------


def _make_chain(tmp_path, workstream: str = "w1"):
    artifact = tmp_path / "shard-00.p3.v1.uuid.jsonl"
    artifact.write_text('{"candidate_id":"c1"}\n', encoding="utf-8")
    ready = write_ready(artifact, producer="exec-1", qc_summary={"schema_ok": 1.0})
    done = write_done(artifact, ready, reviewer="rev-1", qc_evidence={"sampled": 3})
    receipt = write_receipt(
        sprint_root=tmp_path,
        workstream=workstream,
        producer="exec-1",
        reviewer="rev-1",
        artifact=artifact,
        ready=ready,
        done=done,
    )
    return artifact, ready, done, receipt


def test_hash_chain_happy_path(tmp_path):
    artifact = tmp_path / "shard-00.p3.v1.uuid.jsonl"
    artifact.write_text('{"candidate_id":"c1"}\n', encoding="utf-8")
    ready = write_ready(artifact, producer="exec-1", qc_summary={"schema_ok": 1.0})
    done = write_done(artifact, ready, reviewer="rev-1", qc_evidence={"sampled": 3})
    receipt = write_receipt(
        sprint_root=tmp_path,
        workstream="w1",
        producer="exec-1",
        reviewer="rev-1",
        artifact=artifact,
        ready=ready,
        done=done,
    )
    verify_hash_chain(artifact, ready, done, receipt)
    assert ready.name == artifact.name + ".ready"
    assert done.name == artifact.name + ".done"
    assert receipt.parent == tmp_path / "receipts"
    assert ":" not in receipt.name
    assert receipt.name.endswith(".json")
    assert receipt.name.startswith("w1_")


def test_receipt_contains_required_fields_with_posix_paths(tmp_path):
    _artifact, ready, done, receipt = _make_chain(tmp_path, workstream="w8-shadow")
    data = json.loads(receipt.read_text(encoding="utf-8"))
    for key in (
        "receipt_id",
        "artifact_path",
        "artifact_sha256",
        "ready_path",
        "ready_sha256",
        "done_path",
        "done_sha256",
        "producer",
        "reviewer",
        "workstream",
        "ts",
    ):
        assert key in data, key
    for key in ("artifact_path", "ready_path", "done_path"):
        assert "\\" not in data[key], key
        assert Path(data[key]).is_absolute(), key
    assert data["workstream"] == "w8-shadow"
    assert ready.is_file() and done.is_file()


def test_ready_sidecar_records_posix_absolute_artifact_path(tmp_path):
    artifact = tmp_path / "a.jsonl"
    artifact.write_text("row\n", encoding="utf-8")
    ready = write_ready(artifact, producer="e", qc_summary={})
    data = json.loads(ready.read_text(encoding="utf-8"))
    assert data["artifact_path"] == artifact.resolve().as_posix()
    assert data["producer"] == "e"
    assert set(data) == {"artifact_path", "artifact_sha256", "qc_summary", "producer", "ts"}


@pytest.mark.parametrize("which", ["artifact", "ready", "done", "receipt"])
def test_hash_chain_rejects_mismatch(tmp_path, which):
    artifact, ready, done, receipt = _make_chain(tmp_path)
    if which == "artifact":
        artifact.write_text(
            artifact.read_text(encoding="utf-8") + '{"candidate_id":"c2"}\n',
            encoding="utf-8",
        )
    elif which == "ready":
        data = json.loads(ready.read_text(encoding="utf-8"))
        data["qc_summary"]["schema_ok"] = 0.5
        ready.write_text(json.dumps(data), encoding="utf-8")
    elif which == "done":
        data = json.loads(done.read_text(encoding="utf-8"))
        data["qc_evidence"]["sampled"] = 99
        done.write_text(json.dumps(data), encoding="utf-8")
    else:
        data = json.loads(receipt.read_text(encoding="utf-8"))
        data["artifact_sha256"] = "0" * 64
        receipt.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(HashChainError):
        verify_hash_chain(artifact, ready, done, receipt)


def test_hash_chain_rejects_receipt_producer_drift(tmp_path):
    artifact, ready, done, receipt = _make_chain(tmp_path)
    data = json.loads(receipt.read_text(encoding="utf-8"))
    data["producer"] = "someone-else"
    receipt.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(HashChainError):
        verify_hash_chain(artifact, ready, done, receipt)


def test_ready_then_mutate_artifact_is_detected(tmp_path):
    artifact = tmp_path / "a.jsonl"
    artifact.write_text("a\n", encoding="utf-8")
    ready = write_ready(artifact, producer="e", qc_summary={})
    artifact.write_text("b\n", encoding="utf-8")  # forbidden in production; test must catch
    with pytest.raises(HashChainError):
        write_done(artifact, ready, reviewer="r", qc_evidence={})


def test_write_done_rejects_wrong_ready(tmp_path):
    artifact = tmp_path / "a.jsonl"
    artifact.write_text("a\n", encoding="utf-8")
    other = tmp_path / "b.jsonl"
    other.write_text("b\n", encoding="utf-8")
    ready = write_ready(other, producer="e", qc_summary={})
    with pytest.raises(HashChainError):
        write_done(artifact, ready, reviewer="r", qc_evidence={})


# ---------------------------------------------------------------------------
# reconcile_receipts: idempotent manifest rewrite + crash replay
# ---------------------------------------------------------------------------


def _reconcile(sprint_root, *extra):
    return subprocess.run(
        [
            sys.executable,
            str(RECONCILE_SCRIPT),
            "--sprint-root",
            str(sprint_root),
            "--once",
            *extra,
        ],
        capture_output=True,
        text=True,
    )


def _read_manifest(sprint_root):
    return json.loads((sprint_root / "sprint_manifest.json").read_text(encoding="utf-8"))


def test_reconcile_happy_path_archives_receipt(tmp_path):
    _artifact, _ready, _done, receipt = _make_chain(tmp_path, workstream="w4")
    proc = _reconcile(tmp_path)
    assert proc.returncode == 0, proc.stderr
    manifest = _read_manifest(tmp_path)
    assert len(manifest["artifacts"]) == 1
    entry = next(iter(manifest["artifacts"].values()))
    assert entry["workstream"] == "w4"
    assert entry["artifact_sha256"] == sha256_file(_artifact)
    assert not (tmp_path / "receipts" / receipt.name).exists()
    assert (tmp_path / "receipts" / "processed" / receipt.name).is_file()


def test_reconcile_idempotent_duplicate_receipt(tmp_path):
    _artifact, _ready, _done, receipt = _make_chain(tmp_path, workstream="w2")
    assert _reconcile(tmp_path).returncode == 0
    manifest1 = _read_manifest(tmp_path)
    revision1 = manifest1["revision"]

    # Simulate the crash-replay case: the same receipt shows up again.
    shutil.copy2(
        tmp_path / "receipts" / "processed" / receipt.name,
        tmp_path / "receipts" / receipt.name,
    )
    proc2 = _reconcile(tmp_path)
    assert proc2.returncode == 0, proc2.stderr
    manifest2 = _read_manifest(tmp_path)
    assert manifest2["artifacts"] == manifest1["artifacts"]
    assert manifest2["revision"] == revision1  # pure replay: no revision bump
    assert not (tmp_path / "receipts" / receipt.name).exists()


def test_reconcile_crash_after_manifest_before_move(tmp_path, monkeypatch):
    """Patch os.replace to fail on the processed/ move once; rerun --once; artifacts equal."""
    _artifact, _ready, _done, receipt = _make_chain(tmp_path, workstream="w5")
    real_replace = os.replace
    state = {"failed_once": False}

    def flaky_replace(src, dst, *args, **kwargs):
        if "processed" in str(dst) and not state["failed_once"]:
            state["failed_once"] = True
            raise OSError("simulated crash before archive move")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", flaky_replace)
    rc1 = rr.main(["--sprint-root", str(tmp_path), "--once"])
    monkeypatch.undo()

    assert rc1 == 1  # manifest written, archive move failed
    assert receipt.is_file()
    manifest1 = _read_manifest(tmp_path)
    assert len(manifest1["artifacts"]) == 1

    rc2 = rr.main(["--sprint-root", str(tmp_path), "--once"])
    assert rc2 == 0
    manifest2 = _read_manifest(tmp_path)
    assert manifest2["artifacts"] == manifest1["artifacts"]
    assert manifest2["revision"] == manifest1["revision"]  # replay did not bump
    assert (tmp_path / "receipts" / "processed" / receipt.name).is_file()


@pytest.mark.parametrize("which", ["artifact", "ready", "done", "receipt"])
def test_reconcile_rejects_any_hash_mismatch(tmp_path, which):
    artifact, ready, done, receipt = _make_chain(tmp_path)
    if which == "artifact":
        artifact.write_text(artifact.read_text(encoding="utf-8") + "tamper\n", encoding="utf-8")
    elif which == "ready":
        data = json.loads(ready.read_text(encoding="utf-8"))
        data["qc_summary"]["schema_ok"] = 0.0
        ready.write_text(json.dumps(data), encoding="utf-8")
    elif which == "done":
        data = json.loads(done.read_text(encoding="utf-8"))
        data["review_verdict"] = "fail"
        done.write_text(json.dumps(data), encoding="utf-8")
    else:
        data = json.loads(receipt.read_text(encoding="utf-8"))
        data["producer"] = "imposter"
        receipt.write_text(json.dumps(data), encoding="utf-8")

    proc = _reconcile(tmp_path)
    assert proc.returncode == 1
    assert receipt.is_file()  # rejected receipt stays in place, not archived
    assert not (tmp_path / "receipts" / "processed" / receipt.name).exists()
    manifest = _read_manifest(tmp_path)
    assert manifest["artifacts"] == {}


def test_reconcile_rejects_conflicting_duplicate_receipt_id(tmp_path):
    # First receipt records chain A under receipt id A.
    artifact_a, _r, _d, receipt_a = _make_chain(tmp_path, workstream="w1")
    assert _reconcile(tmp_path).returncode == 0
    manifest1 = _read_manifest(tmp_path)
    id_a = next(iter(manifest1["artifacts"]))
    revision1 = manifest1["revision"]

    # Second chain B re-uses receipt id A with different content.
    artifact_b = tmp_path / "shard-09.p3.v1.other.jsonl"
    artifact_b.write_text('{"candidate_id":"c9"}\n', encoding="utf-8")
    ready_b = write_ready(artifact_b, producer="exec-2", qc_summary={"ok": 1})
    done_b = write_done(artifact_b, ready_b, reviewer="rev-2", qc_evidence={})
    receipt_b = write_receipt(
        sprint_root=tmp_path,
        workstream="w1",
        producer="exec-2",
        reviewer="rev-2",
        artifact=artifact_b,
        ready=ready_b,
        done=done_b,
    )
    receipt_b_data = json.loads(receipt_b.read_text(encoding="utf-8"))
    receipt_b_data["receipt_id"] = id_a
    stem_parts = receipt_b.stem.rsplit("_", 2)
    forged_name = f"{stem_parts[0]}_{stem_parts[1]}_{id_a}.json"
    forged = receipt_b.parent / forged_name
    atomic_write_text(forged, json.dumps(receipt_b_data, indent=2, sort_keys=True) + "\n")
    receipt_b.unlink()

    proc = _reconcile(tmp_path)
    assert proc.returncode == 1
    assert forged.is_file()  # conflicting receipt rejected, left in place
    manifest2 = _read_manifest(tmp_path)
    assert manifest2["artifacts"][id_a]["artifact_sha256"] == sha256_file(artifact_a)
    assert len(manifest2["artifacts"]) == 1
    assert manifest2["revision"] == revision1


def test_manifest_does_not_record_own_sha(tmp_path):
    _make_chain(tmp_path)
    assert _reconcile(tmp_path).returncode == 0
    data = _read_manifest(tmp_path)
    assert "sha256" not in data
    assert data["schema"] == "gifagent.token_sprint.manifest.v1"


def test_reconcile_updates_control_plane_sha(tmp_path):
    _make_chain(tmp_path)
    inputs_manifest = tmp_path / "inputs_manifest.json"
    inputs_manifest.write_text('{"schema": "gifagent.token_sprint.inputs.v1"}', encoding="utf-8")
    usage_log = tmp_path / "usage-log.md"
    usage_log.write_text("# usage\n", encoding="utf-8")
    proc = _reconcile(tmp_path, "--usage-log", str(usage_log))
    assert proc.returncode == 0, proc.stderr
    manifest = _read_manifest(tmp_path)
    assert manifest["control_plane"]["inputs_manifest.json"]["sha256"] == sha256_file(inputs_manifest)
    assert manifest["control_plane"]["usage-log.md"]["sha256"] == sha256_file(usage_log)


# ---------------------------------------------------------------------------
# prepare_inputs: absolute source pinning + immutable copies
# ---------------------------------------------------------------------------


PREPARE_SCRIPT = REPO_ROOT / "scripts" / "token_sprint" / "coordinator" / "prepare_inputs.py"


def _run_prepare(sprint_root, *extra):
    return subprocess.run(
        [sys.executable, str(PREPARE_SCRIPT), "--sprint-root", str(sprint_root), *extra],
        capture_output=True,
        text=True,
    )


def test_prepare_inputs_resolves_strict_and_copies(tmp_path):
    src_db = tmp_path / "prod" / "library.db"
    src_db.parent.mkdir()
    src_db.write_bytes(b"sqlite-fake")
    adaptive = tmp_path / "adaptive_test_result.json"
    adaptive.write_text('{"ok": true}', encoding="utf-8")
    exports_dir = tmp_path / "exports"
    exports_dir.mkdir()
    (exports_dir / "result_a.json").write_text('{"a": 1}', encoding="utf-8")
    (exports_dir / "frame.gif").write_bytes(b"GIF89a-fake-but-never-read")
    sprint = tmp_path / "sprint"
    sprint.mkdir()

    proc = _run_prepare(
        sprint,
        "--source-db", str(src_db),
        "--input-adaptive", str(adaptive),
        "--input-checkpoint", str(tmp_path / "nope.json"),
        "--input-export-glob", str(exports_dir / "*"),
    )
    assert proc.returncode == 0, proc.stderr

    manifest = json.loads((sprint / "inputs_manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema"] == "gifagent.token_sprint.inputs.v1"
    assert manifest["source_db_abs"] == src_db.resolve().as_posix()
    assert manifest["source_db_sha256"] == sha256_file(src_db)
    assert Path(manifest["source_db_abs"]).is_absolute()
    assert "\\" not in manifest["source_db_abs"]

    by_key: dict[str, list[dict]] = {}
    for entry in manifest["files"]:
        by_key.setdefault(entry["key"], []).append(entry)

    adaptive_entries = by_key["adaptive_test_result"]
    assert adaptive_entries[0]["status"] == "copied"
    assert adaptive_entries[0]["copy_rel"] == "inputs/adaptive_test_result.json"
    copied_adaptive = sprint / "inputs" / "adaptive_test_result.json"
    assert copied_adaptive.is_file()
    assert adaptive_entries[0]["sha256"] == sha256_file(copied_adaptive)

    checkpoint_entries = by_key["batch_checkpoint"]
    assert checkpoint_entries[0]["status"] == "absent"
    assert checkpoint_entries[0]["sha256"] is None
    assert checkpoint_entries[0]["copy_rel"] is None

    export_entries = by_key["export_results"]
    assert [e["status"] for e in export_entries] == ["copied"]
    assert (sprint / "inputs" / "result_a.json").is_file()

    skipped = {s["source_abs"] for s in manifest["skipped_media"]}
    assert (exports_dir / "frame.gif").resolve().as_posix() in skipped
    assert not (sprint / "inputs" / "frame.gif").exists()

    # The production db is pinned by path+hash, never copied into inputs/.
    assert not (sprint / "inputs" / "library.db").exists()


def test_prepare_inputs_missing_source_db_exits_and_writes_no_manifest(tmp_path):
    sprint = tmp_path / "sprint"
    sprint.mkdir()
    proc = _run_prepare(sprint, "--source-db", str(tmp_path / "missing.db"))
    assert proc.returncode == 2
    assert "error:" in proc.stderr
    assert not (sprint / "inputs_manifest.json").exists()


def test_prepare_inputs_rejects_media_source_db(tmp_path):
    sprint = tmp_path / "sprint"
    sprint.mkdir()
    media_db = tmp_path / "library.gif"
    media_db.write_bytes(b"GIF89a")
    proc = _run_prepare(sprint, "--source-db", str(media_db))
    assert proc.returncode == 2
    assert not (sprint / "inputs_manifest.json").exists()


def test_prepare_inputs_rejects_basename_collision(tmp_path):
    src_db = tmp_path / "library.db"
    src_db.write_bytes(b"sqlite-fake")
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()
    (dir_a / "result.json").write_text('{"which": "a"}', encoding="utf-8")
    (dir_b / "result.json").write_text('{"which": "b"}', encoding="utf-8")
    sprint = tmp_path / "sprint"
    sprint.mkdir()
    proc = _run_prepare(
        sprint,
        "--source-db", str(src_db),
        "--input-adaptive", str(dir_a / "result.json"),
        "--input-checkpoint", str(dir_b / "result.json"),
        "--input-export-glob", str(tmp_path / "no-where" / "*.json"),
    )
    assert proc.returncode == 2
    assert not (sprint / "inputs_manifest.json").exists()
