from pathlib import Path

import gradio as gr


def test_control_layout_declares_fixed_summary_and_detailed_log():
    from app.ui import legacy_candidate_review as candidate_review

    source = Path(candidate_review.__file__).read_text(encoding="utf-8")
    assert 'label="Batch Status"' in source
    assert 'label="Detailed Output Log"' in source
    assert 'elem_id="batch-status"' in source
    assert 'elem_id="batch-log"' in source
    assert "fn=start_batch_queue" in source
    assert "inputs=[dir_input, limit_input, ext_input]" in source


def test_folder_page_selects_first_candidate_for_preview(monkeypatch):
    from app.ui import candidate_review

    first = {
        "candidate_id": "cand-first",
        "source_run_candidate_id": "run-first",
        "artifact_path": "D:/exports/A/first.gif",
    }
    monkeypatch.setattr(
        candidate_review,
        "load_candidate_page",
        lambda *_args, **_kwargs: (["first"], "Folder A", gr.update(value=0), [first]),
    )

    result = candidate_review.load_folder_page("D:/exports/A", "candidate")

    assert result[4] == "cand-first"
    assert result[6] == "D:/exports/A/first.gif"


def test_profile_controls_are_declared_in_a_separate_tab():
    from app.ui import candidate_review

    source = Path(candidate_review.__file__).read_text(encoding="utf-8")

    assert 'with gr.Tab("Profile")' in source


def test_selected_preview_css_keeps_gif_centered():
    from app.ui.tabs.review import REVIEW_LAYOUT_CSS

    assert "selected-gif-preview" in REVIEW_LAYOUT_CSS
    assert "object-position: center" in REVIEW_LAYOUT_CSS


def test_review_shortcuts_include_like_neutral_dislike_and_favorite():
    from app.ui.tabs.review import REVIEW_SHORTCUTS_JS

    assert "'1': 'like-btn'" in REVIEW_SHORTCUTS_JS
    assert "'2': 'neutral-btn'" in REVIEW_SHORTCUTS_JS
    assert "'3': 'dislike-btn'" in REVIEW_SHORTCUTS_JS
    assert "'4': 'favorite-btn'" in REVIEW_SHORTCUTS_JS
    assert "'z'" in REVIEW_SHORTCUTS_JS
    assert "undo-btn" in REVIEW_SHORTCUTS_JS
    assert "ctrlKey" in REVIEW_SHORTCUTS_JS
    assert "INPUT" in REVIEW_SHORTCUTS_JS


def test_control_tab_calls_build_control_tab():
    """The Control tab uses ``build_control_tab`` from the new tab module."""
    from app.ui import candidate_review

    source = Path(candidate_review.__file__).read_text(encoding="utf-8")

    # The new code path calls build_control_tab from the tabs sub-package
    assert "build_control_tab(client)" in source


def test_control_tab_uses_gif_agent_api_client():
    """The Control tab instantiates ``GifAgentApiClient`` for the task API."""
    from app.ui import candidate_review

    source = Path(candidate_review.__file__).read_text(encoding="utf-8")

    # The new code path creates a GifAgentApiClient instance
    assert "GifAgentApiClient(API_BASE)" in source


def test_control_tab_legacy_mode_guard_is_present():
    """The legacy PID mode is activated by ``GIFAGENT_LEGACY_QUEUE_UI``."""
    from app.ui import candidate_review

    source = Path(candidate_review.__file__).read_text(encoding="utf-8")

    assert "GIFAGENT_LEGACY_QUEUE_UI" in source


def test_review_layout_uses_scoped_ga_classes_and_scales():
    """The Review tab rows/columns carry the responsive ga hooks and 2:3 scale."""
    from app.ui.tabs import review

    source = Path(review.__file__).read_text(encoding="utf-8")
    assert "ga-review-layout" in source
    assert "ga-review-preview" in source
    assert "ga-review-controls" in source
    assert "ga-review-gallery" in source
    assert "ga-selected-preview" in source
    assert "scale=2" in source
    assert "scale=3" in source


def test_review_gallery_uses_auto_columns_without_fixed_height():
    """The candidate Gallery uses columns=None and no fixed height."""
    from app.ui.tabs import review

    source = Path(review.__file__).read_text(encoding="utf-8")
    assert "columns=None" in source
    assert "height=600" not in source


def test_review_components_build_with_scoped_classes():
    """build_review_tab still returns all keys with the responsive hooks."""
    components = None
    with gr.Blocks():
        from app.ui.tabs.review import build_review_tab

        components = build_review_tab()

    assert "ga-review-gallery" in components["gallery"].elem_classes
    assert "ga-selected-preview" in components["selected_preview"].elem_classes
    assert components["gallery"].columns is None
    assert components["selected_preview"].height is None
