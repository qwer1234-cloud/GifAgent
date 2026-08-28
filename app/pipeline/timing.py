"""Stage timing collection shared by every app.pipeline module."""
from __future__ import annotations

import functools

from app.services.stage_timing import StageTimings


# ---------------------------------------------------------------------------
# Timing instrumentation.
#
# Each stage runs in its own subprocess, so a module-level collector maps
# one-to-one onto one stage's work.  ``reset_timings()`` exists for the
# in-process test harnesses that drive several stages back to back.
# ---------------------------------------------------------------------------

_TIMINGS = StageTimings()


def reset_timings() -> StageTimings:
    """Start a fresh collection window and return the active collector."""
    global _TIMINGS
    _TIMINGS = StageTimings()
    return _TIMINGS


def current_timings() -> StageTimings:
    """Return the collector accumulating this process's samples."""
    return _TIMINGS


def _timed(metric: str):
    """Record every call to the decorated function under *metric*."""

    def decorate(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            with _TIMINGS.span(metric):
                return fn(*args, **kwargs)

        return wrapper

    return decorate


def _attach_timings(manifest: dict) -> dict:
    """Attach the collected timings to *manifest* when anything was measured.

    The key is additive: manifests written before this existed, and stages
    that measure nothing, simply carry no ``timings``.
    """
    payload = _TIMINGS.to_dict()
    if payload:
        manifest["timings"] = payload
    return manifest

