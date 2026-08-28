"""Shared hypothesis configuration for the token-sprint W5 property suite.

Design constraints (token sprint, W5):
- ``deadline=None`` (no flaky timing failures on a loaded CI box);
- explicit seed (each test module re-locks the seed with ``@seed(20260828)``;
  the ``settings(seed=...)`` kwarg was removed in recent hypothesis releases,
  so the ``hypothesis.seed`` decorator is the explicit-seed mechanism here);
- ``max_examples=80`` keeps the whole directory's wall-clock increment under
  the 60s acceptance gate;
- ``HealthCheck.too_slow`` is suppressed for the same reason.

This directory intentionally has no ``__init__.py`` (W5 File Map), so pytest
prepends ``tests/property`` — not the repo root — when importing these test
modules.  Bootstrap the repo root here so ``app.*`` / ``scripts.*`` imports
also work when only ``tests/property`` is selected.
"""

from __future__ import annotations

import sys
from pathlib import Path

from hypothesis import HealthCheck, settings

_REPO_ROOT = str(Path(__file__).resolve().parents[2])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

settings.register_profile(
    "token_sprint",
    deadline=None,
    max_examples=80,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.load_profile("token_sprint")
