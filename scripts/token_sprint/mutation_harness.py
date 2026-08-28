"""W4 mutation testing harness (token sprint workstream W4, test-only).

Two engines, both operating ONLY on a throwaway sandbox copy of the repo
(``app/`` + ``tests/`` trees copied into a temp directory). The worktree is
never modified in place.

Engines
-------
* ``cosmic-ray`` — the T-1 frozen tool path (canonical manifest
  ``cosmic_ray.status == "included"``). Delegates mutant generation and
  execution to the installed cosmic-ray CLI (8.7.0) run with cwd = sandbox so
  that mutations are applied to the sandbox copy on disk only.
* ``ast`` — stdlib-only fallback engine implementing four operator families
  (AOR rotation, ROR rotation, UOI delete/flip, LCR swap) per the W4 plan.

Import proof
------------
Every pytest invocation runs with cwd = sandbox and a boot plugin
(``mutation_boot``) that fails the run unless ``app`` resolves inside the
sandbox, recording ``app.__file__`` to a file as evidence. The sentinel flow
(``--dry-run-sentinel``) additionally proves end-to-end that a known-killable
mutant is killed while pytest imports the sandbox copy (writes
``imported_from.json`` from inside the test run).

Protocol
--------
* ``--sprint-root`` required; env ``GIFAGENT_SPRINT_ROOT`` only as explicit
  fallback; missing/invalid → exit code 2.
* Engine dispatch gated by canonical manifest ``cosmic_ray.status``:
  ``fallback`` + ``--engine cosmic-ray`` → exit code 2 (never silently switch).
* Score JSON artifacts are written atomically under
  ``<sprint-root>/w4/mutation_scores/<engine>/<module>.json``.

Exit codes: 0 success, 1 harness/test-run failure, 2 protocol error.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# ---------------------------------------------------------------------------
# Module whitelist (only modules present in sprint_base_sha; vector_math is
# additionally gated by canonical manifest key `vector_math_in_baseline`).
# ---------------------------------------------------------------------------

MODULES: dict[str, str] = {
    "clip_merge": "app/services/clip_merge.py",
    "export_ranking": "app/services/export_ranking.py",
    "grid_select": "app/services/grid_select.py",
    "timeline": "app/services/timeline.py",
    "taste_map": "app/services/taste_map.py",
    "narrative_curation": "app/services/narrative_curation.py",
    "collections": "app/services/collections.py",
    "desktop_export_sync": "app/services/desktop_export_sync.py",
    "metrics": "app/quality_lab/metrics.py",
    "calibration": "app/quality_lab/calibration.py",
    "promotion": "app/quality_lab/promotion.py",
    "repository": "app/task_engine/repository.py",
    "fingerprints": "app/task_engine/fingerprints.py",
    "vector_math": "app/services/vector_math.py",
}

# Existing tests (run-only; never modified). Kill tests added later under
# tests/mutation/test_mut_{module}.py are picked up automatically.
DEFAULT_TESTS: dict[str, list[str]] = {
    "clip_merge": ["tests/test_clip_merge.py"],
    "export_ranking": ["tests/test_export_ranking.py"],
    "grid_select": ["tests/test_grid_select.py"],
    "timeline": ["tests/test_timeline.py"],
    "taste_map": ["tests/test_creative_curation.py"],
    "narrative_curation": ["tests/test_creative_curation.py"],
    "collections": ["tests/test_collections.py"],
    "desktop_export_sync": ["tests/test_desktop_export_sync.py"],
    "metrics": ["tests/quality_lab/test_metrics.py"],
    "calibration": ["tests/quality_lab/test_calibration.py"],
    "promotion": ["tests/quality_lab/test_promotion.py"],
    "repository": ["tests/task_engine/test_repository.py"],
    "fingerprints": ["tests/test_adaptive_config.py"],
    "vector_math": [],
}

# Plan rule: desktop_export_sync contains threads/scheduling; kill subprocess
# after 60s and count the timeout as survived (registered for W7).
DEFAULT_TIMEOUTS: dict[str, int] = {"desktop_export_sync": 60}
DEFAULT_TIMEOUT = 30

MEDIA_SUFFIXES = {
    ".gif", ".jpg", ".jpeg", ".png", ".bmp", ".webp", ".ico",
    ".mp4", ".webm", ".mov", ".avi", ".mkv", ".mp3", ".wav",
    ".db", ".sqlite", ".sqlite3", ".zip", ".tar", ".gz", ".exe",
}

# ---------------------------------------------------------------------------
# Errors and small helpers
# ---------------------------------------------------------------------------


class ProtocolError(Exception):
    """Usage/protocol violation → exit code 2."""


class HarnessFailure(Exception):
    """Harness or baseline failure → exit code 1."""


def resolve_sprint_root(explicit: str | None) -> Path:
    """Shared-root passing protocol: CLI arg first, env as explicit fallback."""
    raw = explicit or os.environ.get("GIFAGENT_SPRINT_ROOT")
    if not raw:
        raise ProtocolError(
            "missing --sprint-root (and GIFAGENT_SPRINT_ROOT env fallback is not set)"
        )
    root = Path(raw).resolve()
    if not root.is_dir():
        raise ProtocolError(f"sprint root does not exist: {root}")
    return root


def load_manifest(sprint_root: Path) -> dict:
    manifest_path = sprint_root / "sprint_manifest.json"
    if not manifest_path.is_file():
        raise ProtocolError(f"canonical manifest not found: {manifest_path}")
    with manifest_path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def cosmic_ray_status(manifest: dict) -> str | None:
    status = manifest.get("cosmic_ray", {}).get("status")
    return status if isinstance(status, str) else None


def eligible_modules(manifest: dict) -> list[str]:
    modules = list(MODULES)
    if manifest.get("vector_math_in_baseline") is not True:
        modules.remove("vector_math")
    return modules


def resolve_engine(explicit: str | None, manifest: dict) -> str:
    status = cosmic_ray_status(manifest)
    if explicit is not None:
        if explicit == "cosmic-ray" and status != "included":
            raise ProtocolError(
                f"refusing --engine cosmic-ray: T-1 froze cosmic_ray.status="
                f"{status!r} (ast fallback); do not re-spike or uv add"
            )
        return explicit
    if status == "included":
        return "cosmic-ray"
    if status == "fallback":
        return "ast"
    raise ProtocolError(
        f"manifest cosmic_ray.status missing/invalid ({status!r}); pass --engine explicitly"
    )


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Atomic publish: temp file in the SAME directory, then os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    tmp.write_bytes(data)
    os.replace(tmp, path)


def atomic_write_json(path: Path, payload: dict) -> None:
    atomic_write_bytes(
        path, json.dumps(payload, indent=2, ensure_ascii=True, sort_keys=True).encode("utf-8")
    )


def sha256_of(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Sandbox construction (mutation target copies)
# ---------------------------------------------------------------------------

MUTATION_BOOT_PLUGIN = '''"""W4 boot plugin: hard-prove that `app` resolves inside the sandbox."""
import os
import pathlib

_app = __import__("app")
_here = pathlib.Path(__file__).resolve().parent
_app_file = pathlib.Path(_app.__file__).resolve()

if not _app_file.is_relative_to(_here):
    raise ImportError(
        "W4 mutation sandbox violation: app resolved to %s, expected under %s"
        % (_app_file, _here)
    )

_record = os.environ.get("W4_MUTATION_BOOT_RECORD")
if _record:
    pathlib.Path(_record).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(_record).write_text(str(_app_file), encoding="utf-8")
'''


def _ignore_app_tree(directory: str, names: list[str]) -> set[str]:
    ignored: set[str] = set()
    for name in names:
        entry = Path(directory) / name
        if entry.is_file() and not name.endswith(".py"):
            ignored.add(name)
    return ignored


def _ignore_tests_tree(directory: str, names: list[str]) -> set[str]:
    ignored: set[str] = set()
    for name in names:
        entry = Path(directory) / name
        if entry.is_dir():
            if name == "__pycache__":
                ignored.add(name)
        elif entry.suffix.lower() in MEDIA_SUFFIXES:
            ignored.add(name)
    return ignored


def create_sandbox(repo_root: Path, dest: Path) -> Path:
    """Copy app/ (py only), scripts/ (py only) and tests/ (non-media) into
    dest; add boot plugin. scripts/ is needed because some existing tests
    import the sanctioned facade ``scripts.test_video_adaptive``."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    shutil.copytree(
        repo_root / "app", dest / "app", ignore=_ignore_app_tree, dirs_exist_ok=True
    )
    if (repo_root / "scripts").is_dir():
        shutil.copytree(
            repo_root / "scripts",
            dest / "scripts",
            ignore=_ignore_app_tree,
            dirs_exist_ok=True,
        )
    if (repo_root / "configs").is_dir():
        shutil.copytree(
            repo_root / "configs",
            dest / "configs",
            ignore=_ignore_tests_tree,
            dirs_exist_ok=True,
        )
    if (repo_root / "tests").is_dir():
        shutil.copytree(
            repo_root / "tests", dest / "tests", ignore=_ignore_tests_tree, dirs_exist_ok=True
        )
    (dest / "mutation_boot.py").write_text(MUTATION_BOOT_PLUGIN, encoding="utf-8")
    return dest


def test_paths_for(module: str, repo_root: Path) -> list[str]:
    paths = list(DEFAULT_TESTS.get(module, []))
    kill_tests = f"tests/mutation/test_mut_{module}.py"
    if (repo_root / kill_tests).is_file():
        paths.append(kill_tests)
    if not paths:
        raise HarnessFailure(
            f"no test paths available for module {module!r}; refusing to run mutations"
        )
    for rel in paths:
        if not (repo_root / rel).is_file():
            raise HarnessFailure(f"declared test path does not exist: {rel}")
    return paths


def pytest_env(sandbox: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPATH"] = sandbox.as_posix() + os.pathsep + env.get("PYTHONPATH", "")
    env["W4_MUTATION_BOOT_RECORD"] = (sandbox / "imported_from_boot.txt").as_posix()
    return env


def pytest_command_args(test_paths: list[str]) -> list[str]:
    return [
        sys.executable, "-m", "pytest",
        "-x", "-q", "--no-header", "-p", "no:cacheprovider", "-p", "mutation_boot",
        *test_paths,
    ]


def run_pytest_in_sandbox(
    sandbox: Path, test_paths: list[str], timeout: int
) -> tuple[str, str]:
    """Run pytest inside the sandbox. Returns (outcome, output).

    outcome: "passed" (rc 0), "failed" (rc 1, i.e. mutant killed), "timeout".
    Any other exit code is a harness error.
    """
    cmd = pytest_command_args(test_paths)
    env = pytest_env(sandbox)
    try:
        proc = subprocess.run(
            cmd, cwd=sandbox.as_posix(), env=env, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return "timeout", ""
    output = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode == 0:
        return "passed", output
    if proc.returncode == 1:
        return "failed", output
    raise HarnessFailure(
        f"pytest exited with unexpected code {proc.returncode}\n{output[-4000:]}"
    )


def assert_boot_record_under_sandbox(sandbox: Path) -> str:
    record = sandbox / "imported_from_boot.txt"
    if not record.is_file():
        raise HarnessFailure("mutation boot record missing; sandbox import unproven")
    recorded = record.read_text(encoding="utf-8").strip()
    resolved = Path(recorded).resolve()
    if not resolved.is_relative_to(sandbox.resolve()):
        raise HarnessFailure(
            f"pytest imported app from outside the sandbox: {recorded}"
        )
    return recorded


# ---------------------------------------------------------------------------
# Sentinel: prove pytest imports the sandbox copy and kills a known mutant
# ---------------------------------------------------------------------------

SENTINEL_MODULE = "def sentinel_one():\n    return 1\n"

SENTINEL_TEST = '''"""Generated by W4 mutation harness; lives only inside the sandbox."""
import json
import os
import sys

import sentinel_mod
import app
import app.services.clip_merge


def test_sentinel_and_import_provenance():
    record = os.environ["W4_SENTINEL_RECORD"]
    payload = {
        "sentinel_mod": sentinel_mod.__file__,
        "app": sys.modules["app"].__file__,
        "clip_merge": sys.modules["app.services.clip_merge"].__file__,
    }
    with open(record, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    assert sentinel_mod.sentinel_one() == 1
'''


def _assert_under(recorded: dict, sandbox: Path) -> None:
    resolved_sandbox = sandbox.resolve()
    for key, value in recorded.items():
        if not Path(value).resolve().is_relative_to(resolved_sandbox):
            raise HarnessFailure(
                f"sentinel import proof failed: {key} resolved to {value} "
                f"(outside sandbox {resolved_sandbox})"
            )


def run_sentinel(repo_root: Path, sandbox_root: Path, timeout: int = 180) -> dict:
    """Run the sentinel proof. Mutates return 1 -> return 0 in the sandbox copy."""
    sandbox = create_sandbox(repo_root, sandbox_root)
    (sandbox / "sentinel_mod.py").write_text(SENTINEL_MODULE, encoding="utf-8")
    (sandbox / "tests" / "test_w4_sentinel_probe.py").write_text(
        SENTINEL_TEST, encoding="utf-8"
    )
    record = sandbox / "imported_from.json"
    env = pytest_env(sandbox)
    env["W4_SENTINEL_RECORD"] = record.as_posix()

    test_paths = ["tests/test_w4_sentinel_probe.py"]
    cmd = pytest_command_args(test_paths)

    # Baseline: unmutated sentinel must pass and imports must land in sandbox.
    baseline = subprocess.run(
        cmd, cwd=sandbox.as_posix(), env=env, capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=timeout,
    )
    baseline_output = (baseline.stdout or "") + (baseline.stderr or "")
    if baseline.returncode != 0:
        raise HarnessFailure(
            f"sentinel baseline failed (rc={baseline.returncode})\n{baseline_output[-4000:]}"
        )
    if not record.is_file():
        raise HarnessFailure("sentinel baseline did not write imported_from.json")
    imported_baseline = json.loads(record.read_text(encoding="utf-8"))
    _assert_under(imported_baseline, sandbox)
    boot_recorded = assert_boot_record_under_sandbox(sandbox)

    # Mutant: return 1 -> return 0 inside the SANDBOX copy only.
    sentinel_path = sandbox / "sentinel_mod.py"
    mutated = SENTINEL_MODULE.replace("return 1", "return 0")
    if mutated == SENTINEL_MODULE:
        raise HarnessFailure("sentinel mutation did not apply")
    sentinel_path.write_text(mutated, encoding="utf-8")

    mutant = subprocess.run(
        cmd, cwd=sandbox.as_posix(), env=env, capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=timeout,
    )
    mutant_output = (mutant.stdout or "") + (mutant.stderr or "")
    killed = mutant.returncode != 0
    if not killed:
        raise HarnessFailure(
            f"sentinel mutant SURVIVED (pytest rc={mutant.returncode}) — "
            "pytest is not importing the mutated copy; harness must not continue\n"
            f"{mutant_output[-4000:]}"
        )
    if not record.is_file():
        raise HarnessFailure("sentinel mutant run did not write imported_from.json")
    imported_mutant = json.loads(record.read_text(encoding="utf-8"))
    _assert_under(imported_mutant, sandbox)

    return {
        "sentinel_killed": killed,
        "baseline_passed": True,
        "baseline_imported_from": imported_baseline,
        "mutant_imported_from": imported_mutant,
        "boot_recorded_app_file": boot_recorded,
        "sandbox": sandbox.as_posix(),
    }


# ---------------------------------------------------------------------------
# AST fallback engine: four operator families, single-point mutants
# ---------------------------------------------------------------------------

AOR_CYCLE = [ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod]
ROR_CYCLE = [ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE]


def _has_string_constant(node: ast.AST) -> bool:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, (str, bytes)):
            return True
        if isinstance(sub, ast.JoinedStr):
            return True
    return False


def _is_numeric_constant(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        value = node.value
        if isinstance(value, bool):
            return False
        return isinstance(value, (int, float, complex))
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return (
            isinstance(node.operand, ast.Constant)
            and isinstance(node.operand.value, (int, float))
            and not isinstance(node.operand.value, bool)
        )
    return False


def _aor_allowed(node: ast.BinOp) -> bool:
    """Conservative string/SQL safety: prefer missing a mutant over mutating
    string concatenation or path joins (plan rule for the fallback engine)."""
    if _has_string_constant(node):
        return False
    if isinstance(node.op, ast.Add):
        # Static string/numeric ambiguity: only mutate when at least one side
        # is an obviously numeric literal.
        return _is_numeric_constant(node.left) or _is_numeric_constant(node.right)
    return True


@dataclass
class Mutant:
    mutant_id: str
    kind: str
    lineno: int
    col: int
    source: str
    node_src: str = ""
    mutated_node_src: str = ""


@dataclass
class Site:
    kind: str
    lineno: int
    col: int
    op_index: int = 0
    node_src: str = ""


class _SiteCollector(ast.NodeVisitor):
    """Enumerate mutation sites in the same order the mutator applies them."""

    def __init__(self) -> None:
        self.sites: list[Site] = []

    def visit_BinOp(self, node: ast.BinOp) -> None:
        self.generic_visit(node)
        if isinstance(node.op, tuple(AOR_CYCLE)) and _aor_allowed(node):
            self.sites.append(
                Site(
                    kind="AOR",
                    lineno=node.lineno,
                    col=node.col_offset,
                    node_src=ast.unparse(node),
                )
            )

    def visit_Compare(self, node: ast.Compare) -> None:
        self.generic_visit(node)
        for index, op in enumerate(node.ops):
            if isinstance(op, tuple(ROR_CYCLE)):
                self.sites.append(
                    Site(
                        kind="ROR",
                        lineno=node.lineno,
                        col=node.col_offset,
                        op_index=index,
                        node_src=ast.unparse(node),
                    )
                )

    def visit_UnaryOp(self, node: ast.UnaryOp) -> None:
        self.generic_visit(node)
        if isinstance(node.op, (ast.Not, ast.USub)):
            self.sites.append(
                Site(
                    kind="UOI_del",
                    lineno=node.lineno,
                    col=node.col_offset,
                    node_src=ast.unparse(node),
                )
            )
            self.sites.append(
                Site(
                    kind="UOI_flip",
                    lineno=node.lineno,
                    col=node.col_offset,
                    node_src=ast.unparse(node),
                )
            )

    def visit_BoolOp(self, node: ast.BoolOp) -> None:
        self.generic_visit(node)
        self.sites.append(
            Site(
                kind="LCR",
                lineno=node.lineno,
                col=node.col_offset,
                node_src=ast.unparse(node),
            )
        )


class _Mutator(ast.NodeTransformer):
    """Apply exactly one mutation: the `occurrence`-th site of the given kind."""

    def __init__(self, kind: str, occurrence: int, op_index: int = 0) -> None:
        self.kind = kind
        self.occurrence = occurrence
        self.op_index = op_index
        self.count = 0

    def _matches(self) -> bool:
        hit = self.count == self.occurrence
        self.count += 1
        return hit

    def visit_BinOp(self, node: ast.BinOp) -> ast.AST:
        self.generic_visit(node)
        if (
            self.kind == "AOR"
            and isinstance(node.op, tuple(AOR_CYCLE))
            and _aor_allowed(node)
            and self._matches()
        ):
            index = AOR_CYCLE.index(type(node.op))
            node.op = AOR_CYCLE[(index + 1) % len(AOR_CYCLE)]()
        return node

    def visit_Compare(self, node: ast.Compare) -> ast.AST:
        self.generic_visit(node)
        for index, op in enumerate(node.ops):
            if self.kind == "ROR" and isinstance(op, tuple(ROR_CYCLE)) and index == self.op_index:
                if self._matches():
                    ror_index = ROR_CYCLE.index(type(op))
                    node.ops[index] = ROR_CYCLE[(ror_index + 1) % len(ROR_CYCLE)]()
        return node

    def visit_UnaryOp(self, node: ast.UnaryOp) -> ast.AST:
        self.generic_visit(node)
        if isinstance(node.op, (ast.Not, ast.USub)) and self.kind in ("UOI_del", "UOI_flip"):
            if self._matches():
                if self.kind == "UOI_del":
                    return node.operand
                node.op = ast.USub() if isinstance(node.op, ast.Not) else ast.Not()
        return node

    def visit_BoolOp(self, node: ast.BoolOp) -> ast.AST:
        self.generic_visit(node)
        if self.kind == "LCR" and self._matches():
            node.op = ast.Or() if isinstance(node.op, ast.And) else ast.And()
        return node


def generate_mutants(source: str, module: str) -> tuple[str, list[Mutant]]:
    """Return (baseline_source, mutants). Baseline is the canonical unparse
    form so baseline and mutants share one transformation."""
    tree = ast.parse(source)
    baseline = ast.unparse(tree)
    collector = _SiteCollector()
    collector.visit(tree)

    per_kind_count: dict[str, int] = {}
    mutants: list[Mutant] = []
    for site in collector.sites:
        occurrence = per_kind_count.get(site.kind, 0)
        per_kind_count[site.kind] = occurrence + 1
        if site.kind == "ROR":
            mutator = _Mutator("ROR", occurrence, op_index=site.op_index)
        else:
            mutator = _Mutator(site.kind, occurrence)
        mutated_tree = mutator.visit(ast.parse(source))
        mutated_source = ast.unparse(mutated_tree)
        if mutated_source == baseline:
            continue  # no-op mutant (e.g. neutral unary deletion)
        mutants.append(
            Mutant(
                mutant_id=f"mut_{module}_{site.kind}_{site.lineno}_{site.col}"
                + (f"_{site.op_index}" if site.kind == "ROR" else ""),
                kind=site.kind,
                lineno=site.lineno,
                col=site.col,
                source=mutated_source,
                node_src=site.node_src,
            )
        )
    return baseline, mutants


def run_ast_engine(
    module: str, repo_root: Path, sandbox_root: Path, timeout: int
) -> dict:
    module_rel = MODULES[module]
    source = (repo_root / module_rel).read_text(encoding="utf-8")
    baseline, mutants = generate_mutants(source, module)
    sandbox = create_sandbox(repo_root, sandbox_root)
    target = sandbox / Path(*module_rel.split("/"))

    test_paths = test_paths_for(module, repo_root)

    # Baseline must be green on the transformed module, and the import proof
    # must hold, otherwise the run is invalid (harness bug).
    target.write_text(baseline, encoding="utf-8")
    outcome, output = run_pytest_in_sandbox(sandbox, test_paths, timeout)
    if outcome != "passed":
        raise HarnessFailure(
            f"[{module}] ast-engine baseline failed ({outcome}); "
            "sandbox/unparse path is broken — aborting\n"
            f"{output[-4000:]}"
        )
    boot_recorded = assert_boot_record_under_sandbox(sandbox)

    results: list[dict] = []
    killed = survived = 0
    for mutant in mutants:
        target.write_text(mutant.source, encoding="utf-8")
        try:
            outcome, output = run_pytest_in_sandbox(sandbox, test_paths, timeout)
        except HarnessFailure as exc:
            raise HarnessFailure(f"[{module}] mutant {mutant.mutant_id}: {exc}") from exc
        if outcome == "failed":
            status = "killed"
            killed += 1
        elif outcome == "passed":
            status = "survived"
            survived += 1
        else:  # timeout: counted as survived per plan, flagged for W7
            status = "timeout"
            survived += 1
        results.append(
            {
                "mutant_id": mutant.mutant_id,
                "kind": mutant.kind,
                "lineno": mutant.lineno,
                "col": mutant.col,
                "node_src": mutant.node_src,
                "outcome": status,
                "output_tail": output[-400:] if status in ("survived", "timeout") else "",
            }
        )

    total = len(results)
    return {
        "module": module,
        "engine": "ast",
        "module_path": module_rel,
        "tests": test_paths,
        "timeout_s": timeout,
        "baseline_passed": True,
        "boot_recorded_app_file": boot_recorded,
        "sandbox": sandbox.as_posix(),
        "mutants_total": total,
        "killed": killed,
        "survived": survived,
        "incompetent": 0,
        "skipped": 0,
        "score": (killed / total) if total else 0.0,
        "mutants": results,
    }


# ---------------------------------------------------------------------------
# Cosmic-ray engine (T-1 frozen tool path)
# ---------------------------------------------------------------------------


def cosmic_ray_command() -> list[str]:
    exe_dir = Path(sys.executable).resolve().parent
    for name in ("cosmic-ray.exe", "cosmic-ray.cmd", "cosmic-ray"):
        candidate = exe_dir / name
        if candidate.is_file():
            return [candidate.as_posix()]
    return [
        sys.executable,
        "-c",
        "import sys; sys.argv = ['cosmic-ray'] + sys.argv[1:]; "
        "from cosmic_ray.cli import cli; cli()",
    ]


def _run_cosmic_ray(
    args: list[str], cwd: Path, sandbox: Path | None = None, timeout: int | None = None
) -> subprocess.CompletedProcess:
    cmd = cosmic_ray_command() + args
    if sandbox is not None:
        # Inherit the sandbox proof env (PYTHONPATH, W4_MUTATION_BOOT_RECORD)
        # so every pytest subprocess spawned by cosmic-ray is gated as well.
        env = pytest_env(sandbox)
    else:
        env = os.environ.copy()
        env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        return subprocess.run(
            cmd, cwd=cwd.as_posix(), env=env, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise HarnessFailure(f"cosmic-ray {' '.join(args)} timed out: {exc}") from exc


COSMIC_RAY_CONFIG = """[cosmic-ray]
module-path = "{module_rel}"
test-command = \'\'\'"{python_exe}" -m pytest -x -q --no-header -p no:cacheprovider -p mutation_boot {tests}\'\'\'
timeout = {timeout}.0
excluded-modules = []

[cosmic-ray.distributor]
name = "local"
"""


def run_cosmic_ray_engine(
    module: str, repo_root: Path, sandbox_root: Path, timeout: int
) -> dict:
    module_rel = MODULES[module]
    test_paths = test_paths_for(module, repo_root)
    sandbox = create_sandbox(repo_root, sandbox_root)
    target = sandbox / Path(*module_rel.split("/"))
    pristine = target.read_text(encoding="utf-8")

    config_path = sandbox / "cosmic-ray.toml"
    config_path.write_text(
        COSMIC_RAY_CONFIG.format(
            module_rel=module_rel,
            python_exe=sys.executable.replace("\\", "/"),
            tests=" ".join(test_paths),
            timeout=timeout,
        ),
        encoding="utf-8",
    )
    session = sandbox / "session.sqlite"
    baseline_session = sandbox / "baseline.sqlite"

    init = _run_cosmic_ray(
        ["init", config_path.name, session.name, "--force"], cwd=sandbox
    )
    if init.returncode != 0:
        raise HarnessFailure(
            f"[{module}] cosmic-ray init failed\n{init.stdout[-2000:]}{init.stderr[-2000:]}"
        )

    baseline = _run_cosmic_ray(
        ["baseline", config_path.name, "--session-file", baseline_session.name],
        cwd=sandbox,
        sandbox=sandbox,
    )
    if baseline.returncode != 0:
        raise HarnessFailure(
            f"[{module}] cosmic-ray baseline failed: unmutated tests are not green\n"
            f"{baseline.stdout[-2000:]}{baseline.stderr[-2000:]}"
        )
    boot_recorded = assert_boot_record_under_sandbox(sandbox)

    exec_result = _run_cosmic_ray(
        ["exec", config_path.name, session.name], cwd=sandbox, sandbox=sandbox
    )
    if exec_result.returncode != 0:
        raise HarnessFailure(
            f"[{module}] cosmic-ray exec failed\n"
            f"{exec_result.stdout[-2000:]}{exec_result.stderr[-2000:]}"
        )

    dump = _run_cosmic_ray(["dump", session.name], cwd=sandbox)
    if dump.returncode != 0:
        raise HarnessFailure(
            f"[{module}] cosmic-ray dump failed\n{dump.stderr[-2000:]}"
        )

    results: list[dict] = []
    killed = survived = incompetent = skipped = timeouts = 0
    pending = 0
    for line in dump.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        work_item, result = json.loads(line)
        mutations = work_item.get("mutations") or []
        spec = mutations[0] if mutations else {}
        job_id = work_item.get("job_id", "?")
        if result is None:
            pending += 1
            results.append(
                {
                    "job_id": job_id,
                    "operator": spec.get("operator_name"),
                    "occurrence": spec.get("occurrence"),
                    "start_pos": spec.get("start_pos"),
                    "end_pos": spec.get("end_pos"),
                    "outcome": "pending",
                }
            )
            continue
        worker_outcome = result.get("worker_outcome")
        test_outcome = result.get("test_outcome")
        output = result.get("output") or ""
        if worker_outcome in ("no-test", "skipped"):
            skipped += 1
            status = "skipped"
        elif worker_outcome in ("exception", "abnormal"):
            incompetent += 1
            status = "incompetent"
        elif test_outcome == "killed" and output.strip() == "timeout":
            # Plan rule: timeout counts as survived (W7 registry), not killed.
            timeouts += 1
            survived += 1
            status = "timeout"
        elif test_outcome == "killed":
            if "no tests ran" in output:
                # Collection/usage failure would fake a kill; treat as harness error.
                raise HarnessFailure(
                    f"[{module}] mutant {job_id} collected no tests — invalid run\n{output[-2000:]}"
                )
            killed += 1
            status = "killed"
        elif test_outcome == "survived":
            survived += 1
            status = "survived"
        else:
            incompetent += 1
            status = "incompetent"
        results.append(
            {
                "job_id": job_id,
                "operator": spec.get("operator_name"),
                "occurrence": spec.get("occurrence"),
                "start_pos": spec.get("start_pos"),
                "end_pos": spec.get("end_pos"),
                "definition_name": spec.get("definition_name"),
                "outcome": status,
                "diff": (result.get("diff") or "")[:4000] if status in ("survived", "timeout") else "",
                "output_tail": output[-400:] if status in ("survived", "timeout") else "",
            }
        )

    if pending:
        raise HarnessFailure(
            f"[{module}] cosmic-ray session has {pending} pending work items; exec incomplete"
        )

    total = killed + survived + incompetent
    restored = target.read_text(encoding="utf-8") == pristine
    return {
        "module": module,
        "engine": "cosmic-ray",
        "module_path": module_rel,
        "tests": test_paths,
        "timeout_s": timeout,
        "baseline_passed": True,
        "boot_recorded_app_file": boot_recorded,
        "sandbox": sandbox.as_posix(),
        "sandbox_module_restored": restored,
        "mutants_total": total,
        "killed": killed,
        "survived": survived,
        "timeouts_as_survived": timeouts,
        "incompetent": incompetent,
        "skipped": skipped,
        "score": (killed / total) if total else 0.0,
        "mutants": results,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def module_timeout(module: str, override: int | None) -> int:
    if override is not None:
        return override
    return DEFAULT_TIMEOUTS.get(module, DEFAULT_TIMEOUT)


def run_module(
    module: str, engine: str, sprint_root: Path, repo_root: Path, timeout: int
) -> dict:
    sandbox_root = Path(tempfile.mkdtemp(prefix=f"w4_mutation_{module}_"))
    try:
        if engine == "cosmic-ray":
            report = run_cosmic_ray_engine(module, repo_root, sandbox_root, timeout)
        else:
            report = run_ast_engine(module, repo_root, sandbox_root, timeout)
    finally:
        shutil.rmtree(sandbox_root, ignore_errors=True)
    out_path = sprint_root / "w4" / "mutation_scores" / engine / f"{module}.json"
    atomic_write_json(out_path, report)
    report["score_json"] = out_path.as_posix()
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mutation_harness",
        description="W4 mutation testing harness (sandbox copies only).",
    )
    parser.add_argument("--sprint-root", default=None, help="shared sprint root")
    parser.add_argument(
        "--engine", choices=("ast", "cosmic-ray"), default=None,
        help="mutation engine; default frozen by canonical manifest cosmic_ray.status",
    )
    parser.add_argument(
        "--module", action="append", default=None,
        help="whitelist module key (repeatable); default all eligible",
    )
    parser.add_argument("--timeout", type=int, default=None, help="per-run pytest timeout seconds")
    parser.add_argument(
        "--dry-run-sentinel", action="store_true",
        help="run the sentinel proof (known-killable mutant + sandbox import evidence)",
    )
    parser.add_argument("--list-modules", action="store_true", help="list eligible modules")
    args = parser.parse_args(argv)

    try:
        sprint_root = resolve_sprint_root(args.sprint_root)
        manifest = load_manifest(sprint_root)
        engine = resolve_engine(args.engine, manifest)
        eligible = eligible_modules(manifest)
    except ProtocolError as exc:
        print(f"PROTOCOL ERROR: {exc}", file=sys.stderr)
        return 2

    if args.list_modules:
        print("\n".join(eligible))
        return 0

    if args.dry_run_sentinel:
        try:
            result = run_sentinel(REPO_ROOT, Path(tempfile.mkdtemp(prefix="w4_sentinel_")))
        except HarnessFailure as exc:
            print(f"SENTINEL FAILED: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(result, indent=2, ensure_ascii=True))
        return 0

    if args.module:
        unknown = [m for m in args.module if m not in MODULES]
        if unknown:
            print(f"PROTOCOL ERROR: unknown modules {unknown}", file=sys.stderr)
            return 2
        gated = [m for m in args.module if m not in eligible]
        if gated:
            print(
                f"PROTOCOL ERROR: modules not eligible under canonical manifest: {gated}",
                file=sys.stderr,
            )
            return 2
        modules = args.module
    else:
        modules = eligible

    failed = False
    for module in modules:
        timeout = module_timeout(module, args.timeout)
        try:
            report = run_module(module, engine, sprint_root, REPO_ROOT, timeout)
        except (HarnessFailure, ProtocolError) as exc:
            failed = True
            print(f"[{module}] FAILED: {exc}", file=sys.stderr)
            continue
        print(
            f"module={module} engine={engine} mutants={report['mutants_total']} "
            f"killed={report['killed']} survived={report['survived']} "
            f"incompetent={report['incompetent']} score={report['score']:.3f} "
            f"json={report['score_json']}"
        )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
