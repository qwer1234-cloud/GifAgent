from __future__ import annotations

import gradio as gr


def test_select_first_candidate_returns_preview_for_next_gif():
    from app.ui.candidate_review import select_first_candidate

    selected = select_first_candidate(
        [
            {
                "candidate_id": "cand-next",
                "source_run_candidate_id": "run-next",
                "artifact_path": "D:/exports/next.gif",
            }
        ]
    )

    assert selected == (
        "cand-next",
        "Selected: run-next",
        "D:/exports/next.gif",
        "D:/exports/next.gif",
    )


def test_next_reviewable_folder_uses_queue_order_then_wraps_remaining_folders():
    from app.ui.candidate_review import next_reviewable_folder

    folders = [
        {"folder": "A"},
        {"folder": "B"},
        {"folder": "C"},
    ]

    assert next_reviewable_folder(folders, [{"folder": "B"}, {"folder": "C"}], "A") == "B"
    assert next_reviewable_folder(folders, [{"folder": "A"}], "C") == "A"
    assert next_reviewable_folder(folders, [], "C") is None


def test_rate_and_advance_selects_next_gif_in_current_folder(monkeypatch):
    from app.ui import candidate_review

    next_item = {
        "candidate_id": "cand-next",
        "source_run_candidate_id": "next-run",
        "artifact_path": "D:/exports/A/next.gif",
    }
    monkeypatch.setattr(candidate_review, "rate_candidate", lambda *_args: "Rated: liked")
    monkeypatch.setattr(
        candidate_review,
        "load_candidate_page",
        lambda *_args, **_kwargs: (["gallery"], "Folder: A", gr.update(value=0), [next_item]),
    )

    result = candidate_review.rate_and_advance(
        "cand-current", "like", "", "D:/exports/A/current.gif", 0, "candidate", "A", "D:/exports", [{"folder": "A"}]
    )

    assert result[0] == "Rated: liked"
    assert result[5] == "cand-next"
    assert result[7] == "D:/exports/A/next.gif"


def test_rate_and_advance_loads_next_folder_after_current_folder_is_complete(monkeypatch):
    from app.ui import candidate_review

    next_item = {
        "candidate_id": "cand-b",
        "source_run_candidate_id": "run-b",
        "artifact_path": "D:/exports/B/next.gif",
    }
    refreshed_folders = [{"folder": "B", "relative_folder": "B", "count": 1}]

    monkeypatch.setattr(candidate_review, "rate_candidate", lambda *_args: "Rated: neutral")

    def fake_load_page(page, page_size=candidate_review.PAGE_SIZE, filter_status="candidate", folder=None):
        if folder == "A":
            return [], "Folder: A complete", gr.update(value=0), []
        assert folder == "B"
        return ["gallery-b"], "Folder: B", gr.update(value=0), [next_item]

    monkeypatch.setattr(candidate_review, "load_candidate_page", fake_load_page)
    monkeypatch.setattr(
        candidate_review,
        "load_folder_choices",
        lambda _root: (gr.update(choices=[("B", "B")], value=None), "Found B", refreshed_folders),
    )

    result = candidate_review.rate_and_advance(
        "cand-a", "neutral", "", "D:/exports/A/current.gif", 0, "candidate", "A", "D:/exports", [{"folder": "A"}, {"folder": "B"}]
    )

    assert result[5] == "cand-b"
    assert result[7] == "D:/exports/B/next.gif"
    assert result[9]["value"] == "B"
    assert result[10] == refreshed_folders
