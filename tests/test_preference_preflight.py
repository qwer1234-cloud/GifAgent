"""Pre-flight safety tests for Preference Memory migrations."""

import json
from pathlib import Path

import pytest


@pytest.fixture
def preference_memory_enabled(monkeypatch):
    """Pin the config flag to enabled.

    These tests exercise status-reporting mechanics, not the models.yaml value
    (which was reverted to false in 9e39ac1e without flipping the assertions).
    A disabled-state control lives in
    test_preference_memory_status_reports_disabled_flag.
    """
    import app.config as app_config

    monkeypatch.setattr(
        app_config, "_config", {"preference_memory": {"enabled": True}}
    )


def test_preference_memory_status_reports_safe_defaults(tmp_path, preference_memory_enabled):
    from scripts.preference_memory import collect_status

    db_path = tmp_path / "library.db"
    index_manifest = tmp_path / "manifest.json"
    index_manifest.write_text(
        '{"embedding_model":"nomic-embed-text:latest","dim":768,"vector_count":0}',
        encoding="utf-8",
    )

    status = collect_status(
        library_db_path=db_path, faiss_manifest_path=index_manifest
    )

    assert status["preference_memory_enabled"] is True
    assert status["embedding_model"] == "nomic-embed-text:latest"
    assert status["embedding_dim"] == 768
    assert status["wal_file_exists"] is False
    assert status["production_write_allowed"] is True   # not wal_file_exists = True
    assert status["manifest_error"] is None


def test_status_handles_missing_manifest(tmp_path, preference_memory_enabled):
    """Manifest file does not exist — status still returns safe defaults with manifest_error set."""
    from scripts.preference_memory import collect_status

    db_path = tmp_path / "library.db"
    missing_manifest = tmp_path / "nonexistent.json"

    status = collect_status(
        library_db_path=db_path, faiss_manifest_path=missing_manifest
    )

    assert status["library_db_exists"] is False
    assert status["wal_file_exists"] is False
    assert status["preference_memory_enabled"] is True
    assert status["production_write_allowed"] is True   # not wal_file_exists = True
    assert status["embedding_model"] is None
    assert status["embedding_dim"] is None
    assert status["vector_count"] == 0
    assert "manifest not found" in (status["manifest_error"] or "")


def test_status_handles_malformed_manifest(tmp_path, preference_memory_enabled):
    """Manifest is not valid JSON — handled gracefully."""
    from scripts.preference_memory import collect_status

    db_path = tmp_path / "library.db"
    bad_manifest = tmp_path / "manifest.json"
    bad_manifest.write_text("not json at all", encoding="utf-8")

    status = collect_status(
        library_db_path=db_path, faiss_manifest_path=bad_manifest
    )

    assert status["preference_memory_enabled"] is True
    assert status["wal_file_exists"] is False
    assert status["production_write_allowed"] is True   # not wal_file_exists = True
    assert status["embedding_model"] is None
    assert status["embedding_dim"] is None
    assert status["vector_count"] == 0
    assert "invalid manifest JSON" in (status["manifest_error"] or "")


def test_wal_file_exists_detected(tmp_path):
    """When the WAL file is present, wal_file_exists is True and production_write_allowed is False."""
    from scripts.preference_memory import collect_status

    db_path = tmp_path / "library.db"
    wal_path = db_path.with_name(db_path.name + "-wal")
    wal_path.write_text("", encoding="utf-8")  # create WAL file

    index_manifest = tmp_path / "manifest.json"
    index_manifest.write_text(
        '{"embedding_model":"nomic-embed-text:latest","dim":768,"vector_count":0}',
        encoding="utf-8",
    )

    status = collect_status(
        library_db_path=db_path, faiss_manifest_path=index_manifest
    )

    assert status["wal_file_exists"] is True
    assert status["production_write_allowed"] is False  # not wal_file_exists = not True = False


def test_preference_memory_status_reports_disabled_flag(tmp_path, monkeypatch):
    """Control: with the flag disabled, preference_memory_enabled is False."""
    import app.config as app_config

    from scripts.preference_memory import collect_status

    monkeypatch.setattr(app_config, "_config", {"preference_memory": {"enabled": False}})

    status = collect_status(
        library_db_path=tmp_path / "library.db", faiss_manifest_path=None
    )

    assert status["preference_memory_enabled"] is False
