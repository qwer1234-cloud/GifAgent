from __future__ import annotations

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


def test_repair_rejects_temporal_regression_even_when_photometric_score_improves():
    from app.quality_moe.repair import search_repairs

    sample = dark_sample()
    result = search_repairs(
        sample,
        QualityMoeConfig.defaults(),
        transform=lambda frame, _recipe, index: np.full_like(frame, 255 if index % 2 else 0),
    )

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
