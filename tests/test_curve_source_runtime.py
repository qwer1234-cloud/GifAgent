"""Tests for the out-of-process curve source feature runtime protocol."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

from app.services.curve_source_features import (
    CURVE_SOURCE_FEATURE_NAMES,
    CURVE_SOURCE_RUNTIME_PROTOCOL_VERSION,
    SOURCE_DECODE_PROFILE_V1,
    CurveWindow,
    window_input_hash,
)
from app.services.curve_source_runtime import (
    CurveRuntimeConfig,
    CurveSourceRuntime,
    RuntimeResourceLock,
    model_package_sha256,
)

SOURCE_SHA = "a" * 64
KEY_A = "b" * 64
KEY_B = "c" * 64


def _windows() -> tuple[CurveWindow, ...]:
    return (
        CurveWindow(preexport_key=KEY_A, start_us=1_000_000, end_us=4_000_000),
        CurveWindow(preexport_key=KEY_B, start_us=5_000_000, end_us=8_000_000),
    )


FAKE_WORKER = r"""
import json, sys, time
from pathlib import Path

args = sys.argv[1:]
req_path = Path(args[args.index("--request") + 1])
resp_path = Path(args[args.index("--response") + 1])
req = json.loads(req_path.read_text(encoding="utf-8"))
mode = req.get("fake_mode", "")

if mode == "sleep":
    time.sleep(30)
if mode == "crash":
    sys.exit(3)
if mode == "garbage":
    resp_path.write_text("this is not json", encoding="utf-8")
    sys.exit(0)
if mode == "dump_request":
    Path(req["fake_dump_path"]).write_text(json.dumps(req), encoding="utf-8")

FEATS = {
    "pose_valid_ratio": 0.92,
    "subject_area_ratio": 0.18,
    "extension_mean": 0.55,
    "extension_peak": 0.80,
    "extension_range": 0.35,
    "torso_speed_p95": 0.21,
    "motion_smoothness": 0.74,
    "visibility_ratio": 0.83,
    "border_clip_ratio": 0.04,
    "verticality_mean": 0.06,
}
results = []
for w in req["windows"]:
    start_s = w["start_us"] / 1e6
    step = max((w["end_us"] - w["start_us"]) / 1e6 / 8.0, 0.001)
    stamps = [round(start_s + i * step, 6) for i in range(4)]
    if mode == "window_fail" and w["preexport_key"] == "c" * 64:
        results.append({
            "preexport_key": w["preexport_key"],
            "input_hash": w["input_hash"],
            "status": "UNAVAILABLE",
            "features": None,
            "actual_timestamps": [],
            "missing_reasons": ["pose_coverage_low"],
            "wall_clock_ms": 1.0,
        })
    elif mode == "incomplete_features":
        feats = dict(FEATS)
        feats.pop("verticality_mean")
        results.append({
            "preexport_key": w["preexport_key"],
            "input_hash": w["input_hash"],
            "status": "AVAILABLE",
            "features": feats,
            "actual_timestamps": stamps,
            "missing_reasons": [],
            "wall_clock_ms": 1.0,
        })
    else:
        results.append({
            "preexport_key": w["preexport_key"],
            "input_hash": w["input_hash"],
            "status": "AVAILABLE",
            "features": dict(FEATS),
            "actual_timestamps": stamps,
            "missing_reasons": [],
            "wall_clock_ms": 1.0,
        })

resp = {
    "protocol_version": req["protocol_version"],
    "request_sha256": req["request_sha256"],
    "model_package_sha256": req["model_package_sha256"],
    "extractor_sha256": req["extractor_sha256"],
    "feature_version": req["feature_version"],
    "results": results,
}
if mode == "bad_extractor":
    resp["extractor_sha256"] = "9" * 64
if mode == "bad_request_hash":
    resp["request_sha256"] = "0" * 64
if mode == "bad_model_hash":
    resp["model_package_sha256"] = "1" * 64

resp_path.write_text(json.dumps(resp), encoding="utf-8")
sys.exit(0)
"""


@pytest.fixture()
def runtime_env(tmp_path: Path) -> tuple[CurveSourceRuntime, Path]:
    worker_script = tmp_path / "fake_worker.py"
    worker_script.write_text(FAKE_WORKER, encoding="utf-8")
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    (model_dir / "pose.onnx").write_bytes(b"fake-model-bytes")
    backend_script = tmp_path / "pose_backend.py"
    backend_script.write_text("# fake pose backend" + chr(10), encoding="utf-8")
    config = CurveRuntimeConfig(
        worker_executable=sys.executable,
        worker_script=str(worker_script),
        backend_script=str(backend_script),
        model_dir=str(model_dir),
        lock_dir=str(tmp_path / "locks"),
        lock_wait_s=0.2,
    )
    return CurveSourceRuntime(config, owner="test"), tmp_path


def _request_with_mode(mode: str, **extra) -> dict:
    request = {"fake_mode": mode}
    request.update(extra)
    return request


def _patch_request_mode(runtime: CurveSourceRuntime, mode: str, **extra) -> None:
    original = runtime._build_request

    def build_with_mode(source_path, source_sha256, windows, model_hash, extractor):
        request = original(source_path, source_sha256, windows, model_hash, extractor)
        request.update(_request_with_mode(mode, **extra))
        return request

    runtime._build_request = build_with_mode  # type: ignore[method-assign]


def test_evaluate_video_returns_available_and_replays_identically(runtime_env):
    runtime, _tmp = runtime_env
    first = runtime.evaluate_video(Path("fake.mp4"), SOURCE_SHA, _windows(), 10.0)
    second = runtime.evaluate_video(Path("fake.mp4"), SOURCE_SHA, _windows(), 10.0)
    assert len(first) == 2
    for result in first:
        assert result.status == "AVAILABLE"
        assert set(result.features) == set(CURVE_SOURCE_FEATURE_NAMES)
        assert all(
            isinstance(v, float) and v == v and abs(v) != float("inf")
            for v in result.features.values()
        )
        assert len(result.actual_timestamps) >= 3
    # Replay consistency: features, timestamps, hashes, and status are
    # identical; only the wall-clock measurement may differ.
    assert [r.features for r in first] == [r.features for r in second]
    assert [r.actual_timestamps for r in first] == [r.actual_timestamps for r in second]
    assert [r.input_hash for r in first] == [r.input_hash for r in second]
    assert [r.status for r in first] == [r.status for r in second]


def test_request_protocol_fields_are_complete(runtime_env, tmp_path):
    runtime, _tmp = runtime_env
    dump_path = tmp_path / "dumped_request.json"
    _patch_request_mode(runtime, "dump_request", fake_dump_path=str(dump_path))
    runtime.evaluate_video(Path("fake.mp4"), SOURCE_SHA, _windows(), 10.0)

    dumped = json.loads(dump_path.read_text(encoding="utf-8"))
    assert dumped["protocol_version"] == CURVE_SOURCE_RUNTIME_PROTOCOL_VERSION
    assert dumped["source_sha256"] == SOURCE_SHA
    assert dumped["feature_version"] == "curve-source-v1"
    assert dumped["decode_profile"] == dict(SOURCE_DECODE_PROFILE_V1)
    assert len(dumped["extractor_sha256"]) == 64
    assert len(dumped["request_sha256"]) == 64
    assert len(dumped["model_package_sha256"]) == 64
    by_key = {w["preexport_key"]: w for w in dumped["windows"]}
    assert set(by_key) == {KEY_A, KEY_B}
    expected_extractor = runtime._expected_extractor_identity(
        model_package_sha256(Path(runtime.config.model_dir))
    )
    assert dumped["extractor_sha256"] == expected_extractor
    for key, window in by_key.items():
        assert window["start_us"] == (1_000_000 if key == KEY_A else 5_000_000)
        assert window["end_us"] == (4_000_000 if key == KEY_A else 8_000_000)
        assert window["input_hash"] == window_input_hash(
            SOURCE_SHA,
            _windows()[0 if key == KEY_A else 1],
            extractor_sha256=expected_extractor,
        )


def test_timeout_returns_structured_unavailable(runtime_env):
    runtime, _tmp = runtime_env
    _patch_request_mode(runtime, "sleep")
    results = runtime.evaluate_video(Path("fake.mp4"), SOURCE_SHA, _windows(), 0.5)
    assert all(r.status == "UNAVAILABLE" for r in results)
    assert all("inference_timeout" in r.missing_reasons for r in results)
    assert all(r.features is None for r in results)


def test_garbage_response_is_structured_unavailable(runtime_env):
    runtime, _tmp = runtime_env
    _patch_request_mode(runtime, "garbage")
    results = runtime.evaluate_video(Path("fake.mp4"), SOURCE_SHA, _windows(), 10.0)
    assert all("response_invalid" in r.missing_reasons for r in results)


def test_nonzero_exit_is_structured_unavailable(runtime_env):
    runtime, _tmp = runtime_env
    _patch_request_mode(runtime, "crash")
    results = runtime.evaluate_video(Path("fake.mp4"), SOURCE_SHA, _windows(), 10.0)
    assert all("worker_exit_nonzero" in r.missing_reasons for r in results)


def test_missing_worker_executable_never_crashes(tmp_path):
    config = CurveRuntimeConfig(
        worker_executable=str(tmp_path / "missing_python.exe"),
        worker_script=str(tmp_path / "missing_worker.py"),
        model_dir=str(tmp_path),
        lock_dir=str(tmp_path / "locks"),
        lock_wait_s=0.1,
    )
    runtime = CurveSourceRuntime(config, owner="test")
    results = runtime.evaluate_video(Path("fake.mp4"), SOURCE_SHA, _windows(), 10.0)
    assert all(
        "inference_runtime_unavailable" in r.missing_reasons for r in results
    )


def test_request_hash_mismatch_is_detected(runtime_env):
    runtime, _tmp = runtime_env
    _patch_request_mode(runtime, "bad_request_hash")
    results = runtime.evaluate_video(Path("fake.mp4"), SOURCE_SHA, _windows(), 10.0)
    assert all("request_hash_mismatch" in r.missing_reasons for r in results)


def test_model_hash_drift_is_detected(runtime_env):
    runtime, _tmp = runtime_env
    _patch_request_mode(runtime, "bad_model_hash")
    results = runtime.evaluate_video(Path("fake.mp4"), SOURCE_SHA, _windows(), 10.0)
    assert all("model_package_hash_mismatch" in r.missing_reasons for r in results)


def test_single_window_failure_does_not_poison_others(runtime_env):
    runtime, _tmp = runtime_env
    _patch_request_mode(runtime, "window_fail")
    results = runtime.evaluate_video(Path("fake.mp4"), SOURCE_SHA, _windows(), 10.0)
    by_key = {r.preexport_key: r for r in results}
    assert by_key[KEY_A].status == "AVAILABLE"
    assert by_key[KEY_B].status == "UNAVAILABLE"
    assert "pose_coverage_low" in by_key[KEY_B].missing_reasons
    assert by_key[KEY_B].features is None


def test_incomplete_available_features_are_rejected_not_filled(runtime_env):
    runtime, _tmp = runtime_env
    _patch_request_mode(runtime, "incomplete_features")
    results = runtime.evaluate_video(Path("fake.mp4"), SOURCE_SHA, _windows(), 10.0)
    assert all(r.status == "UNAVAILABLE" for r in results)
    assert all("feature_set_incomplete" in r.missing_reasons for r in results)


def test_busy_runtime_lock_returns_inference_runtime_busy(runtime_env):
    runtime, tmp_path = runtime_env
    holder = RuntimeResourceLock(
        lock_dir=Path(runtime.config.lock_dir),
        resource_key=runtime.config.resource_key,
        owner="someone-else",
        ttl_s=600.0,
    )
    assert holder.acquire(wait_s=0.0, now_epoch=time.time()) is True
    try:
        results = runtime.evaluate_video(Path("fake.mp4"), SOURCE_SHA, _windows(), 10.0)
        assert all("inference_runtime_busy" in r.missing_reasons for r in results)
    finally:
        holder.release()

    released = runtime.evaluate_video(Path("fake.mp4"), SOURCE_SHA, _windows(), 10.0)
    assert all(r.status == "AVAILABLE" for r in released)


def test_stale_lock_is_taken_over(runtime_env):
    runtime, tmp_path = runtime_env
    stale = RuntimeResourceLock(
        lock_dir=Path(runtime.config.lock_dir),
        resource_key=runtime.config.resource_key,
        owner="dead-process",
        ttl_s=1.0,
    )
    stale.acquire(wait_s=0.0, now_epoch=time.time() - 10.0)
    # The stale lease expired (created 10s ago with a 1s TTL); the runtime
    # must take it over instead of reporting busy forever.
    results = runtime.evaluate_video(Path("fake.mp4"), SOURCE_SHA, _windows(), 10.0)
    assert all(r.status == "AVAILABLE" for r in results)


def test_model_package_hash_is_content_addressed(runtime_env):
    runtime, tmp_path = runtime_env
    model_dir = Path(runtime.config.model_dir)
    expected = model_package_sha256(model_dir)
    assert expected == model_package_sha256(model_dir)
    (model_dir / "pose.onnx").write_bytes(b"changed-model-bytes")
    assert model_package_sha256(model_dir) != expected


def test_extractor_identity_binds_model_package_and_worker_code(runtime_env):
    """P1 remediation: the extractor identity changes when the model package
    OR the worker implementation changes, so feature domains can never be
    silently mixed after an upgrade."""
    runtime, tmp_path = runtime_env
    model_dir = Path(runtime.config.model_dir)
    model_hash = model_package_sha256(model_dir)
    identity = runtime._expected_extractor_identity(model_hash)

    # Same model + same worker -> same identity.
    assert runtime._expected_extractor_identity(model_hash) == identity

    # Model package change -> different identity.
    (model_dir / "pose.onnx").write_bytes(b"updated-weights")
    new_model_hash = model_package_sha256(model_dir)
    assert new_model_hash != model_hash
    assert runtime._expected_extractor_identity(new_model_hash) != identity

    # Worker implementation change -> different identity.
    worker_script = Path(runtime.config.worker_script)
    worker_script.write_text(
        worker_script.read_text(encoding="utf-8") + chr(10) + "# touched" + chr(10)
    )
    assert runtime._expected_extractor_identity(new_model_hash) != identity

    # Pose backend implementation change -> different identity (review P2).
    backend_script = Path(runtime.config.backend_script)
    backend_script.write_text(
        backend_script.read_text(encoding="utf-8") + chr(10) + "# thresholds tuned" + chr(10)
    )
    assert runtime._expected_extractor_identity(new_model_hash) != identity


def test_extractor_echo_mismatch_is_structured_unavailable(runtime_env):
    runtime, _tmp = runtime_env
    _patch_request_mode(runtime, "bad_extractor")
    results = runtime.evaluate_video(Path("fake.mp4"), SOURCE_SHA, _windows(), 10.0)
    assert all(r.status == "UNAVAILABLE" for r in results)
    assert all("extractor_sha256_mismatch" in r.missing_reasons for r in results)