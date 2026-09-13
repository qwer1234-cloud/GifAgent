"""Disable pipeline library ingest during pytest unless a test opts in."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _disable_pipeline_library_ingest(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIFAGENT_LIBRARY_INGEST", "0")
