from __future__ import annotations

import gradio as gr
import pytest


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


def _candidate(cid, artifact_path, status="candidate", start_sec=0.0, end_sec=1.0):
    return {
        "candidate_id": cid,
        "source_run_candidate_id": f"run-{cid}",
        "artifact_path": artifact_path,
        "preview_path": artifact_path,
        "status": status,
        "start_sec": start_sec,
        "end_sec": end_sec,
    }


def test_local_advance_skips_server_reloads():
    from app.ui.tabs import review as review_tab

    items = [
        _candidate("cand-a", "D:/exports/A/a.gif"),
        _candidate("cand-b", "D:/exports/A/b.gif"),
    ]
    calls = {"load_page": 0, "load_folders": 0}

    def counting_load_page(*_args, **_kwargs):
        calls["load_page"] += 1
        return [], "unused", gr.update(), []

    def counting_load_folders(*_args, **_kwargs):
        calls["load_folders"] += 1
        return gr.update(), "unused", []

    result = review_tab.rate_and_advance(
        "cand-a", "like", "", "D:/exports/A/a.gif", 0, "candidate", "A",
        "D:/exports", [{"folder": "A"}],
        page_items_state=items,
        _submit_action=lambda *_args: "Rated: liked",
        _load_page=counting_load_page,
        _load_folders=counting_load_folders,
    )

    assert calls == {"load_page": 0, "load_folders": 0}
    assert result[0] == "Rated: liked"
    assert result[4] == [items[1]]
    assert result[5] == "cand-b"


def test_local_advance_removes_arbitrary_rated_id_and_keeps_alignment():
    from app.ui.tabs import review as review_tab

    items = [
        _candidate("cand-a", "D:/exports/A/a.gif", start_sec=1.0, end_sec=2.0),
        _candidate("cand-b", "D:/exports/A/b.gif", start_sec=3.0, end_sec=4.0),
        _candidate("cand-c", "D:/exports/A/c.gif", start_sec=5.0, end_sec=6.0),
    ]
    remaining = [items[0], items[2]]

    result = review_tab.rate_and_advance(
        "cand-b", "like", "", "D:/exports/A/b.gif", 0, "candidate", "A",
        "D:/exports", [{"folder": "A"}],
        page_items_state=items,
        _submit_action=lambda *_args: "Rated: liked",
        _load_page=lambda *_a, **_k: pytest.fail("_load_page must not be called"),
        _load_folders=lambda *_a, **_k: pytest.fail("_load_folders must not be called"),
    )

    assert result[4] == remaining
    assert result[1] == [
        ("D:/exports/A/a.gif", "todo [candidate] 1s-2s | cand-a"),
        ("D:/exports/A/c.gif", "todo [candidate] 5s-6s | cand-c"),
    ]


def test_local_advance_selects_next_candidate_and_preview():
    from app.ui.tabs import review as review_tab

    items = [
        _candidate("cand-first", "D:/exports/A/first.gif"),
        _candidate("cand-rated", "D:/exports/A/rated.gif"),
    ]

    result = review_tab.rate_and_advance(
        "cand-rated", "like", "", "D:/exports/A/rated.gif", 0, "candidate", "A",
        "D:/exports", [{"folder": "A"}],
        page_items_state=items,
        _submit_action=lambda *_args: "Rated: liked",
        _load_page=lambda *_a, **_k: pytest.fail("_load_page must not be called"),
        _load_folders=lambda *_a, **_k: pytest.fail("_load_folders must not be called"),
    )

    assert result[5] == "cand-first"
    assert result[6] == "Selected: run-cand-first"
    assert result[7] == "D:/exports/A/first.gif"
    assert result[8] == "D:/exports/A/first.gif"


def test_non_candidate_filter_falls_back_to_single_page_load():
    from app.ui.tabs import review as review_tab

    items = [
        _candidate("cand-a", "D:/exports/A/a.gif"),
        _candidate("cand-b", "D:/exports/A/b.gif"),
    ]
    calls = {"load_page": 0, "load_folders": 0}

    def counting_load_page(*_args, **_kwargs):
        calls["load_page"] += 1
        return ["gallery"], "Folder: A", gr.update(value=0), items

    def counting_load_folders(*_args, **_kwargs):
        calls["load_folders"] += 1
        return gr.update(), "unused", []

    result = review_tab.rate_and_advance(
        "cand-a", "like", "", "D:/exports/A/a.gif", 0, "favorited", "A",
        "D:/exports", [{"folder": "A"}],
        page_items_state=items,
        _submit_action=lambda *_args: "Rated: liked",
        _load_page=counting_load_page,
        _load_folders=counting_load_folders,
    )

    assert calls == {"load_page": 1, "load_folders": 0}
    assert result[4] == items


@pytest.mark.parametrize(
    "page_items_state",
    [
        None,
        "not-a-list",
        {},
        [],
        [{"candidate_id": "cand-other"}],
        [{"candidate_id": "cand-a"}],
    ],
)
def test_missing_or_inconsistent_state_falls_back_to_page_load(page_items_state):
    from app.ui.tabs import review as review_tab

    calls = {"load_page": 0}

    def counting_load_page(*_args, **_kwargs):
        calls["load_page"] += 1
        return ["gallery"], "Folder: A", gr.update(value=0), [{"candidate_id": "cand-a"}]

    result = review_tab.rate_and_advance(
        "cand-a", "like", "", "D:/exports/A/a.gif", 0, "candidate", "A",
        "D:/exports", [{"folder": "A"}],
        page_items_state=page_items_state,
        _submit_action=lambda *_args: "Rated: liked",
        _load_page=counting_load_page,
        _load_folders=lambda *_a, **_k: pytest.fail("_load_folders must not be called"),
    )

    assert calls["load_page"] == 1
    assert result[4] == [{"candidate_id": "cand-a"}]


def test_failed_post_preserves_selection_and_state():
    from app.ui.tabs import review as review_tab

    items = [
        _candidate("cand-a", "D:/exports/A/a.gif"),
        _candidate("cand-b", "D:/exports/A/b.gif"),
    ]

    result = review_tab.rate_and_advance(
        "cand-a", "like", "", "D:/exports/A/a.gif", 0, "candidate", "A",
        "D:/exports", [{"folder": "A"}],
        page_items_state=items,
        _submit_action=lambda *_args: "Error: 500 - boom",
        _load_page=lambda *_a, **_k: pytest.fail("_load_page must not run after failure"),
        _load_folders=lambda *_a, **_k: pytest.fail("_load_folders must not run after failure"),
    )

    assert result[0] == "Error: 500 - boom"
    assert result[5] == "cand-a"
    assert result[6] == "Rating failed; selection kept"
    assert result[7] == "D:/exports/A/a.gif"
    assert result[8] == "D:/exports/A/a.gif"
    assert result[4] == gr.update()
