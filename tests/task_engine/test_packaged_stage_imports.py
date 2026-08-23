"""Packaged stage-mode dependency closure tests.

The frozen launcher executes bundled ``scripts/`` files through ``runpy``
(``--run-script``), so PyInstaller never statically analyzes those files.
``build_exe.spec`` must therefore list the stage script's direct service
imports explicitly.
"""

from __future__ import annotations

from pathlib import Path

_SPEC_PATH = Path(__file__).resolve().parents[2] / "build_exe.spec"

_STAGE_DIRECT_IMPORTS = [
    "app.services.clip_merge",
    "app.services.action_pipeline",
    "app.services.export_ranking",
    "app.services.gif_windows",
    "app.services.stage_timing",
    "app.services.transition_candidates",
    "app.services.transition_guard",
    "app.quality_moe",
    "app.quality_moe.config",
    "app.quality_moe.evaluator",
    "app.quality_moe.experts",
    "app.quality_moe.judge",
    "app.quality_moe.models",
    "app.quality_moe.policy",
    "app.quality_moe.repair",
    "app.quality_moe.sampling",
]


def _explicit_hiddenimports() -> set[str]:
    """Parse the literal ``hiddenimports += [...]`` list in the spec."""
    lines = _SPEC_PATH.read_text(encoding="utf-8").splitlines()
    in_list = False
    names: set[str] = set()
    for line in lines:
        stripped = line.strip()
        if stripped == "hiddenimports += [":
            in_list = True
            continue
        if in_list:
            if stripped == "]":
                break
            if len(stripped) >= 3 and stripped.startswith('"'):
                names.add(stripped[1:-2])
            elif len(stripped) >= 3 and stripped.startswith("'"):
                names.add(stripped[1:-2])
    return names


def test_spec_explicitly_includes_all_stage_script_imports():
    explicit = _explicit_hiddenimports()
    for module in _STAGE_DIRECT_IMPORTS:
        assert module in explicit, (
            f"build_exe.spec must explicitly list {module!r} "
            "for the packaged stage-mode script"
        )
