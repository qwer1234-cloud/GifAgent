"""W4 harness self-tests.

Proves the two hard gates of the mutation harness:

1. Sentinel — a known-killable mutant (``return 1`` -> ``return 0``) is killed
   by a pytest subprocess that demonstrably imports the SANDBOX copy (evidence
   written from inside the test run: ``imported_from.json`` records
   ``__file__`` of ``sentinel_mod``/``app``/``app.services.clip_merge``).
2. Engine freeze — engine dispatch follows the canonical manifest:
   ``cosmic_ray.status == "fallback"`` + ``--engine cosmic-ray`` must be
   refused with exit code 2 (never silently switched).

The ast fallback engine's mutant generation is exercised on a synthetic
snippet without subprocesses.
"""

from __future__ import annotations

import json
from pathlib import Path

import scripts.token_sprint.mutation_harness as mh

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Sentinel: pytest imports the copy, not the worktree module
# ---------------------------------------------------------------------------


def test_sentinel_mutant_killed_and_pytest_imports_sandbox_copy(tmp_path):
    result = mh.run_sentinel(
        repo_root=REPO_ROOT, sandbox_root=tmp_path / "sandbox", timeout=180
    )

    assert result["baseline_passed"] is True
    assert result["sentinel_killed"] is True

    sandbox = Path(result["sandbox"]).resolve()
    for key in ("sentinel_mod", "app", "clip_merge"):
        recorded = Path(result["mutant_imported_from"][key]).resolve()
        assert recorded.is_relative_to(sandbox), (
            f"pytest imported {key} from {recorded} instead of the sandbox copy"
        )
        assert recorded.is_file()
    # The mutated sentinel module itself is the sandbox file that was imported.
    assert Path(result["mutant_imported_from"]["sentinel_mod"]).resolve() == (
        sandbox / "sentinel_mod.py"
    )
    boot_app = Path(result["boot_recorded_app_file"]).resolve()
    assert boot_app.is_relative_to(sandbox)


# ---------------------------------------------------------------------------
# Engine dispatch frozen by canonical manifest
# ---------------------------------------------------------------------------


def _write_manifest(root: Path, payload: dict) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / "sprint_manifest.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_cosmic_ray_engine_refused_when_manifest_froze_ast_fallback(tmp_path, capsys):
    _write_manifest(
        tmp_path,
        {"schema": "gifagent.token_sprint.manifest.v1", "cosmic_ray": {"status": "fallback"}},
    )
    rc = mh.main(
        [
            "--sprint-root", str(tmp_path),
            "--engine", "cosmic-ray",
            "--module", "clip_merge",
        ]
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "refusing --engine cosmic-ray" in err


def test_cosmic_ray_engine_refused_when_manifest_status_missing(tmp_path):
    _write_manifest(tmp_path, {"schema": "gifagent.token_sprint.manifest.v1"})
    rc = mh.main(
        [
            "--sprint-root", str(tmp_path),
            "--engine", "cosmic-ray",
            "--module", "clip_merge",
        ]
    )
    assert rc == 2


def test_default_engine_resolved_from_manifest(tmp_path):
    _write_manifest(
        tmp_path,
        {
            "schema": "gifagent.token_sprint.manifest.v1",
            "cosmic_ray": {"status": "included"},
            "vector_math_in_baseline": True,
        },
    )
    manifest = mh.load_manifest(tmp_path)
    assert mh.resolve_engine(None, manifest) == "cosmic-ray"
    assert mh.resolve_engine("ast", manifest) == "ast"  # fallback entry stays usable

    _write_manifest(
        tmp_path,
        {
            "schema": "gifagent.token_sprint.manifest.v1",
            "cosmic_ray": {"status": "fallback"},
        },
    )
    manifest = mh.load_manifest(tmp_path)
    assert mh.resolve_engine(None, manifest) == "ast"


# ---------------------------------------------------------------------------
# Shared-root passing protocol
# ---------------------------------------------------------------------------


def test_sprint_root_required_and_env_fallback(tmp_path, monkeypatch):
    monkeypatch.delenv("GIFAGENT_SPRINT_ROOT", raising=False)
    try:
        mh.resolve_sprint_root(None)
    except mh.ProtocolError as exc:
        assert "missing --sprint-root" in str(exc)
    else:
        raise AssertionError("missing sprint root must raise ProtocolError")

    rc = mh.main(["--engine", "ast", "--module", "clip_merge"])
    assert rc == 2

    monkeypatch.setenv("GIFAGENT_SPRINT_ROOT", str(tmp_path))
    assert mh.resolve_sprint_root(None) == tmp_path.resolve()

    rc = mh.main(["--sprint-root", str(tmp_path / "does-not-exist"), "--list-modules"])
    assert rc == 2


def test_vector_math_gated_by_manifest_flag(tmp_path):
    with_vector = {
        "cosmic_ray": {"status": "included"},
        "vector_math_in_baseline": True,
    }
    without_vector = {
        "cosmic_ray": {"status": "included"},
        "vector_math_in_baseline": False,
    }
    assert "vector_math" in mh.eligible_modules(with_vector)
    assert "vector_math" not in mh.eligible_modules(without_vector)
    assert "clip_merge" in mh.eligible_modules(without_vector)


# ---------------------------------------------------------------------------
# AST fallback engine mutant generation (no subprocess)
# ---------------------------------------------------------------------------


def test_ast_engine_generates_expected_operator_families():
    source = (
        "def decide(a, b, flag):\n"
        "    if a < b:\n"
        "        total = a * 2\n"
        "    else:\n"
        "        total = b\n"
        "    if flag and total > 0:\n"
        "        return -total\n"
        "    return total\n"
    )
    baseline, mutants = mh.generate_mutants(source, "snippet")

    assert baseline != source  # canonical unparse form
    kinds = {m.kind for m in mutants}
    assert "ROR" in kinds   # a < b, total > 0
    assert "AOR" in kinds   # a * 2 (numeric constant operand)
    assert "LCR" in kinds   # flag and total > 0
    assert "UOI_del" in kinds and "UOI_flip" in kinds  # -total

    for mutant in mutants:
        assert mutant.source != baseline
        compile(mutant.source, "<mutant>", "exec")  # every mutant must parse
        assert mutant.mutant_id.startswith("mut_snippet_")
        assert mutant.lineno >= 1


def test_ast_engine_skips_string_addition():
    source = 'def greet(name):\n    return "hello, " + name\n'
    _, mutants = mh.generate_mutants(source, "snippet")
    assert all(mutant.kind != "AOR" for mutant in mutants), (
        "string concatenation must not be mutated (SQL/string safety rule)"
    )
