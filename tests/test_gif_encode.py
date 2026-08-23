"""Tests for shared GIF palette filter construction."""

import pytest

from app.services.gif_encode import (
    build_palette_filters,
    is_divisible_gif_fps,
    nearest_divisible_gif_fps,
)


def test_defaults_reproduce_current_commands():
    gen, use = build_palette_filters(
        stats_mode="full", dither="sierra2_4a", diff_mode="none"
    )

    assert gen == "palettegen"
    assert use == "paletteuse"


def test_diff_mode_and_stats_mode_are_emitted():
    gen, use = build_palette_filters(
        stats_mode="diff", dither="sierra2_4a", diff_mode="rectangle"
    )

    assert gen == "palettegen=stats_mode=diff"
    assert use == "paletteuse=dither=sierra2_4a:diff_mode=rectangle"


def test_a_non_default_dither_is_emitted_on_its_own():
    gen, use = build_palette_filters(
        stats_mode="full", dither="bayer", diff_mode="none"
    )

    assert gen == "palettegen"
    assert use == "paletteuse=dither=bayer:diff_mode=none"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"stats_mode": "; rm -rf /", "dither": "sierra2_4a", "diff_mode": "none"},
        {"stats_mode": "full", "dither": "; rm -rf /", "diff_mode": "none"},
        {"stats_mode": "full", "dither": "sierra2_4a", "diff_mode": "drop table"},
        {"stats_mode": "", "dither": "sierra2_4a", "diff_mode": "none"},
        {"stats_mode": None, "dither": "sierra2_4a", "diff_mode": "none"},
    ],
)
def test_unknown_value_is_rejected(kwargs):
    with pytest.raises(ValueError):
        build_palette_filters(**kwargs)


def test_no_filter_fragment_can_smuggle_a_separator():
    gen, use = build_palette_filters(
        stats_mode="single", dither="floyd_steinberg", diff_mode="rectangle"
    )

    for fragment in (gen, use):
        assert "," not in fragment
        assert ";" not in fragment
        assert " " not in fragment


@pytest.mark.parametrize("fps", [25, 20, 10, 50, 4, 5, 2, 1, 100])
def test_divisible_frame_rates_are_accepted(fps):
    assert is_divisible_gif_fps(fps) is True


@pytest.mark.parametrize("fps", [24, 30, 15, 12, 23, 60])
def test_non_divisible_frame_rates_are_flagged(fps):
    assert is_divisible_gif_fps(fps) is False


@pytest.mark.parametrize("fps", [0, -1, None, "25", 24.5, True])
def test_invalid_frame_rates_are_not_divisible(fps):
    assert is_divisible_gif_fps(fps) is False


def test_nearest_divisible_brackets_the_requested_rate():
    assert nearest_divisible_gif_fps(24) == [20, 25]
    assert nearest_divisible_gif_fps(30) == [25, 50]
    assert nearest_divisible_gif_fps(15) == [10, 20]


def test_nearest_divisible_of_an_already_valid_rate_is_itself():
    assert nearest_divisible_gif_fps(25) == [25]
