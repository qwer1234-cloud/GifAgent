"""Shared GIF palette filter construction.

Direct and staged exports must emit byte-identical FFmpeg commands for the
same frozen config, so both paths build their ``palettegen`` /
``paletteuse`` fragments here.

Every value is whitelisted: these strings are interpolated straight into a
filtergraph, where ``,`` and ``;`` separate filters, so an unvalidated
config value would let a snapshot inject arbitrary filters.
"""

from __future__ import annotations

# FFmpeg's own defaults.  When a fragment matches them entirely we emit the
# bare filter name so default configs keep producing the exact command
# arrays this pipeline has always produced.  These are the single source of
# truth for the corresponding ``extract_config()`` fallbacks.
DEFAULT_STATS_MODE = "full"
DEFAULT_DITHER = "sierra2_4a"
DEFAULT_DIFF_MODE = "none"

_STATS_MODES = frozenset({"full", "diff", "single"})
_DITHERS = frozenset({"none", "bayer", "floyd_steinberg", "sierra2_4a"})
_DIFF_MODES = frozenset({"none", "rectangle"})

# GIF stores each frame's delay in centiseconds, so a frame rate that does
# not divide 100 cannot be represented exactly and the encoder silently
# rounds, producing uneven playback.
_GIF_DELAY_BASE = 100
_DIVISIBLE_FPS = tuple(
    fps for fps in range(1, _GIF_DELAY_BASE + 1) if _GIF_DELAY_BASE % fps == 0
)


def _validated(value: object, allowed: frozenset[str], name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string, got {type(value).__name__}")
    text = value.strip()
    if text not in allowed:
        raise ValueError(
            f"{name} must be one of {sorted(allowed)}, got {value!r}"
        )
    return text


def build_palette_filters(
    *,
    stats_mode: str,
    dither: str,
    diff_mode: str,
) -> tuple[str, str]:
    """Return the ``palettegen`` and ``paletteuse`` filtergraph fragments.

    Raises ``ValueError`` for any value outside the whitelist.
    """
    stats = _validated(stats_mode, _STATS_MODES, "stats_mode")
    dither_value = _validated(dither, _DITHERS, "dither")
    diff = _validated(diff_mode, _DIFF_MODES, "diff_mode")

    palettegen = (
        "palettegen"
        if stats == DEFAULT_STATS_MODE
        else f"palettegen=stats_mode={stats}"
    )
    if dither_value == DEFAULT_DITHER and diff == DEFAULT_DIFF_MODE:
        paletteuse = "paletteuse"
    else:
        paletteuse = f"paletteuse=dither={dither_value}:diff_mode={diff}"
    return palettegen, paletteuse


def is_divisible_gif_fps(fps: object) -> bool:
    """Return True when *fps* maps onto an exact centisecond frame delay."""
    if isinstance(fps, bool) or not isinstance(fps, int):
        return False
    if fps < 1 or fps > _GIF_DELAY_BASE:
        return False
    return _GIF_DELAY_BASE % fps == 0


def nearest_divisible_gif_fps(fps: object) -> list[int]:
    """Return the divisible frame rates bracketing *fps*.

    An already-divisible rate returns just itself, so callers can compare
    the result against ``[fps]`` to decide whether to warn.
    """
    if is_divisible_gif_fps(fps):
        return [int(fps)]  # type: ignore[arg-type]
    try:
        target = float(fps)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return list(_DIVISIBLE_FPS)
    lower = [c for c in _DIVISIBLE_FPS if c < target]
    upper = [c for c in _DIVISIBLE_FPS if c > target]
    nearest: list[int] = []
    if lower:
        nearest.append(lower[-1])
    if upper:
        nearest.append(upper[0])
    return nearest or list(_DIVISIBLE_FPS)
