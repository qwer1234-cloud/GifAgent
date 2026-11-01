"""Exercise review previews through Gradio's actual file-serving boundary."""

import asyncio
import importlib
from pathlib import Path

import gradio as gr
import pytest
from fastapi.testclient import TestClient
from gradio.routes import App
from PIL import Image


def _write_gif(path: Path, color: str = "red") -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (8, 8), color).save(
        path,
        save_all=True,
        append_images=[Image.new("RGB", (8, 8), "blue")],
        duration=40,
        loop=0,
    )
    return path.read_bytes()


def _serve_previews(paths: list[str]) -> list[bytes]:
    with gr.Blocks(analytics_enabled=False) as demo:
        outputs = [gr.Image() for _ in paths]
        gr.Button().click(lambda: paths if len(paths) > 1 else paths[0], outputs=outputs)
    result = asyncio.run(demo.process_api(0, inputs=[]))
    with TestClient(App.create_app(demo)) as client:
        responses = [client.get(value["url"]) for value in result["data"]]
    assert [response.status_code for response in responses] == [200] * len(paths)
    assert all(response.headers["content-type"] == "image/gif" for response in responses)
    return [response.content for response in responses]


@pytest.mark.parametrize("module_name", ["app.ui.tabs.review", "app.ui.legacy_candidate_review"])
@pytest.mark.parametrize("name", ["video%C2%A0.gif", "video%E2%80%8B.gif", "video#1.gif"])
def test_selected_gif_with_url_characters_loads_without_changing_feedback_path(
    tmp_path, monkeypatch, module_name, name
):
    module = importlib.import_module(module_name)
    monkeypatch.chdir(tmp_path)
    artifact = tmp_path / "exports%20" / name
    expected = _write_gif(artifact)
    item = {"candidate_id": "cand-one", "artifact_path": str(artifact)}

    candidate_id, _label, preview, feedback_path = module.selection_values(item)

    assert _serve_previews([preview]) == [expected]
    assert candidate_id == "cand-one"
    assert feedback_path == str(artifact)
    assert item["artifact_path"] == str(artifact)


def test_next_gif_preload_uses_a_loadable_preview(tmp_path, monkeypatch):
    from app.ui.tabs import review

    monkeypatch.chdir(tmp_path)
    paths = [tmp_path / "first.gif", tmp_path / "next%E2%80%8B.gif"]
    expected = [_write_gif(path) for path in paths]
    items = [
        {"candidate_id": f"cand-{index}", "artifact_path": str(path)}
        for index, path in enumerate(paths)
    ]

    _cid, _label, preview, feedback_path, next_preview = review.selection_values_with_next(
        items[0], items
    )

    assert _serve_previews([preview, next_preview]) == expected
    assert feedback_path == str(paths[0])


def test_replaced_gif_refreshes_the_preview(tmp_path, monkeypatch):
    from app.ui.tabs import review

    monkeypatch.chdir(tmp_path)
    artifact = tmp_path / "video%20.gif"
    first = _write_gif(artifact)
    item = {"candidate_id": "cand-one", "artifact_path": str(artifact)}
    assert _serve_previews([review.selection_values(item)[2]]) == [first]

    replacement = tmp_path / "replacement.gif"
    second = _write_gif(replacement, "green")
    replacement.replace(artifact)

    assert _serve_previews([review.selection_values(item)[2]]) == [second]
    assert second != first


def test_preview_still_loads_when_hard_links_are_unavailable(tmp_path, monkeypatch):
    import errno

    from app.ui import preview_paths
    from app.ui.tabs import review

    monkeypatch.chdir(tmp_path)
    artifact = tmp_path / "video%20.gif"
    expected = _write_gif(artifact)

    def cross_volume_link(*_args):
        raise OSError(errno.EXDEV, "Cross-device link")

    monkeypatch.setattr(preview_paths.os, "link", cross_volume_link)
    item = {"candidate_id": "cand-one", "artifact_path": str(artifact)}

    assert _serve_previews([review.selection_values(item)[2]]) == [expected]
