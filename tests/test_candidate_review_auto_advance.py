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


def test_load_folder_choices_reports_remaining_reviewable_folder_count(monkeypatch):
    from app.ui.tabs import review

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {
                "root": "D:/exports",
                "folders": [
                    {"folder": "A", "status_counts": {"candidate": 2}},
                    {"folder": "B", "status_counts": {"candidate": 0}},
                    {"folder": "C", "status_counts": {"candidate": 1}},
                ],
            }

    monkeypatch.setattr(review.httpx, "get", lambda *_args, **_kwargs: FakeResponse())

    result = review.load_folder_choices("D:/exports")

    assert result[3] == 2


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
        lambda _root: (
            gr.update(choices=[("B", "B")], value=None),
            "Found B",
            refreshed_folders,
            1,
        ),
    )

    result = candidate_review.rate_and_advance(
        "cand-a", "neutral", "", "D:/exports/A/current.gif", 0, "candidate", "A", "D:/exports", [{"folder": "A"}, {"folder": "B"}]
    )

    assert result[5] == "cand-b"
    assert result[7] == "D:/exports/B/next.gif"
    assert result[9]["value"] == "B"
    assert result[10] == refreshed_folders
    assert result[12] == 1


def test_rate_and_advance_consumes_current_page_queue_without_reloading(monkeypatch):
    from app.ui.tabs import review

    current = {
        "candidate_id": "cand-current",
        "source_run_candidate_id": "run-current",
        "artifact_path": "D:/exports/A/current.gif",
        "status": "candidate",
    }
    following = {
        "candidate_id": "cand-following",
        "source_run_candidate_id": "run-following",
        "artifact_path": "D:/exports/A/following.gif",
        "status": "candidate",
    }
    following_next = {
        "candidate_id": "cand-following-next",
        "source_run_candidate_id": "run-following-next",
        "artifact_path": "D:/exports/A/following-next.gif",
        "status": "candidate",
    }
    later = {
        "candidate_id": "cand-later",
        "source_run_candidate_id": "run-later",
        "artifact_path": "D:/exports/A/later.gif",
        "status": "candidate",
    }

    def fail_if_reloaded(*_args, **_kwargs):
        raise AssertionError("the current page should be consumed locally")

    monkeypatch.setattr(review, "_candidate_display_path", lambda item: item["artifact_path"])

    result = review.rate_and_advance(
        "cand-current",
        "like",
        "",
        "D:/exports/A/current.gif",
        0,
        "candidate",
        "A",
        "D:/exports",
        [{"folder": "A"}],
        [current, following, later],
        _submit_action=lambda *_args: "Rated: liked",
        _load_page=fail_if_reloaded,
    )

    assert [item["candidate_id"] for item in result[4]] == [
        "cand-following",
        "cand-later",
    ]
    assert result[5] == "cand-following"
    assert result[7] == "D:/exports/A/following.gif"
    assert result[11] == "D:/exports/A/later.gif"


def test_rate_and_advance_reloads_only_when_local_page_queue_is_empty(monkeypatch):
    from app.ui.tabs import review

    current = {
        "candidate_id": "cand-current",
        "source_run_candidate_id": "run-current",
        "artifact_path": "D:/exports/A/current.gif",
        "status": "candidate",
    }
    following = {
        "candidate_id": "cand-following",
        "source_run_candidate_id": "run-following",
        "artifact_path": "D:/exports/A/following.gif",
        "status": "candidate",
    }
    following_next = {
        "candidate_id": "cand-following-next",
        "source_run_candidate_id": "run-following-next",
        "artifact_path": "D:/exports/A/following-next.gif",
        "status": "candidate",
    }
    calls = []

    def load_page(page, page_size=review.PAGE_SIZE, filter_status="candidate", folder=None):
        calls.append((page, page_size, filter_status, folder))
        return ["gallery"], "Folder: A", gr.update(value=0), [following, following_next]

    monkeypatch.setattr(review, "_candidate_display_path", lambda item: item["artifact_path"])

    result = review.rate_and_advance(
        "cand-current",
        "like",
        "",
        "D:/exports/A/current.gif",
        0,
        "candidate",
        "A",
        "D:/exports",
        [{"folder": "A"}],
        [current],
        _submit_action=lambda *_args: "Rated: liked",
        _load_page=load_page,
    )

    assert calls == [(0, review.PAGE_SIZE, "candidate", "A")]
    assert result[4] == [following, following_next]
    assert result[5] == "cand-following"
    assert result[11] == "D:/exports/A/following-next.gif"


def test_select_candidate_returns_the_following_path_for_preload():
    from app.ui.tabs import review

    items = [
        {
            "candidate_id": "cand-one",
            "source_run_candidate_id": "run-one",
            "artifact_path": "D:/exports/A/one.gif",
        },
        {
            "candidate_id": "cand-two",
            "source_run_candidate_id": "run-two",
            "artifact_path": "D:/exports/A/two.gif",
        },
    ]

    values = review.selection_values_with_next(items[0], items)

    assert values[:4] == (
        "cand-one",
        "Selected: run-one",
        "D:/exports/A/one.gif",
        "D:/exports/A/one.gif",
    )
    assert values[4] == "D:/exports/A/two.gif"
