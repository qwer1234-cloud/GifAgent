from __future__ import annotations

import inspect
import errno
from pathlib import Path
import shutil
import subprocess
import threading

import cv2
import numpy as np
import pytest

from app.quality_moe.config import QualityMoeConfig
from app.quality_moe.models import EvidenceStatus, RepairRecipe
from app.quality_moe.sampling import SampledClip


def dark_sample() -> SampledClip:
    frames = tuple(np.full((48, 80, 3), 20, dtype=np.uint8) for _ in range(6))
    return SampledClip(
        candidate_id="dark-candidate",
        video_path="synthetic://dark-candidate",
        start_ts=0.0,
        end_ts=1.0,
        timestamps=tuple(index / 5 for index in range(6)),
        frames=frames,
    )


def gradient_sample(*, video_path: str = "synthetic://gradient") -> SampledClip:
    base = np.tile(np.linspace(10, 220, 80, dtype=np.uint8), (48, 1))
    frames = tuple(
        np.dstack((base, np.roll(base, index * 3, axis=1), base))
        for index in range(6)
    )
    return SampledClip(
        candidate_id="gradient-candidate",
        video_path=video_path,
        start_ts=0.0,
        end_ts=1.0,
        timestamps=tuple(index / 5 for index in range(6)),
        frames=frames,
    )


def test_search_never_exceeds_twelve_variants_and_uses_one_recipe_per_clip():
    from app.quality_moe.repair import apply_recipe_to_frame, search_repairs

    sample = dark_sample()
    result = search_repairs(sample, QualityMoeConfig.defaults())

    assert len(result.evaluated_recipes) <= 12
    assert result.best_recipe is not None
    transformed = [apply_recipe_to_frame(frame, result.best_recipe) for frame in sample.frames]
    assert len({result.best_recipe.recipe_id for _ in transformed}) == 1
    assert all(frame.dtype == np.uint8 for frame in transformed)


def test_invalid_crop_is_rejected():
    with pytest.raises(ValueError, match="crop area"):
        RepairRecipe(recipe_id="bad", crop=(0.0, 0.0, 0.5, 0.5)).validate()


def test_validated_best_recipe_binds_measured_delta_to_source_proxy_recipe_and_config():
    from app.quality_moe.repair import search_repairs

    sample = dark_sample()
    config = QualityMoeConfig.defaults()
    result = search_repairs(sample, config)

    assert result.best_recipe is not None
    assert result.best_recipe.quality_gain >= 0.15
    assert result.best_recipe.confidence >= 0.80
    assert result.repair_delta is not None
    assert result.repair_delta.status is EvidenceStatus.AVAILABLE
    assert result.repair_delta.scores["proxy_quality"] > result.repair_delta.scores["source_quality"]
    assert result.repair_delta.scores["quality_gain"] == pytest.approx(
        result.repair_delta.scores["proxy_quality"] - result.repair_delta.scores["source_quality"]
    )
    assert result.repair_delta.parent_input_hash == sample.input_hash
    validation = result.best_recipe.validation
    assert validation is not None
    assert validation.source_input_hash == sample.input_hash
    assert validation.proxy_artifact_hash == result.repair_delta.input_hash
    assert validation.recipe_hash == result.best_recipe.recipe_hash
    assert validation.config_hash == config.config_hash
    assert validation.repair_delta_evidence_id == result.repair_delta.identity_hash


def test_search_exposes_no_transform_callback_or_mutates_source_frames():
    from app.quality_moe.repair import render_recipe_proxy, search_repairs

    sample = gradient_sample()
    before = tuple(frame.copy() for frame in sample.frames)
    signature = inspect.signature(search_repairs)
    proxy = render_recipe_proxy(sample, RepairRecipe(recipe_id="safe", exposure_ev=0.35))

    assert "transform" not in signature.parameters
    assert all(np.array_equal(frame, expected) for frame, expected in zip(sample.frames, before))
    assert any(not np.array_equal(source, rendered) for source, rendered in zip(sample.frames, proxy.frames))


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is required for proxy certification")
def test_certified_proxy_pixels_match_independent_same_filter_render(tmp_path: Path):
    from app.quality_moe.repair import build_ffmpeg_filter, render_recipe_proxy

    sample = gradient_sample()
    recipe = RepairRecipe(
        recipe_id="certified", exposure_ev=0.35, gamma=0.85,
        contrast=0.10, shadows=0.15, highlights=-0.15,
        white_balance=(1.08, 1.0, 0.92),
    )
    certified = render_recipe_proxy(sample, recipe)
    for index, frame in enumerate(sample.frames):
        assert cv2.imwrite(str(tmp_path / f"source-{index:03d}.png"), frame)
    command = [
        "ffmpeg", "-v", "error", "-y", "-framerate", "12", "-start_number", "0",
        "-i", str(tmp_path / "source-%03d.png"), "-frames:v", str(len(sample.frames)),
        "-vf", build_ffmpeg_filter(recipe, fps=12, max_width=sample.frames[0].shape[1]),
        str(tmp_path / "independent-%03d.png"),
    ]
    subprocess.run(command, check=True, capture_output=True)
    independent = tuple(
        cv2.imread(str(tmp_path / f"independent-{index + 1:03d}.png"), cv2.IMREAD_COLOR)
        for index in range(len(sample.frames))
    )

    assert all(frame is not None for frame in independent)
    assert all(np.max(np.abs(left.astype(np.int16) - right.astype(np.int16))) <= 1
               for left, right in zip(certified.frames, independent))


def test_exact_recipe_grid_contains_overexposure_and_opposite_white_balance_repairs():
    from app.quality_moe.repair import approved_recipes

    recipes = approved_recipes()

    assert len(recipes) == 12
    assert {recipe.exposure_ev for recipe in recipes} >= {-0.75, -0.35, 0.35, 0.75}
    assert {recipe.gamma for recipe in recipes} >= {0.85, 1.15}
    assert {recipe.contrast for recipe in recipes} >= {-0.10, 0.10}
    assert any(recipe.shadows == 0.15 for recipe in recipes)
    assert any(recipe.highlights == -0.15 for recipe in recipes)
    assert (1.08, 1.0, 0.92) in {recipe.white_balance for recipe in recipes}
    assert (0.92, 1.0, 1.08) in {recipe.white_balance for recipe in recipes}


def test_contact_sheets_do_not_overwrite_source_named_like_legacy_default(tmp_path: Path):
    from app.quality_moe.repair import search_repairs

    source = tmp_path / "original-contact-sheet.png"
    source.write_bytes(b"source-must-not-change")
    source_before = source.read_bytes()
    sample = gradient_sample(video_path=str(source))
    disabled = QualityMoeConfig.from_mapping(
        {"quality_moe": {"repairability": {"enabled": False}}}
    )
    search_repairs(sample, disabled, work_dir=tmp_path)

    assert source.read_bytes() == source_before
    names = {path.name for path in tmp_path.iterdir()}
    assert "original-contact-sheet.png" in names
    assert any(name.endswith("-original-contact-sheet.png") and name != source.name for name in names)


def test_contact_sheet_atomic_create_allows_one_concurrent_winner_without_overwrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    import app.quality_moe.repair as repair

    target = tmp_path / "contended-contact-sheet.png"
    monkeypatch.setattr(repair, "_contact_destination", lambda *_args, **_kwargs: target)
    first = gradient_sample()
    second = SampledClip(
        candidate_id=first.candidate_id,
        video_path=first.video_path,
        start_ts=first.start_ts,
        end_ts=first.end_ts,
        timestamps=first.timestamps,
        frames=tuple(np.full_like(frame, 255) for frame in first.frames),
    )
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def write(sample: SampledClip) -> None:
        try:
            barrier.wait()
            repair._save_contact_sheet(tmp_path, sample, kind="original", frames=sample.frames)
        except BaseException as exc:  # Thread assertion is checked below.
            errors.append(exc)

    workers = [threading.Thread(target=write, args=(sample,)) for sample in (first, second)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()

    assert target.is_file()
    assert len(errors) == 1
    assert isinstance(errors[0], FileExistsError)
    rendered = cv2.imread(str(target), cv2.IMREAD_COLOR)
    assert rendered is not None
    assert (
        rendered.mean() == pytest.approx(first.frames[0].mean(), abs=1.0)
        or rendered.mean() == pytest.approx(255.0, abs=1.0)
    )


def test_contact_sheet_target_is_never_visible_as_a_partial_file_during_slow_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    import app.quality_moe.repair as repair

    target = tmp_path / "observed-contact-sheet.png"
    monkeypatch.setattr(repair, "_contact_destination", lambda *_args, **_kwargs: target)
    original_write = repair.os.write
    write_started = threading.Event()
    release_write = threading.Event()
    writes = 0

    def slow_write(descriptor: int, content: bytes) -> int:
        nonlocal writes
        writes += 1
        if writes == 1:
            half = max(1, len(content) // 2)
            result = original_write(descriptor, content[:half])
            write_started.set()
            assert release_write.wait(timeout=2.0)
            return result
        return original_write(descriptor, content)

    monkeypatch.setattr(repair.os, "write", slow_write)
    errors: list[BaseException] = []
    worker = threading.Thread(
        target=lambda: _threaded_contact_write(repair, tmp_path, gradient_sample(), errors),
    )
    worker.start()
    assert write_started.wait(timeout=2.0)
    observed: list[bytes] = []
    for _ in range(25):
        if target.is_file():
            observed.append(target.read_bytes())
    release_write.set()
    worker.join()

    assert not errors
    assert observed == []
    assert cv2.imread(str(target), cv2.IMREAD_COLOR) is not None


def _threaded_contact_write(
    repair: object, work_dir: Path, sample: SampledClip, errors: list[BaseException],
) -> None:
    try:
        repair._save_contact_sheet(work_dir, sample, kind="original", frames=sample.frames)
    except BaseException as exc:
        errors.append(exc)


def test_identical_concurrent_contact_sheets_are_idempotent_successes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    import app.quality_moe.repair as repair

    target = tmp_path / "same-contact-sheet.png"
    monkeypatch.setattr(repair, "_contact_destination", lambda *_args, **_kwargs: target)
    sample = gradient_sample()
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def write() -> None:
        try:
            barrier.wait()
            repair._save_contact_sheet(tmp_path, sample, kind="original", frames=sample.frames)
        except BaseException as exc:
            errors.append(exc)

    workers = [threading.Thread(target=write) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()

    assert target.is_file()
    assert errors == []
    assert cv2.imread(str(target), cv2.IMREAD_COLOR) is not None


def test_fallback_publisher_ignores_stale_lock_file_from_dead_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    import app.quality_moe.repair as repair

    target = tmp_path / "fallback-contact-sheet.png"
    stale_lock = target.with_name(f".{target.name}.publish.lock")
    stale_lock.write_bytes(b"stale-owner")

    def unsupported_link(*_args, **_kwargs):
        raise OSError(errno.EOPNOTSUPP, "hard links unsupported")

    monkeypatch.setattr(repair.os, "link", unsupported_link)
    repair._write_new_file_or_reuse_identical(target, b"complete-image")

    assert target.read_bytes() == b"complete-image"


def test_best_contact_name_includes_config_recipe_and_proxy_hashes(tmp_path: Path):
    from app.quality_moe.repair import _contact_destination

    sample = gradient_sample()
    config = QualityMoeConfig.defaults()
    recipe = RepairRecipe(recipe_id="best", exposure_ev=0.35)
    proxy = gradient_sample(video_path="synthetic://proxy")
    destination = _contact_destination(
        tmp_path, sample, kind="best", config=config, recipe=recipe, proxy=proxy,
    )

    assert sample.input_hash[:12] in destination.name
    assert config.config_hash[:12] in destination.name
    assert recipe.recipe_hash[:12] in destination.name
    assert proxy.input_hash[:12] in destination.name


def test_ffmpeg_timeout_is_bounded_and_cannot_certify_recipe(
    monkeypatch: pytest.MonkeyPatch,
):
    import app.quality_moe.repair as repair

    called: dict[str, object] = {}

    def timed_out(*_args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        called.update(kwargs)
        raise subprocess.TimeoutExpired("ffmpeg", 1.0)

    monkeypatch.setattr(repair.subprocess, "run", timed_out)
    with pytest.raises(repair.RepairProxyRenderError, match="timed out"):
        repair.render_recipe_proxy(dark_sample(), RepairRecipe(recipe_id="timeout"), timeout_seconds=1.0)

    assert called["timeout"] == 1.0


def test_all_render_timeouts_are_structured_failures_not_evaluated_recipes(
    monkeypatch: pytest.MonkeyPatch,
):
    import app.quality_moe.repair as repair

    monkeypatch.setattr(
        repair,
        "render_recipe_proxy",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            repair.RepairProxyRenderError("timeout", "FFmpeg proxy render timed out.")
        ),
    )
    result = repair.search_repairs(dark_sample(), QualityMoeConfig.defaults())

    assert result.evaluated_recipes == ()
    assert len(result.render_failures) == 12
    assert result.unavailable_recipes == result.render_failures
    assert {failure.error_code for failure in result.render_failures} == {"timeout"}
    assert result.best_recipe is None
    assert result.repair_delta is None


def test_ffmpeg_filter_is_bounded_and_has_identical_palette_and_gif_prefix():
    from app.quality_moe.repair import build_ffmpeg_filter

    recipe = RepairRecipe(
        recipe_id="safe",
        exposure_ev=0.25,
        gamma=0.95,
        contrast=0.05,
        shadows=0.10,
        highlights=-0.10,
        white_balance=(1.02, 1.0, 0.98),
    )
    palette = build_ffmpeg_filter(recipe, fps=12, max_width=480)
    gif = build_ffmpeg_filter(recipe, fps=12, max_width=480)

    assert palette == gif
    assert palette.startswith("fps=12,")
    assert palette.endswith("scale=480:-1:flags=lanczos")
    with pytest.raises(ValueError, match="outside approved bounds"):
        build_ffmpeg_filter(RepairRecipe(recipe_id="unsafe", exposure_ev=1.0), fps=12, max_width=480)


@pytest.mark.parametrize("fps,max_width", [(0, 480), (12, 0), (True, 480)])
def test_ffmpeg_filter_rejects_invalid_render_dimensions(fps: int, max_width: int):
    from app.quality_moe.repair import build_ffmpeg_filter

    with pytest.raises(ValueError):
        build_ffmpeg_filter(None, fps=fps, max_width=max_width)
