from pathlib import Path

import gradio as gr


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
    from app.ui.candidate_review import REVIEW_LAYOUT_CSS

    assert "selected-gif-preview" in REVIEW_LAYOUT_CSS
    assert "object-position: center" in REVIEW_LAYOUT_CSS


def test_review_shortcuts_include_like_neutral_dislike_and_favorite():
    from app.ui.candidate_review import REVIEW_SHORTCUTS_JS

    assert "'1': 'like-btn'" in REVIEW_SHORTCUTS_JS
    assert "'2': 'neutral-btn'" in REVIEW_SHORTCUTS_JS
    assert "'3': 'dislike-btn'" in REVIEW_SHORTCUTS_JS
    assert "'4': 'favorite-btn'" in REVIEW_SHORTCUTS_JS
    assert "INPUT" in REVIEW_SHORTCUTS_JS
