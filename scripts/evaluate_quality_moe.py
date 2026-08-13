#!/usr/bin/env python3
"""Evaluate one explicit, read-only video interval with the quality MoE.

This runner deliberately has no directory-discovery mode.  It is intended for
bounded smoke tests and writes only into a caller-owned output directory.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import dataclass, replace
import hashlib
import json
import math
from pathlib import Path
import subprocess
import sys
from typing import Mapping, Sequence
from uuid import uuid4

import httpx
import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from app.quality_moe.config import QualityMoeConfig
from app.quality_moe.evaluator import evaluate_candidate
from app.quality_moe.judge import OllamaQualityJudge
from app.quality_moe.models import EvidenceStatus
from app.quality_moe.repair import (
    _contact_destination,
    _save_contact_sheet,
    _write_new_file_or_reuse_identical,
)
from app.quality_moe.sampling import SampledClip, sample_clip_frames
from app.services.ollama_runtime import (
    EmbeddingRuntimeConfig,
    OllamaRuntimeManager,
    normalize_base_url,
)


DEFAULT_CONFIG = REPOSITORY_ROOT / "configs" / "models.yaml"
DEFAULT_DURATION_SECONDS = 12.0


@dataclass(frozen=True)
class CliRunResult:
    output_dir: Path
    assessment_path: Path
    payload: Mapping[str, object]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Evaluate exactly one read-only video interval with quality MoE."
    )
    parser.add_argument("--video", required=True, help="Exact source video file")
    parser.add_argument("--start", type=float, default=0.0, help="Requested start in seconds")
    parser.add_argument(
        "--duration", type=float, default=DEFAULT_DURATION_SECONDS,
        help="Requested bounded duration in seconds (default: 12)",
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="Frozen YAML configuration")
    parser.add_argument("--output-dir", required=True, help="Artifact directory")
    parser.add_argument(
        "--skip-judge", action="store_true",
        help="Do not construct or call the external Ollama judge",
    )
    return parser


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _probe_duration(path: Path) -> float:
    command = [
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "json", str(path),
    ]
    try:
        completed = subprocess.run(
            command, check=True, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30,
        )
        payload = json.loads(completed.stdout)
        duration = float(payload["format"]["duration"])
    except (OSError, subprocess.SubprocessError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError(f"ffprobe could not determine video duration: {error}") from error
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("ffprobe returned a non-positive video duration")
    return duration


def _resolve_interval(start: float, duration: float, media_duration: float) -> tuple[float, float, bool]:
    if not math.isfinite(start) or start < 0:
        raise ValueError("start must be finite and non-negative")
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("duration must be finite and positive")
    if start + duration <= media_duration:
        return start, start + duration, False
    # An out-of-range request is deliberately relocated to the movie centre,
    # rather than silently evaluating a neighbouring tail interval.
    resolved_duration = min(duration, media_duration)
    resolved_start = max(0.0, (media_duration - resolved_duration) / 2.0)
    resolved_end = resolved_start + resolved_duration
    # Random access exactly at EOF is not a decodable frame on common codecs.
    if resolved_end >= media_duration:
        resolved_end = max(resolved_start + min(0.001, media_duration / 2.0), media_duration - 0.001)
    if resolved_end <= resolved_start:
        raise ValueError("video is too short to sample a positive interval")
    return resolved_start, resolved_end, True


def _sample_exact_interval(video: Path, start: float, end: float, candidate_id: str) -> SampledClip:
    """Sample inside the requested bounds even when a codec rounds EOF seeks up.

    Some codecs report the frame decoded for an exact end timestamp a few
    milliseconds after that timestamp.  Retrying with an interior final sample
    preserves the candidate's requested bounds while ensuring every observed
    frame is actually inside them.
    """
    sampled = sample_clip_frames(video, start, end, candidate_id)
    if sampled.status is EvidenceStatus.AVAILABLE:
        return sampled
    retryable = {
        "decoded_timestamp_outside_interval",
        "decoded_timestamp_mismatch",
        "frame_decode_failed",
    }
    if sampled.diagnostics.get("code") not in retryable:
        return sampled
    interval = end - start
    for requested_margin in (0.05, 0.10, 0.25):
        margin = min(requested_margin, interval / 4.0)
        interior_end = end - margin
        if interior_end <= start:
            break
        retry = sample_clip_frames(video, start, interior_end, candidate_id)
        if retry.status is EvidenceStatus.AVAILABLE:
            diagnostics = dict(retry.diagnostics)
            diagnostics.update({
                "requested_start_ts": start,
                "requested_end_ts": end,
                "sampling_end_margin_s": margin,
            })
            return replace(
                retry, start_ts=start, end_ts=end, diagnostics=diagnostics
            )
        if retry.diagnostics.get("code") not in retryable:
            return retry
    return sampled


def _load_and_freeze_config(
    path: Path, *, skip_judge: bool = False,
) -> tuple[QualityMoeConfig, dict[str, object]]:
    if not path.is_file():
        raise ValueError("config must be an existing regular file")
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if loaded is None:
        loaded = {}
    if not isinstance(loaded, dict):
        raise ValueError("config root must be a mapping")
    snapshot: dict[str, object] = deepcopy(loaded)
    quality = snapshot.setdefault("quality_moe", {})
    if not isinstance(quality, dict):
        raise ValueError("quality_moe config must be a mapping")
    judge = quality.setdefault("judge", {})
    if not isinstance(judge, dict):
        raise ValueError("quality_moe.judge config must be a mapping")
    if skip_judge:
        # Skip mode must not consult environment variables, WSL, lifecycle
        # managers, or endpoint discovery.  The sentinel is frozen and cannot
        # accidentally reach httpx because no judge object is constructed.
        judge["base_url"] = "skipped"
        frozen = QualityMoeConfig.from_mapping(snapshot)
        return frozen, snapshot
    configured = str(judge.get("base_url", "http://127.0.0.1:11434") or "").strip()
    if configured.lower() == "inherit_vlm":
        vlm = snapshot.get("vlm", {})
        if not isinstance(vlm, dict):
            raise ValueError("vlm config must be a mapping")
        configured = str(vlm.get("base_url", "auto") or "auto").strip()
    if configured.lower() == "auto":
        vlm = snapshot.get("vlm", {})
        if not isinstance(vlm, dict):
            vlm = {}
        runtime = EmbeddingRuntimeConfig(
            base_url="auto",
            manage_lifecycle=bool(vlm.get("manage_lifecycle", False)),
            launch_mode=str(vlm.get("launch_mode", "none") or "none").lower(),
            wsl_distro=str(vlm.get("wsl_distro", "Ubuntu-20.04") or "Ubuntu-20.04"),
            startup_timeout_s=float(vlm.get("startup_timeout_s", 120.0) or 120.0),
            request_timeout_s=float(vlm.get("timeout_seconds", 120.0) or 120.0),
            embedding_model=str(judge.get("model_id", vlm.get("model", "llava:13b"))),
        )
        configured = OllamaRuntimeManager().resolve_base_url(runtime)
    absolute_url = normalize_base_url(configured)
    if not absolute_url.startswith(("http://", "https://")):
        raise ValueError("resolved quality judge base_url must be absolute")
    judge["base_url"] = absolute_url
    frozen = QualityMoeConfig.from_mapping(snapshot)
    return frozen, snapshot


def _validate_paths(video_value: str, output_value: str, config_value: str) -> tuple[Path, Path, Path]:
    video = Path(video_value).expanduser().resolve(strict=False)
    if not video.is_file():
        raise ValueError("video must be an existing regular file")
    config = Path(config_value).expanduser().resolve(strict=False)
    output = Path(output_value).expanduser().resolve(strict=False)
    if output == video or output.is_relative_to(video):
        raise ValueError("output directory conflicts with source video")
    if output.exists() and not output.is_dir():
        raise ValueError("output directory conflicts with an existing file")
    return video, output, config


def _claim_run_dir(requested: Path) -> Path:
    """Atomically claim a unique child directory for one immutable CLI run."""
    requested.mkdir(parents=True, exist_ok=True)
    if not requested.is_dir():
        raise ValueError("output directory conflicts with an existing file")
    for _attempt in range(16):
        candidate = requested / f"run-{uuid4().hex}"
        try:
            candidate.mkdir()
        except FileExistsError:
            continue
        return candidate
    raise FileExistsError(f"could not claim a unique run directory below: {requested}")


def _atomic_json(path: Path, payload: Mapping[str, object]) -> None:
    content = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    _write_new_file_or_reuse_identical(path, content)


def _judge_status(assessment: Mapping[str, object], *, skipped: bool) -> dict[str, object]:
    if skipped:
        return {"requested": False, "status": "SKIPPED"}
    evidence = assessment.get("evidence", [])
    semantic = [
        item for item in evidence
        if isinstance(item, Mapping) and item.get("signal_family") == "semantic_video_critic"
    ] if isinstance(evidence, list) else []
    if not semantic:
        return {"requested": True, "status": "NOT_ROUTED"}
    statuses = [str(item.get("status", "INVALID")) for item in semantic]
    if EvidenceStatus.AVAILABLE.value in statuses:
        status = "COMPLETED"
    elif EvidenceStatus.ABSTAINED.value in statuses:
        status = EvidenceStatus.ABSTAINED.value
    elif EvidenceStatus.UNAVAILABLE.value in statuses:
        status = EvidenceStatus.UNAVAILABLE.value
    else:
        status = EvidenceStatus.INVALID.value
    return {"requested": True, "status": status, "evidence_statuses": statuses}


def run(argv: Sequence[str] | None = None) -> CliRunResult:
    args = _parser().parse_args(argv)
    video, requested_output, config_path = _validate_paths(args.video, args.output_dir, args.config)
    config, frozen_mapping = _load_and_freeze_config(
        config_path, skip_judge=args.skip_judge,
    )
    media_duration = _probe_duration(video)
    resolved_start, resolved_end, clamped = _resolve_interval(args.start, args.duration, media_duration)
    source_before = _sha256(video)

    output_dir = _claim_run_dir(requested_output)
    candidate_id = f"cli-{source_before[:12]}-{round(resolved_start * 1000)}-{round(resolved_end * 1000)}"
    sampled = _sample_exact_interval(video, resolved_start, resolved_end, candidate_id)
    if sampled.status is not EvidenceStatus.AVAILABLE or not sampled.frames:
        code = sampled.diagnostics.get("code", "sampling_unavailable")
        raise ValueError(f"source interval could not be sampled: {code}")
    _save_contact_sheet(output_dir, sampled, kind="original", frames=sampled.frames)
    original_path = _contact_destination(output_dir, sampled, kind="original")

    judge = None if args.skip_judge else OllamaQualityJudge(config, httpx.HTTPTransport())
    candidate = {
        "candidate_id": candidate_id,
        "video_path": str(video),
        "start_ts": resolved_start,
        "end_ts": resolved_end,
        "source_file_sha256": source_before,
    }
    assessment_object = evaluate_candidate(
        candidate, config=config, work_dir=output_dir,
        sampler=lambda *_args, **_kwargs: sampled, judge=judge,
    )
    assessment = assessment_object.to_dict()
    source_after = _sha256(video)
    if source_after != source_before:
        raise RuntimeError("source video hash changed during evaluation")

    best_matches = sorted(output_dir.glob(f"{candidate_id}-*-best-contact-sheet.png"))
    if len(best_matches) > 1:
        raise RuntimeError("multiple best contact sheets were produced for one candidate")
    best_path = best_matches[0] if best_matches else None
    payload: dict[str, object] = {
        "schema_version": "quality-moe-cli-v1",
        "run": {
            "run_id": output_dir.name.removeprefix("run-"),
            "requested_output_dir": str(requested_output),
            "actual_output_dir": str(output_dir.resolve()),
        },
        "source": {
            "path": str(video), "sha256_before": source_before,
            "sha256_after": source_after, "read_only_verified": True,
        },
        "interval": {
            "requested": {"start": float(args.start), "duration": float(args.duration)},
            "resolved": {
                "start": resolved_start, "end": resolved_end,
                "duration": resolved_end - resolved_start,
            },
            "media_duration": media_duration, "clamped": clamped,
        },
        "frozen_config": config.to_dict(),
        "frozen_config_source": str(config_path),
        "judge_execution": _judge_status(assessment, skipped=args.skip_judge),
        "artifacts": {
            "original_contact_sheet": str(original_path.resolve()),
            "best_contact_sheet": str(best_path.resolve()) if best_path else None,
            "best_contact_sheet_status": "PRESENT" if best_path else "ABSENT_NO_VALIDATED_REPAIR",
        },
        "assessment": assessment,
    }
    # Keep the detached mapping live in this function so freezing is explicit;
    # only the strict quality subtree is persisted to avoid unrelated secrets.
    del frozen_mapping
    assessment_path = output_dir / "quality_assessment.json"
    _atomic_json(assessment_path, payload)
    return CliRunResult(output_dir.resolve(), assessment_path.resolve(), payload)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        result = run(argv)
    except Exception as error:
        print(f"quality MoE smoke failed: {error}", file=sys.stderr)
        return 2
    assessment = result.payload.get("assessment", {})
    decision = assessment.get("recommended_decision", "UNKNOWN") if isinstance(assessment, Mapping) else "UNKNOWN"
    judge = result.payload.get("judge_execution", {})
    judge_status = judge.get("status", "UNKNOWN") if isinstance(judge, Mapping) else "UNKNOWN"
    print(f"assessment: {result.assessment_path}")
    print(f"output_dir: {result.output_dir}")
    print(f"decision: {decision}")
    print(f"judge: {judge_status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
