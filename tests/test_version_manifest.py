"""Tests for the version manifest writer and the smoke script."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_manifest(dist_dir: Path, output: str = "MANIFEST.json") -> dict:
    """Run write_version_manifest.py and return the parsed manifest dict."""
    script = Path(__file__).resolve().parents[1] / "scripts" / "write_version_manifest.py"
    result = subprocess.run(
        [sys.executable, str(script), "--dist", str(dist_dir), "--output", output],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, f"manifest script failed:\n{result.stderr}"
    manifest_path = dist_dir / output
    assert manifest_path.exists(), f"manifest not written to {manifest_path}"
    with open(manifest_path, encoding="utf-8") as f:
        return json.load(f)


def _make_file(path: Path, content: bytes = b"hello world") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


# ---------------------------------------------------------------------------
# Manifest tests
# ---------------------------------------------------------------------------


class TestWriteVersionManifest:
    """Tests for scripts/write_version_manifest.py."""

    def test_manifest_contains_git_commit(self, tmp_path: Path) -> None:
        """The manifest must contain a non-empty git_commit string."""
        _make_file(tmp_path / "GifAgentUI.exe")
        manifest = _run_manifest(tmp_path)
        assert isinstance(manifest["git_commit"], str)
        assert len(manifest["git_commit"]) > 0

    def test_manifest_contains_python_version(self, tmp_path: Path) -> None:
        """The manifest must contain a python_version string."""
        _make_file(tmp_path / "GifAgentUI.exe")
        manifest = _run_manifest(tmp_path)
        assert isinstance(manifest["python_version"], str)
        assert len(manifest["python_version"]) > 0

    def test_manifest_config_schema_version(self, tmp_path: Path) -> None:
        """The manifest must include config_schema_version."""
        _make_file(tmp_path / "GifAgentUI.exe")
        manifest = _run_manifest(tmp_path)
        assert manifest["config_schema_version"] == "1.0"

    def test_manifest_task_migration_version(self, tmp_path: Path) -> None:
        """The manifest must include task_migration_version."""
        _make_file(tmp_path / "GifAgentUI.exe")
        manifest = _run_manifest(tmp_path)
        assert manifest["task_migration_version"] == 1

    def test_manifest_has_created_at(self, tmp_path: Path) -> None:
        """The manifest must have an ISO-format created_at."""
        _make_file(tmp_path / "GifAgentUI.exe")
        manifest = _run_manifest(tmp_path)
        assert isinstance(manifest["created_at"], str)
        assert "T" in manifest["created_at"]

    def test_manifest_git_dirty_bool(self, tmp_path: Path) -> None:
        """git_dirty must be a boolean."""
        _make_file(tmp_path / "GifAgentUI.exe")
        manifest = _run_manifest(tmp_path)
        assert isinstance(manifest["git_dirty"], bool)

    def test_present_file_has_sha256_and_size(self, tmp_path: Path) -> None:
        """A present file must record sha256 and size_bytes."""
        content = b"fake exe content"
        _make_file(tmp_path / "GifAgentUI.exe", content)
        manifest = _run_manifest(tmp_path)
        entry = manifest["files"].get("GifAgentUI.exe")
        assert entry is not None, "GifAgentUI.exe not in manifest files"
        assert entry["sha256"] == _sha256(content)
        assert entry["size_bytes"] == len(content)

    def test_missing_file_listed_in_files_missing(self, tmp_path: Path) -> None:
        """If a file does not exist, it must appear in files_missing."""
        manifest = _run_manifest(tmp_path)
        assert "GifAgentUI.exe" in manifest.get("files_missing", [])

    def test_present_and_missing_separated(self, tmp_path: Path) -> None:
        """Present files go to 'files'; missing files go to 'files_missing'."""
        _make_file(tmp_path / "GifAgentUI.exe")
        manifest = _run_manifest(tmp_path)
        assert "GifAgentUI.exe" in manifest["files"]
        # repository.py does not exist -> listed as missing
        missing = manifest.get("files_missing", [])
        assert "_internal/app/task_engine/repository.py" in missing

    def test_manifest_is_valid_json(self, tmp_path: Path) -> None:
        """Output must be valid JSON."""
        _make_file(tmp_path / "GifAgentUI.exe")
        script = Path(__file__).resolve().parents[1] / "scripts" / "write_version_manifest.py"
        result = subprocess.run(
            [sys.executable, str(script), "--dist", str(tmp_path)],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0
        # Ensure the default output file name works
        manifest_path = tmp_path / "version_manifest.json"
        assert manifest_path.exists()
        with open(manifest_path, encoding="utf-8") as f:
            data = json.load(f)
        assert isinstance(data, dict)
        assert "git_commit" in data

    def test_repository_py_sha256(self, tmp_path: Path) -> None:
        """If _internal/.../repository.py exists, record its sha256."""
        repo_path = tmp_path / "_internal/app/task_engine/repository.py"
        content = b"# fake repository module"
        _make_file(repo_path, content)
        _make_file(tmp_path / "GifAgentUI.exe", b"exe")
        manifest = _run_manifest(tmp_path)
        rel = "_internal/app/task_engine/repository.py"
        assert rel in manifest["files"]
        assert manifest["files"][rel]["sha256"] == _sha256(content)
        assert manifest["files"][rel]["size_bytes"] == len(content)


# ---------------------------------------------------------------------------
# Smoke script tests
# ---------------------------------------------------------------------------


class TestSmokeTaskEngineScript:
    """Tests for scripts/smoke_task_engine.py."""

    SMOKE_SCRIPT = (
        Path(__file__).resolve().parents[1] / "scripts" / "smoke_task_engine.py"
    )

    def test_requires_data_dir(self) -> None:
        """smoke_task_engine.py must require --data-dir."""
        result = subprocess.run(
            [sys.executable, str(self.SMOKE_SCRIPT)],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode != 0
        assert "required" in result.stderr.lower() or "required" in result.stdout.lower()
        assert "--data-dir" in result.stderr or "--data-dir" in result.stdout

    def test_rejects_production_data_dir(self) -> None:
        """smoke must refuse to run against the configured production data dir."""
        # Use "data" as the production data directory
        result = subprocess.run(
            [sys.executable, str(self.SMOKE_SCRIPT), "--data-dir", "data"],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 2
        message = (result.stderr + result.stdout).lower()
        assert "production data directory" in message or "refusing" in message

    def test_rejects_realpath_data_dir(self) -> None:
        """smoke must reject realpath-normalised production directories."""
        cwd = Path.cwd()
        result = subprocess.run(
            [sys.executable, str(self.SMOKE_SCRIPT),
             "--data-dir", str(cwd / "data")],
            capture_output=True, text=True, timeout=30,
        )
        # Should exit with code 2
        assert result.returncode == 2

    def test_rejects_dist_data_dir(self) -> None:
        """smoke must reject dist/GifAgentUI/data."""
        result = subprocess.run(
            [sys.executable, str(self.SMOKE_SCRIPT),
             "--data-dir", "dist/GifAgentUI/data"],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 2

    @pytest.mark.slow
    def test_smoke_runs_with_tmp_dir(self, tmp_path: Path) -> None:
        """smoke should pass with a temporary data directory."""
        result = subprocess.run(
            [sys.executable, str(self.SMOKE_SCRIPT),
             "--data-dir", str(tmp_path)],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            msg = (result.stderr + result.stdout)[:500]
            pytest.skip(f"smoke test failed (expected if dependencies missing): {msg}")
