"""Tests for Phase 3 Task 6: Active review and profile UI tabs.

Verifies tab modules exist, expose the expected builder functions, and the
main candidate_review module composes them as a thin wrapper.
"""

from __future__ import annotations

from pathlib import Path


# ── Tab module imports ──────────────────────────────────────────────────────


class TestReviewTabModule:
    def test_review_tab_module_exists(self):
        """app/ui/tabs/review.py exists and is importable."""
        from app.ui.tabs import review
        assert review is not None

    def test_review_tab_exports_build_function(self):
        """The review tab module exports ``build_review_tab``."""
        from app.ui.tabs.review import build_review_tab
        assert callable(build_review_tab)

    def test_review_tab_build_returns_component_dict(self):
        """``build_review_tab()`` returns a dict of component references."""
        import gradio as gr
        from app.ui.tabs.review import build_review_tab

        with gr.Blocks():
            components = build_review_tab()
        assert isinstance(components, dict)
        # Expect at least a few key components
        assert "gallery" in components
        assert "candidate_id_input" in components
        assert "folder_dropdown" in components


class TestProfileTabModule:
    def test_profile_tab_module_exists(self):
        """app/ui/tabs/profile.py exists and is importable."""
        from app.ui.tabs import profile
        assert profile is not None

    def test_profile_tab_exports_build_function(self):
        """The profile tab module exports ``build_profile_tab``."""
        from app.ui.tabs.profile import build_profile_tab
        assert callable(build_profile_tab)

    def test_profile_tab_build_returns_component_dict(self):
        """``build_profile_tab()`` returns a dict of component references."""
        import gradio as gr
        from app.ui.tabs.profile import build_profile_tab

        with gr.Blocks():
            components = build_profile_tab()
        assert isinstance(components, dict)
        # Expect key profile components
        assert "profile_status" in components
        assert "build_btn" in components
        assert "publish_btn" in components
        assert "publish_profile_dropdown" in components

    def test_profile_tab_preview_button_separate_from_publish(self):
        """Profile tab has separate preview and publish buttons."""
        import gradio as gr
        from app.ui.tabs.profile import build_profile_tab

        with gr.Blocks():
            components = build_profile_tab()

        # There should be a build button (which does the preview/build)
        # and a separate publish button.
        assert "build_btn" in components
        assert "publish_btn" in components
        # The build button and publish button should be distinct
        assert components["build_btn"] != components["publish_btn"]


# ── candidate_review composition ────────────────────────────────────────────


class TestCandidateReviewComposition:
    def test_candidate_review_imports_review_tab(self):
        """candidate_review.py imports build_review_tab from tabs.review."""
        from app.ui import candidate_review

        source = Path(candidate_review.__file__).read_text(encoding="utf-8")
        assert "from app.ui.tabs.review import build_review_tab" in source
        assert "build_review_tab()" in source

    def test_candidate_review_imports_profile_tab(self):
        """candidate_review.py imports build_profile_tab from tabs.profile."""
        from app.ui import candidate_review

        source = Path(candidate_review.__file__).read_text(encoding="utf-8")
        assert "from app.ui.tabs.profile import build_profile_tab" in source
        assert "build_profile_tab()" in source

    def test_candidate_review_does_not_define_inline_profile_tab(self):
        """candidate_review.py delegates profile tab to build_profile_tab
        instead of building inline."""
        from app.ui import candidate_review

        source = Path(candidate_review.__file__).read_text(encoding="utf-8")
        # Should NOT have the old inline Profile construction elements.
        # The old code had `profile_status = gr.Textbox(label="Status"...)`
        # The new code accesses via dict: profile_components["profile_status"]
        assert "profile_status = gr.Textbox" not in source
        # Should delegate to the tab module
        assert "from app.ui.tabs.profile import build_profile_tab" in source
        # The tab module call builds the components
        assert "profile_components = build_profile_tab()" in source

    def test_candidate_review_does_not_define_inline_review_tab(self):
        """candidate_review.py delegates review tab to build_review_tab
        instead of building inline."""
        from app.ui import candidate_review

        source = Path(candidate_review.__file__).read_text(encoding="utf-8")
        # Should NOT have the old inline Review construction elements
        assert "review_root_input = gr.Textbox" not in source
        assert "gallery = gr.Gallery" not in source
        # Should delegate to the tab module
        assert "from app.ui.tabs.review import build_review_tab" in source
        # The tab module call builds the components
        assert "review_components = build_review_tab()" in source


# ── Helper function compatibility ───────────────────────────────────────────


class TestHelperCompatibility:
    def test_candidate_review_exports_rate_and_advance(self):
        """Legacy helper functions remain accessible on candidate_review."""
        from app.ui import candidate_review
        assert hasattr(candidate_review, "rate_and_advance")

    def test_candidate_review_exports_profile_publish_choices(self):
        """Legacy profile helper functions remain accessible."""
        from app.ui import candidate_review
        assert hasattr(candidate_review, "profile_publish_choices")

    def test_candidate_review_exports_undo_last_action(self):
        """Undo helper remains on candidate_review."""
        from app.ui import candidate_review
        assert hasattr(candidate_review, "undo_last_action")
