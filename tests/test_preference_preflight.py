"""Pre-flight safety tests for Preference Memory migrations."""

import json
from pathlib import Path


def test_preference_memory_status_reports_safe_defaults(tmp_path, monkeypatch):
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

    assert status["preference_memory_enabled"] is False
    assert status["embedding_model"] == "nomic-embed-text:latest"
    assert status["embedding_dim"] == 768
    assert status["production_write_allowed"] is False
