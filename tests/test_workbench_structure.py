"""Tests for Phase 4 Task 1: Workbench shell and modular UI boundary."""
from __future__ import annotations

from pathlib import Path


def test_candidate_review_under_300_lines():
    """candidate_review.py must be a thin launcher under 300 lines."""
    from app.ui import candidate_review

    source = Path(candidate_review.__file__).read_text(encoding="utf-8")
    line_count = len(source.splitlines())
    assert line_count < 300, (
        f"candidate_review.py has {line_count} lines, expected < 300"
    )


def test_candidate_review_no_sql():
    """candidate_review.py must not contain inline SQL code."""
    from app.ui import candidate_review

    source = Path(candidate_review.__file__).read_text(encoding="utf-8")
    # Re-exports from app.db are OK; inline SQL usage is not.
    for pattern in [".execute(", "cursor", "SELECT ", "INSERT "]:
        assert pattern not in source, (
            f"SQL pattern {pattern!r} found in candidate_review.py"
        )


def test_candidate_review_no_subprocess():
    """candidate_review.py must not contain subprocess usage."""
    from app.ui import candidate_review

    source = Path(candidate_review.__file__).read_text(encoding="utf-8")
    assert "subprocess" not in source, (
        "subprocess reference found in candidate_review.py"
    )


def test_candidate_review_no_queue_lifecycle():
    """candidate_review.py must not contain queue lifecycle code."""
    from app.ui import candidate_review

    source = Path(candidate_review.__file__).read_text(encoding="utf-8")
    forbidden = [
        "PID_FILE",
        "CHECKPOINT_FILE",
        "get_batch_status",
        "stop_batch",
        "start_batch",
        "is_batch_process",
    ]
    for pattern in forbidden:
        assert pattern not in source, (
            f"Queue lifecycle pattern {pattern!r} found in candidate_review.py"
        )


def test_candidate_review_imports_tab_builders():
    """candidate_review.py must import the workbench tab builders."""
    from app.ui import candidate_review

    source = Path(candidate_review.__file__).read_text(encoding="utf-8")
    assert "build_workbench" in source
    assert "WorkbenchContext" in source
    assert "launch_kwargs" in source


def test_workbench_context_dataclass():
    """WorkbenchContext must be a frozen dataclass with the expected fields."""
    from app.ui.workbench import WorkbenchContext
    from dataclasses import fields

    field_names = {f.name for f in fields(WorkbenchContext)}
    assert "client" in field_names
    assert "allowed_paths" in field_names
    assert "refresh_seconds" in field_names


def test_build_workbench_function():
    """build_workbench must accept WorkbenchContext and return gr.Blocks."""
    from app.ui.workbench import WorkbenchContext, build_workbench
    from app.ui.api_client import GifAgentApiClient

    import gradio as gr

    context = WorkbenchContext(
        client=GifAgentApiClient(),
        allowed_paths=("/tmp",),
    )
    result = build_workbench(context)
    assert isinstance(result, gr.Blocks)


def test_navigation_tabs_present_exactly_once():
    """All 7 Chinese tab labels must appear exactly once in workbench.py."""
    from app.ui import workbench

    source = Path(workbench.__file__).read_text(encoding="utf-8")
    tabs = ["今日", "队列", "审核", "搜索", "合集", "实验室", "设置"]
    for tab in tabs:
        count = source.count(tab)
        assert count == 1, (
            f"Tab label {tab!r} appears {count} times in workbench.py "
            f"(expected 1)"
        )


def test_tab_builder_functions_exported():
    """All 4 new tab builder functions must be importable from workbench."""
    from app.ui.workbench import (
        build_today_tab,
        build_search_tab,
        build_collections_tab,
        build_settings_tab,
    )
    # Verify each is callable
    assert callable(build_today_tab)
    assert callable(build_search_tab)
    assert callable(build_collections_tab)
    assert callable(build_settings_tab)


def test_component_modules_exist():
    """Components package must exist with common module."""
    from app.ui.components import common  # noqa: F401


def test_new_tab_modules_exist():
    """Today, search, collections, and settings tab modules must exist."""
    from app.ui.tabs import today  # noqa: F401
    from app.ui.tabs import search  # noqa: F401
    from app.ui.tabs import collections  # noqa: F401
    from app.ui.tabs import settings  # noqa: F401
