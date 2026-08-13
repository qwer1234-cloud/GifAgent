"""Bounded, pixel-preserving repair search for quality-MoE proxy clips.

The search certifies only proxies rendered by the same fixed FFmpeg filter used
for final output.  It uses one immutable recipe for every frame in a candidate
interval and writes only optional contact sheets to a caller-owned directory.
"""

from __future__ import annotations

from dataclasses import dataclass
import errno
import hashlib
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
from uuid import uuid4

import cv2
import numpy as np

from app.quality_moe.config import QualityMoeConfig
from app.quality_moe.experts import CinematicExpert, TechnicalAestheticExpert, TemporalExpert
from app.quality_moe.models import (
    EvidencePolarity,
    EvidenceStatus,
    ExpertEvidence,
    RepairRecipe,
    RepairValidation,
)
from app.quality_moe.sampling import SampledClip


_MAX_FPS = 120
_MAX_WIDTH = 8192
_EPSILON = 1e-6
_RECIPE_FIELDS = frozenset(RepairRecipe.__dataclass_fields__)
_PROXY_FPS = 12
_RENDER_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True)
class RepairSearchResult:
    """Measured repair candidates and the single proxy that passed validation."""

    evaluated_recipes: tuple[RepairRecipe, ...]
    best_recipe: RepairRecipe | None
    repair_delta: ExpertEvidence | None
    source_technical: ExpertEvidence
    source_cinematic: ExpertEvidence
    source_temporal: ExpertEvidence
    render_failures: tuple["RepairRenderFailure", ...] = ()
    best_proxy: SampledClip | None = None

    @property
    def unavailable_recipes(self) -> tuple["RepairRenderFailure", ...]:
        """Compatibility name for recipes unavailable due to render failure."""
        return self.render_failures


@dataclass(frozen=True)
class RepairRenderFailure:
    recipe_id: str
    error_code: str
    summary: str

    def __post_init__(self) -> None:
        if not isinstance(self.recipe_id, str) or not self.recipe_id:
            raise ValueError("recipe_id must be a non-empty string")
        if not isinstance(self.error_code, str) or not self.error_code:
            raise ValueError("error_code must be a non-empty string")
        if not isinstance(self.summary, str) or not self.summary:
            raise ValueError("summary must be a non-empty string")


class RepairProxyRenderError(RuntimeError):
    """A proxy could not be rendered, so it cannot support certification."""

    def __init__(self, error_code: str, summary: str) -> None:
        super().__init__(summary)
        self.error_code = error_code
        self.summary = summary


def approved_recipes() -> tuple[RepairRecipe, ...]:
    """Return the finite, clip-global v1 search grid (never more than twelve)."""
    settings = (
        {"exposure_ev": -0.75},
        {"exposure_ev": -0.35},
        {"exposure_ev": 0.35},
        {"exposure_ev": 0.75},
        {"gamma": 0.85},
        {"gamma": 1.15},
        {"contrast": -0.10},
        {"contrast": 0.10},
        {"shadows": 0.15},
        {"highlights": -0.15},
        {"white_balance": (1.08, 1.00, 0.92)},
        {"white_balance": (0.92, 1.00, 1.08)},
    )
    recipes = tuple(
        RepairRecipe(recipe_id=f"photometric-{index + 1:02d}", **values).validate()
        for index, values in enumerate(settings)
    )
    if len(recipes) > 12:  # Defensive invariant if the search grid changes.
        raise RuntimeError("repair search exceeds its bounded variant budget")
    return recipes


def _assert_recipe_is_safe(recipe: RepairRecipe) -> RepairRecipe:
    if not isinstance(recipe, RepairRecipe):
        raise ValueError("repair recipe must be a RepairRecipe")
    if set(vars(recipe)) != _RECIPE_FIELDS:
        raise ValueError("repair recipe has unknown fields")
    recipe.validate()
    if (
        recipe.crop != (0.0, 0.0, 1.0, 1.0)
        or recipe.zoom != 1.0
        or recipe.rotation_degrees != 0.0
        or recipe.perspective_corner_movement != 0.0
    ):
        raise ValueError("v1 repair accepts photometric recipes only")
    return recipe


def apply_recipe_to_frame(frame: np.ndarray, recipe: RepairRecipe) -> np.ndarray:
    """Apply a fixed, non-generative photometric recipe to one BGR uint8 frame."""
    _assert_recipe_is_safe(recipe)
    if not isinstance(frame, np.ndarray) or frame.ndim != 3 or frame.shape[2] != 3:
        raise ValueError("frame must be a BGR image array")
    if frame.dtype != np.uint8 or frame.size == 0:
        raise ValueError("frame must be a non-empty uint8 array")

    pixels = frame.astype(np.float32) / 255.0
    pixels = np.clip(pixels * (2.0 ** recipe.exposure_ev), 0.0, 1.0)
    pixels = np.power(pixels, recipe.gamma)
    pixels = (pixels - 0.5) * (1.0 + recipe.contrast) + 0.5
    pixels = pixels + recipe.shadows * np.square(1.0 - pixels)
    pixels = pixels + recipe.highlights * np.square(pixels)
    # Sampled frames are BGR, so the recipe's channel factors are BGR too.
    pixels = pixels * np.asarray(recipe.white_balance, dtype=np.float32)
    return np.rint(np.clip(pixels, 0.0, 1.0) * 255.0).astype(np.uint8)


def _proxy_filter(sampled_clip: SampledClip, recipe: RepairRecipe) -> str:
    return build_ffmpeg_filter(
        recipe,
        fps=_PROXY_FPS,
        max_width=sampled_clip.frames[0].shape[1],
    )


def render_recipe_proxy(
    sampled_clip: SampledClip,
    recipe: RepairRecipe,
    *,
    timeout_seconds: float = _RENDER_TIMEOUT_SECONDS,
) -> SampledClip:
    """Render a complete proxy sequence through the certified FFmpeg filter."""
    if not isinstance(sampled_clip, SampledClip) or sampled_clip.status is not EvidenceStatus.AVAILABLE:
        raise ValueError("rendering requires an available SampledClip")
    _assert_recipe_is_safe(recipe)
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(float(timeout_seconds))
        or not 0.1 <= float(timeout_seconds) <= _RENDER_TIMEOUT_SECONDS
    ):
        raise ValueError(f"timeout_seconds must be finite and in [0.1, {_RENDER_TIMEOUT_SECONDS}]")
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RepairProxyRenderError("ffmpeg_unavailable", "FFmpeg is unavailable for repair proxy rendering.")
    filter_graph = _proxy_filter(sampled_clip, recipe)
    with tempfile.TemporaryDirectory(prefix="gifagent-quality-repair-") as temp_dir:
        root = Path(temp_dir)
        source_pattern = root / "source-%06d.png"
        output_pattern = root / "rendered-%06d.png"
        for index, frame in enumerate(sampled_clip.frames):
            source_path = root / f"source-{index:06d}.png"
            if not cv2.imwrite(str(source_path), frame):
                raise RepairProxyRenderError("source_frame_write_failed", "Could not write isolated repair proxy input.")
        try:
            completed = subprocess.run(
                [
                    ffmpeg, "-v", "error", "-y", "-framerate", str(_PROXY_FPS),
                    "-start_number", "0", "-i", str(source_pattern),
                    "-frames:v", str(len(sampled_clip.frames)), "-vf", filter_graph,
                    str(output_pattern),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=float(timeout_seconds),
            )
        except subprocess.TimeoutExpired as exc:
            raise RepairProxyRenderError(
                "timeout", "FFmpeg repair proxy render timed out."
            ) from exc
        except OSError as exc:
            raise RepairProxyRenderError(
                "ffmpeg_launch_failed", "FFmpeg repair proxy renderer could not start."
            ) from exc
        if completed.returncode != 0:
            raise RepairProxyRenderError("ffmpeg_failed", "FFmpeg repair proxy render failed.")
        output_paths = tuple(sorted(root.glob("rendered-*.png")))
        if len(output_paths) != len(sampled_clip.frames):
            raise RepairProxyRenderError("unexpected_frame_count", "FFmpeg repair proxy returned an unexpected frame count.")
        frames = tuple(cv2.imread(str(path), cv2.IMREAD_COLOR) for path in output_paths)
        if any(frame is None for frame in frames):
            raise RepairProxyRenderError("unreadable_frame", "FFmpeg repair proxy produced unreadable output.")
    return SampledClip(
        candidate_id=sampled_clip.candidate_id,
        video_path=sampled_clip.video_path,
        start_ts=sampled_clip.start_ts,
        end_ts=sampled_clip.end_ts,
        timestamps=sampled_clip.timestamps,
        frames=tuple(np.ascontiguousarray(frame) for frame in frames),
        semantic_labels=sampled_clip.semantic_labels,
    )


def _quality(technical: ExpertEvidence, cinematic: ExpertEvidence) -> float:
    return 0.90 * technical.scores["technical_integrity"] + 0.10 * cinematic.scores["color_balance"]


def _safety_passes(
    source_technical: ExpertEvidence,
    source_temporal: ExpertEvidence,
    proxy_technical: ExpertEvidence,
    proxy_temporal: ExpertEvidence,
) -> bool:
    return (
        proxy_technical.scores["clipping"] + _EPSILON >= source_technical.scores["clipping"]
        and proxy_technical.scores["sharpness"] + _EPSILON >= source_technical.scores["sharpness"]
        and proxy_temporal.scores["temporal_coherence"] + _EPSILON >= source_temporal.scores["temporal_coherence"]
    )


def _validated_recipe(
    recipe: RepairRecipe,
    *,
    sampled_clip: SampledClip,
    proxy: SampledClip,
    config: QualityMoeConfig,
    source_quality: float,
    proxy_quality: float,
    source_temporal: float,
    proxy_temporal: float,
    quality_gain: float,
    confidence: float,
) -> tuple[RepairRecipe, ExpertEvidence]:
    measured = RepairRecipe(
        recipe_id=recipe.recipe_id,
        exposure_ev=recipe.exposure_ev,
        gamma=recipe.gamma,
        contrast=recipe.contrast,
        shadows=recipe.shadows,
        highlights=recipe.highlights,
        white_balance=recipe.white_balance,
        quality_gain=quality_gain,
        confidence=confidence,
    ).validate()
    delta = ExpertEvidence(
        candidate_id=sampled_clip.candidate_id,
        evaluation_version=config.evaluation_version,
        expert_id="repair_validation",
        expert_version="deterministic-v1",
        signal_family="repair_delta",
        status=EvidenceStatus.AVAILABLE,
        scores={
            "source_quality": source_quality,
            "proxy_quality": proxy_quality,
            "quality_gain": quality_gain,
            "validation_confidence": confidence,
            "source_temporal_coherence": source_temporal,
            "proxy_temporal_coherence": proxy_temporal,
        },
        findings=({
            "code": "measured_proxy_gain",
            "recipe_id": measured.recipe_id,
            "ffmpeg_filter": _proxy_filter(sampled_clip, recipe),
        },),
        summary="Measured quality gain from the source proxy and one rendered repair proxy.",
        input_hash=proxy.input_hash,
        parent_input_hash=sampled_clip.input_hash,
        config_hash=config.config_hash,
        polarity=EvidencePolarity.POSITIVE,
    )
    validation = RepairValidation(
        candidate_id=sampled_clip.candidate_id,
        evaluation_version=config.evaluation_version,
        source_input_hash=sampled_clip.input_hash,
        proxy_artifact_hash=proxy.input_hash,
        recipe_hash=measured.recipe_hash,
        config_hash=config.config_hash,
        repair_delta_evidence_id=delta.identity_hash,
        repair_delta_status=delta.status,
    )
    return RepairRecipe(
        recipe_id=measured.recipe_id,
        exposure_ev=measured.exposure_ev,
        gamma=measured.gamma,
        contrast=measured.contrast,
        shadows=measured.shadows,
        highlights=measured.highlights,
        white_balance=measured.white_balance,
        quality_gain=quality_gain,
        confidence=confidence,
        validation=validation,
    ).validate(), delta


def _contact_destination(
    work_dir: str | Path,
    sampled_clip: SampledClip,
    *,
    kind: str,
    config: QualityMoeConfig | None = None,
    recipe: RepairRecipe | None = None,
    proxy: SampledClip | None = None,
) -> Path:
    destination = Path(work_dir)
    destination.mkdir(parents=True, exist_ok=True)
    if not destination.is_dir():
        raise ValueError("work_dir must be a directory")
    candidate = re.sub(r"[^A-Za-z0-9_-]+", "-", sampled_clip.candidate_id).strip("-")
    source_hash = sampled_clip.input_hash[:12]
    if kind == "original":
        name = f"{candidate}-{source_hash}-original-contact-sheet.png"
    elif kind == "best":
        if config is None or recipe is None or proxy is None:
            raise ValueError("best contact sheets require config, recipe, and proxy identity")
        name = (
            f"{candidate}-{source_hash}-{config.config_hash[:12]}-"
            f"{recipe.recipe_hash[:12]}-{proxy.input_hash[:12]}-best-contact-sheet.png"
        )
    else:
        raise ValueError("contact sheet kind must be original or best")
    target = destination / name
    if "://" not in sampled_clip.video_path:
        source = Path(sampled_clip.video_path).resolve(strict=False)
        if target.resolve(strict=False) == source:
            raise ValueError("contact sheet destination must not equal source media")
    return target


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _stable_file_hash(path: Path) -> str:
    """Hash an existing competing file only after two stable observations."""
    observed = False
    for _attempt in range(20):
        try:
            before = path.stat()
            observed = True
            digest = _file_hash(path)
            after = path.stat()
        except FileNotFoundError:
            time.sleep(0.005)
            continue
        if before.st_size == after.st_size and before.st_mtime_ns == after.st_mtime_ns:
            return digest
        time.sleep(0.005)
    if not observed:
        raise FileNotFoundError(path)
    raise FileExistsError(f"contact sheet target is not in a stable state: {path}")


def _write_complete_temp_file(target: Path, content: bytes) -> Path:
    temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
        0o600,
    )
    try:
        written = 0
        while written < len(content):
            count = os.write(descriptor, content[written:])
            if count <= 0:
                raise OSError("could not complete contact sheet temporary write")
            written += count
        os.fsync(descriptor)
    except BaseException:
        os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise
    else:
        os.close(descriptor)
        return temporary


def _link_is_unsupported(error: OSError) -> bool:
    return error.errno in {errno.EPERM, errno.EOPNOTSUPP, errno.ENOSYS, errno.EXDEV} or getattr(error, "winerror", None) in {1, 50}


def _try_advisory_lock(descriptor: int) -> bool:
    if os.name == "nt":
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    import fcntl

    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _release_advisory_lock(descriptor: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(descriptor, fcntl.LOCK_UN)


def _publish_via_locked_replace(temporary: Path, target: Path, content_hash: str) -> None:
    """Fallback for filesystems without hard links; the OS releases dead locks."""
    lock = target.with_name(f".{target.name}.publish.lock")
    deadline = time.monotonic() + 1.0
    descriptor = os.open(
        lock,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0),
        0o600,
    )
    if os.fstat(descriptor).st_size == 0:
        os.write(descriptor, b"\0")
        os.fsync(descriptor)
    acquired = False
    try:
        while not acquired:
            acquired = _try_advisory_lock(descriptor)
            if not acquired:
                if time.monotonic() >= deadline:
                    raise FileExistsError(
                        f"contact sheet publication lock timed out: {target}"
                    )
                time.sleep(0.01)
        try:
            existing_hash = _stable_file_hash(target)
        except FileNotFoundError:
            os.replace(temporary, target)
            return
        if existing_hash == content_hash:
            return
        raise FileExistsError(f"contact sheet already exists with different content: {target}")
    finally:
        if acquired:
            _release_advisory_lock(descriptor)
        os.close(descriptor)


def _write_new_file_or_reuse_identical(target: Path, content: bytes) -> None:
    """Publish a complete file with atomic fail-if-exists semantics."""
    content_hash = hashlib.sha256(content).hexdigest()
    temporary = _write_complete_temp_file(target, content)
    try:
        try:
            os.link(temporary, target)
        except FileExistsError:
            if _stable_file_hash(target) != content_hash:
                raise FileExistsError(f"contact sheet already exists with different content: {target}")
        except OSError as error:
            if not _link_is_unsupported(error):
                raise
            _publish_via_locked_replace(temporary, target, content_hash)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _save_contact_sheet(
    work_dir: str | Path,
    sampled_clip: SampledClip,
    *,
    kind: str,
    frames: tuple[np.ndarray, ...],
    config: QualityMoeConfig | None = None,
    recipe: RepairRecipe | None = None,
    proxy: SampledClip | None = None,
) -> None:
    target = _contact_destination(
        work_dir, sampled_clip, kind=kind, config=config, recipe=recipe, proxy=proxy,
    )
    height = min(frame.shape[0] for frame in frames)
    tiles = tuple(
        frame if frame.shape[0] == height else cv2.resize(frame, (round(frame.shape[1] * height / frame.shape[0]), height))
        for frame in frames
    )
    encoded, buffer = cv2.imencode(".png", cv2.hconcat(tiles))
    if not encoded:
        raise OSError(f"failed to encode {target.name}")
    _write_new_file_or_reuse_identical(target, buffer.tobytes())


def search_repairs(
    sampled_clip: SampledClip,
    config: QualityMoeConfig,
    *,
    work_dir: str | Path | None = None,
) -> RepairSearchResult:
    """Evaluate at most twelve clip-global recipes and retain only validated repair."""
    if not isinstance(sampled_clip, SampledClip):
        raise ValueError("sampled_clip must be a SampledClip")
    if not isinstance(config, QualityMoeConfig):
        raise ValueError("config must be a QualityMoeConfig")
    technical = TechnicalAestheticExpert(config)
    cinematic = CinematicExpert(config)
    temporal = TemporalExpert(config)
    source_technical = technical.evaluate(sampled_clip)
    source_cinematic = cinematic.evaluate(sampled_clip)
    source_temporal = temporal.evaluate(sampled_clip)
    if work_dir is not None and sampled_clip.status is EvidenceStatus.AVAILABLE:
        _save_contact_sheet(work_dir, sampled_clip, kind="original", frames=sampled_clip.frames)

    if not config.repairability.enabled or sampled_clip.status is not EvidenceStatus.AVAILABLE:
        return RepairSearchResult((), None, None, source_technical, source_cinematic, source_temporal)

    candidates = approved_recipes()[: config.repairability.max_proxy_variants]
    evaluated: list[RepairRecipe] = []
    render_failures: list[RepairRenderFailure] = []
    best: tuple[RepairRecipe, ExpertEvidence, SampledClip] | None = None
    source_quality = _quality(source_technical, source_cinematic)
    for recipe in candidates:
        try:
            proxy = render_recipe_proxy(sampled_clip, recipe)
        except RepairProxyRenderError as error:
            render_failures.append(RepairRenderFailure(recipe.recipe_id, error.error_code, error.summary))
            continue
        proxy_technical = technical.evaluate(proxy)
        proxy_cinematic = cinematic.evaluate(proxy)
        proxy_temporal = temporal.evaluate(proxy)
        if any(item.status is not EvidenceStatus.AVAILABLE for item in (proxy_technical, proxy_cinematic, proxy_temporal)):
            evaluated.append(recipe)
            continue
        gain = max(0.0, _quality(proxy_technical, proxy_cinematic) - source_quality)
        confidence = min(1.0, 0.80 * gain / config.repairability.min_quality_gain)
        measured = RepairRecipe(
            recipe_id=recipe.recipe_id,
            exposure_ev=recipe.exposure_ev,
            gamma=recipe.gamma,
            contrast=recipe.contrast,
            shadows=recipe.shadows,
            highlights=recipe.highlights,
            white_balance=recipe.white_balance,
            quality_gain=gain,
            confidence=confidence,
        ).validate()
        evaluated.append(measured)
        if (
            gain + _EPSILON < config.repairability.min_quality_gain
            or confidence + _EPSILON < config.repairability.min_confidence
            or not _safety_passes(source_technical, source_temporal, proxy_technical, proxy_temporal)
        ):
            continue
        validated, delta = _validated_recipe(
            recipe,
            sampled_clip=sampled_clip,
            proxy=proxy,
            config=config,
            source_quality=source_quality,
            proxy_quality=_quality(proxy_technical, proxy_cinematic),
            source_temporal=source_temporal.scores["temporal_coherence"],
            proxy_temporal=proxy_temporal.scores["temporal_coherence"],
            quality_gain=gain,
            confidence=confidence,
        )
        if best is None or validated.quality_gain > best[0].quality_gain:
            best = (validated, delta, proxy)

    if best is not None and work_dir is not None:
        _save_contact_sheet(
            work_dir, sampled_clip, kind="best", frames=best[2].frames,
            config=config, recipe=best[0], proxy=best[2],
        )
    return RepairSearchResult(
        tuple(evaluated), best[0] if best else None, best[1] if best else None,
        source_technical, source_cinematic, source_temporal, tuple(render_failures),
        best[2] if best else None,
    )


def _format(value: float) -> str:
    return f"{value:.6f}"


def recipe_to_safe_filters(recipe: RepairRecipe) -> tuple[str, ...]:
    """Serialize only validated numeric recipe values to a fixed FFmpeg grammar."""
    recipe = _assert_recipe_is_safe(recipe)
    brightness = (2.0 ** recipe.exposure_ev - 1.0) / 2.0
    contrast = 1.0 + recipe.contrast
    shadow_point = 0.25 + recipe.shadows * 0.25
    # A negative value is the approved highlight-compression direction.
    highlight_point = 0.75 + recipe.highlights * 0.25
    blue, green, red = recipe.white_balance
    return (
        f"eq=brightness={_format(brightness)}:contrast={_format(contrast)}:gamma={_format(recipe.gamma)}",
        f"curves=all='0/0 0.25/{_format(shadow_point)} 0.75/{_format(highlight_point)} 1/1'",
        f"colorchannelmixer=rr={_format(red)}:gg={_format(green)}:bb={_format(blue)}",
    )


def build_ffmpeg_filter(recipe: RepairRecipe | None, *, fps: int, max_width: int) -> str:
    """Build the one safe prefix shared by palette creation and GIF encoding."""
    if isinstance(fps, bool) or not isinstance(fps, int) or not 1 <= fps <= _MAX_FPS:
        raise ValueError(f"fps must be an integer in [1, {_MAX_FPS}]")
    if isinstance(max_width, bool) or not isinstance(max_width, int) or not 1 <= max_width <= _MAX_WIDTH:
        raise ValueError(f"max_width must be an integer in [1, {_MAX_WIDTH}]")
    filters = [f"fps={fps}"]
    if recipe is not None:
        filters.extend(recipe_to_safe_filters(recipe))
    filters.append(f"scale={max_width}:-1:flags=lanczos")
    return ",".join(filters)
