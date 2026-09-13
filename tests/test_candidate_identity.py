from pathlib import Path
import subprocess

import pytest

from app.services.candidate_identity import DuplicateReviewIndex, clip_identity_keys, normalize_video_title


class _Row(dict):
    def __getitem__(self, key):
        return dict.__getitem__(self, key)


def test_clip_identity_keys_do_not_require_the_file_to_exist():
    keys = clip_identity_keys(
        r"Z:\missing\export\Vid@@@001_1000ms-2000ms.gif", 1.0, 2.0
    )
    assert any(key.startswith("name:") for key in keys)
    assert any(key.startswith("path:") for key in keys)


def test_normalize_video_title_strips_resolution_and_copy_suffix():
    assert normalize_video_title("Girl Rides Cock. Creampie 1080P") == "girl rides cock creampie"
    assert normalize_video_title("Same Title (1) 2160p") == "same title"


def test_shadow_status_hides_export_copy_of_rated_collection(tmp_path):
    collection = "P站顶流 Comatozze 更新至20260709大合集"
    rated = tmp_path / "data" / collection / "Old Title 1080P" / "a@@@001_1000ms-2000ms.gif"
    exported = (
        tmp_path / "adaptive_test" / collection / "New Title Creampie 1080P"
        / "b@@@009_9000ms-10000ms.gif"
    )
    rated.parent.mkdir(parents=True)
    exported.parent.mkdir(parents=True)
    index = DuplicateReviewIndex(
        [
            _Row(artifact_path=str(rated), start_sec=1.0, end_sec=2.0, status="liked"),
            _Row(artifact_path=str(exported), start_sec=9.0, end_sec=10.0, status="candidate"),
        ]
    )
    assert index.shadow_status(exported.parent) == "liked"
    assert index.shadow_status(rated.parent) is None


def test_shadow_status_keeps_unrelated_generic_parent_folders(tmp_path):
    rated = tmp_path / "data" / "videos" / "Rated Movie 1080P" / "a@@@001_1000ms-2000ms.gif"
    exported = tmp_path / "adaptive_test" / "videos" / "Other Movie 1080P" / "b@@@001_1000ms-2000ms.gif"
    index = DuplicateReviewIndex(
        [
            _Row(artifact_path=str(rated), start_sec=1.0, end_sec=2.0, status="liked"),
            _Row(artifact_path=str(exported), start_sec=1.0, end_sec=2.0, status="candidate"),
        ]
    )
    assert index.shadow_status(Path(exported).parent) is None


def _make_junction(link: Path, target: Path) -> Path:
    link.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        pytest.skip(completed.stderr.strip() or completed.stdout.strip() or "junction failed")
    return link


def test_shadow_status_does_not_treat_junction_alias_as_another_copy(tmp_path):
    real = tmp_path / "data" / "exports" / "adaptive_test" / "Vid Title 1080P"
    real.mkdir(parents=True)
    gif = real / "Vid Title 1080P@@@001_1000ms-2000ms.gif"
    gif.write_bytes(b"gif")
    alias_exports = tmp_path / "dist" / "GifAgentUI" / "data" / "exports"
    _make_junction(alias_exports, tmp_path / "data" / "exports")
    alias_gif = (
        alias_exports / "adaptive_test" / "Vid Title 1080P"
        / "Vid Title 1080P@@@001_1000ms-2000ms.gif"
    )
    index = DuplicateReviewIndex(
        [
            _Row(
                artifact_path=str(alias_gif),
                start_sec=1.0,
                end_sec=2.0,
                status="liked",
            )
        ]
    )
    assert index.shadow_status(real) is None
    assert index.shadow_status(alias_gif.parent) is None
    assert index.shadow_status(alias_gif.parent.resolve()) is None
