"""Out-of-process curve source feature runtime client.

The main app process only holds the JSON protocol and a subprocess client;
heavy vision inference (onnxruntime/cv2) runs inside the ``.venv-spike``
worker (``scripts/extract_curve_source_features.py``). Every failure mode
degrades to a structured ``UNAVAILABLE`` result -- never to synthetic,
zero-filled, or hashed-placeholder features.

Path safety: every filesystem path built from runtime values is sanitized to
a strict charset and verified, immediately before use, to resolve inside its
configured root directory. All file mutations go through pathlib's atomic
methods (mkdir/write_text/replace/unlink), never raw os-level writes.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from app.services.curve_source_features import (
    CURVE_SOURCE_FEATURE_VERSION,
    CURVE_SOURCE_RUNTIME_PROTOCOL_VERSION,
    DEFAULT_SOURCE_EXTRACTOR_SHA256,
    SOURCE_DECODE_PROFILE_V1,
    CurveFeatureResult,
    CurveWindow,
    validate_window_feature_values,
    window_input_hash,
)

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_WORKER_EXECUTABLE = str(REPO_ROOT / ".venv-spike" / "Scripts" / "python.exe")
DEFAULT_WORKER_SCRIPT = str(REPO_ROOT / "scripts" / "extract_curve_source_features.py")
DEFAULT_MODEL_DIR = str(REPO_ROOT / "data" / "continuity_spike_models")
DEFAULT_LOCK_DIR = str(REPO_ROOT / "data" / "runtime_locks")

SAFE_COMPONENT_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
)

LEASE_FILE_NAME = "lease.json"


def _safe_component(value: str) -> str:
    """Reduce a path component to a strict safe charset (no separators)."""
    return "".join(ch if ch in SAFE_COMPONENT_CHARS else "_" for ch in value)


def _verified_inside(root: Path, candidate: Path) -> Path:
    """Verify ``candidate`` resolves inside ``root`` and return it resolved."""
    root_str = str(root)
    candidate_str = str(candidate)
    if candidate_str != root_str and not candidate_str.startswith(root_str + os.sep):
        raise ValueError(f"path escapes root: {candidate_str!r}")
    return candidate


def _unavailable(
    window: CurveWindow,
    reasons: Iterable[str],
    input_hash: str = "",
    wall_clock_ms: float = 0.0,
) -> CurveFeatureResult:
    return CurveFeatureResult(
        preexport_key=window.preexport_key,
        status="UNAVAILABLE",
        feature_version=CURVE_SOURCE_FEATURE_VERSION,
        extractor_sha256=DEFAULT_SOURCE_EXTRACTOR_SHA256,
        features=None,
        actual_timestamps=(),
        input_hash=input_hash,
        missing_reasons=tuple(dict.fromkeys(reasons)),
        wall_clock_ms=round(float(wall_clock_ms), 2),
    )


@dataclass
class RuntimeResourceLock:
    """Cross-process advisory lock for the shared inference runtime.

    The lock is a directory claimed atomically via ``mkdir(exist_ok=False)``;
    it holds a ``lease.json`` with owner, PID, and creation/expiry epochs.
    Takeover is TTL-based: an expired lease directory is removed and
    re-claimed. Only the lock holder spawns workers, and only its own child
    processes are ever terminated -- other applications (Ollama, WSL,
    editors) are untouched.
    """

    lock_dir: Path
    resource_key: str
    owner: str
    ttl_s: float = 900.0
    _path: Path | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        root = Path(self.lock_dir).resolve()
        # The lock directory name is sanitized to a strict charset before it
        # is ever joined onto the resolved root.
        dir_name = _safe_component(self.resource_key) + ".lock"
        lock_path = root / dir_name
        self._resolved_root = root
        self._path = _verified_inside(root, lock_path.resolve())

    @property
    def path(self) -> Path:
        assert self._path is not None
        return self._path

    @property
    def _lease_path(self) -> Path:
        return self.path / LEASE_FILE_NAME

    def _lease_payload(self, now_epoch: float) -> dict[str, Any]:
        return {
            "owner": self.owner,
            "pid": os.getpid(),
            "created_epoch": now_epoch,
            "expires_epoch": now_epoch + self.ttl_s,
        }

    def _ensure_parent(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _try_claim(self, now_epoch: float) -> bool:
        self._ensure_parent()
        try:
            self.path.mkdir(exist_ok=False)
        except FileExistsError:
            return False
        self._lease_path.write_text(
            json.dumps(self._lease_payload(now_epoch)), encoding="utf-8"
        )
        return True

    def _expired(self, now_epoch: float) -> bool:
        try:
            lease = json.loads(self._lease_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return True
        expires = lease.get("expires_epoch")
        if not isinstance(expires, (int, float)) or not math.isfinite(expires):
            return True
        return now_epoch > float(expires)

    def _remove_stale(self) -> None:
        """Best-effort removal of an expired lease directory."""
        try:
            self._lease_path.unlink()
        except OSError:
            pass
        try:
            self.path.rmdir()
        except OSError:
            pass

    def acquire(self, wait_s: float, now_epoch: float | None = None) -> bool:
        """Claim the lock, waiting up to ``wait_s`` seconds for a busy runtime."""
        start = time.monotonic()
        if now_epoch is None:
            now_epoch = time.time()
        while True:
            if self._try_claim(now_epoch):
                return True
            if self._expired(now_epoch):
                self._remove_stale()
                if self._try_claim(now_epoch):
                    return True
            if time.monotonic() - start >= wait_s:
                return False
            time.sleep(0.05)

    def release(self) -> None:
        try:
            self._lease_path.unlink()
        except OSError:
            pass
        try:
            self.path.rmdir()
        except OSError:
            logger.debug("runtime lock release failed", exc_info=True)
        self._path = None


@dataclass(frozen=True)
class CurveRuntimeConfig:
    """Where and how the out-of-process feature worker runs."""

    worker_executable: str = DEFAULT_WORKER_EXECUTABLE
    worker_script: str = DEFAULT_WORKER_SCRIPT
    model_dir: str = DEFAULT_MODEL_DIR
    lock_dir: str = DEFAULT_LOCK_DIR
    resource_key: str = "curve-source-inference"
    lock_wait_s: float = 5.0
    expected_model_package_sha256: str | None = None


def model_package_sha256(model_dir: Path) -> str | None:
    """Content-addressed hash over the model package files."""
    model_dir = Path(model_dir)
    if not model_dir.is_dir():
        return None
    entries: dict[str, str] = {}
    for file_path in sorted(model_dir.rglob("*")):
        if not file_path.is_file():
            continue
        digest = hashlib.sha256()
        with open(file_path, "rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        entries[file_path.relative_to(model_dir).as_posix()] = digest.hexdigest()
    if not entries:
        return None
    canonical = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


class CurveSourceRuntime:
    """Subprocess client for the curve source feature worker."""

    def __init__(
        self,
        config: CurveRuntimeConfig | None = None,
        *,
        owner: str = "curve-source-runtime",
    ) -> None:
        self.config = config or CurveRuntimeConfig()
        self.owner = owner

    def evaluate_video(
        self,
        source_path: Path,
        source_sha256: str,
        windows: tuple[CurveWindow, ...],
        timeout_s: float = 120.0,
    ) -> tuple[CurveFeatureResult, ...]:
        """Evaluate all post-dedup windows of one source video.

        Windows are matched back strictly by ``preexport_key``; any runtime
        failure yields structured ``UNAVAILABLE`` results for the affected
        windows and never raises into the ranking pipeline.
        """
        t0 = time.perf_counter()
        if not windows:
            return ()

        seen: set[str] = set()
        for window in windows:
            if window.preexport_key in seen:
                raise ValueError(
                    f"duplicate window preexport_key: {window.preexport_key}"
                )
            seen.add(window.preexport_key)
            if window.end_us <= window.start_us:
                raise ValueError(
                    f"invalid window bounds for {window.preexport_key}"
                )

        model_hash = model_package_sha256(Path(self.config.model_dir))
        if model_hash is None:
            return self._all_unavailable(windows, ["inference_runtime_unavailable"], t0)
        if (
            self.config.expected_model_package_sha256
            and self.config.expected_model_package_sha256 != model_hash
        ):
            return self._all_unavailable(windows, ["model_package_hash_mismatch"], t0)

        worker_ready = Path(self.config.worker_executable).is_file() and Path(
            self.config.worker_script
        ).is_file()
        if not worker_ready:
            return self._all_unavailable(windows, ["inference_runtime_unavailable"], t0)

        lock = RuntimeResourceLock(
            lock_dir=Path(self.config.lock_dir),
            resource_key=self.config.resource_key,
            owner=self.owner,
        )
        if not lock.acquire(wait_s=self.config.lock_wait_s):
            return self._all_unavailable(windows, ["inference_runtime_busy"], t0)

        try:
            return self._run_worker(
                source_path, source_sha256, windows, timeout_s, model_hash, t0
            )
        finally:
            lock.release()

    def _build_request(
        self,
        source_path: Path,
        source_sha256: str,
        windows: tuple[CurveWindow, ...],
        model_hash: str,
    ) -> dict[str, Any]:
        window_entries = []
        for window in windows:
            window_entries.append(
                {
                    "preexport_key": window.preexport_key,
                    "start_us": window.start_us,
                    "end_us": window.end_us,
                    "input_hash": window_input_hash(source_sha256, window),
                }
            )
        request = {
            "protocol_version": CURVE_SOURCE_RUNTIME_PROTOCOL_VERSION,
            "source_path": str(source_path),
            "source_sha256": source_sha256,
            "windows": window_entries,
            "feature_version": CURVE_SOURCE_FEATURE_VERSION,
            "decode_profile": dict(SOURCE_DECODE_PROFILE_V1),
            "extractor_sha256": DEFAULT_SOURCE_EXTRACTOR_SHA256,
            "model_dir": str(self.config.model_dir),
            "model_package_sha256": model_hash,
        }
        request_sha = hashlib.sha256(
            json.dumps(request, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        request["request_sha256"] = request_sha
        return request

    def _run_worker(
        self,
        source_path: Path,
        source_sha256: str,
        windows: tuple[CurveWindow, ...],
        timeout_s: float,
        model_hash: str,
        t0: float,
    ) -> tuple[CurveFeatureResult, ...]:
        request = self._build_request(source_path, source_sha256, windows, model_hash)

        with tempfile.TemporaryDirectory(prefix="curve_source_runtime_") as tmp:
            work_dir = Path(tmp).resolve()
            request_path = work_dir / "request.json"
            response_path = work_dir / "response.json"
            request_path.write_text(
                json.dumps(request, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )

            command = [
                str(self.config.worker_executable),
                str(self.config.worker_script),
                "--request",
                str(request_path),
                "--response",
                str(response_path),
            ]
            try:
                completed = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=float(timeout_s),
                )
            except subprocess.TimeoutExpired:
                logger.warning("curve source worker timed out after %.1fs", timeout_s)
                return self._all_unavailable(windows, ["inference_timeout"], t0)
            except OSError as exc:
                logger.warning(
                    "curve source worker failed to start: %s", type(exc).__name__
                )
                return self._all_unavailable(windows, ["inference_runtime_unavailable"], t0)

            if completed.returncode != 0:
                logger.warning("curve source worker exited rc=%s", completed.returncode)
                return self._all_unavailable(windows, ["worker_exit_nonzero"], t0)

            if not response_path.is_file():
                return self._all_unavailable(windows, ["response_missing"], t0)

            try:
                response = json.loads(response_path.read_text(encoding="utf-8"))
            except ValueError:
                return self._all_unavailable(windows, ["response_invalid"], t0)

        return self._verify_response(request, response, windows, t0)

    def _verify_response(
        self,
        request: Mapping[str, Any],
        response: Any,
        windows: tuple[CurveWindow, ...],
        t0: float,
    ) -> tuple[CurveFeatureResult, ...]:
        if not isinstance(response, dict):
            return self._all_unavailable(windows, ["response_invalid"], t0)
        if response.get("protocol_version") != CURVE_SOURCE_RUNTIME_PROTOCOL_VERSION:
            return self._all_unavailable(windows, ["protocol_version_mismatch"], t0)
        if response.get("request_sha256") != request.get("request_sha256"):
            return self._all_unavailable(windows, ["request_hash_mismatch"], t0)
        if response.get("model_package_sha256") != request.get("model_package_sha256"):
            return self._all_unavailable(windows, ["model_package_hash_mismatch"], t0)

        results_raw = response.get("results")
        if not isinstance(results_raw, list):
            return self._all_unavailable(windows, ["response_invalid"], t0)

        by_key: dict[str, dict[str, Any]] = {}
        for entry in results_raw:
            if not isinstance(entry, dict):
                return self._all_unavailable(
                    windows, ["response_result_shape_invalid"], t0
                )
            key = entry.get("preexport_key")
            if not isinstance(key, str) or not key or key in by_key:
                return self._all_unavailable(
                    windows, ["response_result_shape_invalid"], t0
                )
            by_key[key] = entry
        requested_keys = {window.preexport_key for window in windows}
        if set(by_key) - requested_keys:
            return self._all_unavailable(
                windows, ["response_result_shape_invalid"], t0
            )

        expected_hashes = {
            entry["preexport_key"]: entry["input_hash"]
            for entry in request["windows"]
        }

        results: list[CurveFeatureResult] = []
        for window in windows:
            entry = by_key.get(window.preexport_key)
            if entry is None:
                results.append(
                    _unavailable(
                        window, ["window_result_missing"], wall_clock_ms=self._ms(t0)
                    )
                )
                continue
            if entry.get("input_hash") != expected_hashes[window.preexport_key]:
                results.append(
                    _unavailable(
                        window, ["input_hash_mismatch"], wall_clock_ms=self._ms(t0)
                    )
                )
                continue
            results.append(self._window_result(window, entry, self._ms(t0)))
        return tuple(results)

    @staticmethod
    def _ms(t0: float) -> float:
        return round((time.perf_counter() - t0) * 1000.0, 2)

    def _all_unavailable(
        self,
        windows: tuple[CurveWindow, ...],
        reasons: list[str],
        t0: float,
    ) -> tuple[CurveFeatureResult, ...]:
        return tuple(
            _unavailable(window, reasons, wall_clock_ms=self._ms(t0))
            for window in windows
        )

    @staticmethod
    def _window_result(
        window: CurveWindow,
        entry: Mapping[str, Any],
        wall_clock_ms: float,
    ) -> CurveFeatureResult:
        status = entry.get("status")
        features: Mapping[str, Any] | None = entry.get("features")
        timestamps = entry.get("actual_timestamps") or []
        reasons = entry.get("missing_reasons") or []
        input_hash = str(entry.get("input_hash") or "")
        worker_ms = float(entry.get("wall_clock_ms") or 0.0)
        total_ms = round(wall_clock_ms + worker_ms, 2)

        if status == "AVAILABLE":
            assert features is not None  # strict validation rejects None
            strict_reasons = validate_window_feature_values(features, timestamps, window)
            if strict_reasons:
                return CurveFeatureResult(
                    preexport_key=window.preexport_key,
                    status="UNAVAILABLE",
                    feature_version=CURVE_SOURCE_FEATURE_VERSION,
                    extractor_sha256=DEFAULT_SOURCE_EXTRACTOR_SHA256,
                    features=None,
                    actual_timestamps=(),
                    input_hash=input_hash,
                    missing_reasons=tuple(dict.fromkeys(strict_reasons)),
                    wall_clock_ms=total_ms,
                )
            return CurveFeatureResult(
                preexport_key=window.preexport_key,
                status="AVAILABLE",
                feature_version=CURVE_SOURCE_FEATURE_VERSION,
                extractor_sha256=DEFAULT_SOURCE_EXTRACTOR_SHA256,
                features={name: float(features[name]) for name in sorted(features)},
                actual_timestamps=tuple(float(ts) for ts in timestamps),
                input_hash=input_hash,
                missing_reasons=(),
                wall_clock_ms=total_ms,
            )

        reason_list = (
            reasons if isinstance(reasons, list) and reasons else ["window_inference_failed"]
        )
        return _unavailable(window, reason_list, input_hash, total_ms)
