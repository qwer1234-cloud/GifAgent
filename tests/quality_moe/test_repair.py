from __future__ import annotations

import inspect
from pathlib import Path
import shutil
import subprocess

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
