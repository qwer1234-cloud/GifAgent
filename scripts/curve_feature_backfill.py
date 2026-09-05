"""Historical curve-feature backfill (spec 2026-09-04, sections 9.6 and 12).

Extracts versioned body-curve soft features from labeled historical GIFs and
writes them into the ``curve_features`` table of the library database.

Safety contract (spec section 12):

- ``--execute`` absent means dry-run: it only reports scanned counts, file
  availability, projected compute, and skip reasons, and writes nothing.
- Before the first write the database is copied with the SQLite backup API
  and the copy's SHA-256 is recorded in the checkpoint file.
- Writes happen in small committed batches with a checkpoint; a re-run
  processes only rows missing the current feature version.
- A single failure becomes an ``UNAVAILABLE`` row with a stable reason and
  never rolls back completed batches.
- Nothing here touches ``candidate_gifs``, ``favorite_gifs``, or
  ``preference_events``; historical GIF files are only read.

Every SQL statement is an inline literal at its call site and binds all
values through named parameters; table and column names are hardcoded
literals, never user input.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, UTC
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from scripts.continuity_spike_backend_real import (  # noqa: E402
    KEYPOINT_SCORE_MIN,
    POSE_MODEL,
    RealSpikeBackend,
    _pose_valid,
    source_media_dims,
)

FEATURE_VERSION = "curve-v1"
BATCH_SIZE = 25
MAX_GIF_BYTES = 64 * 1024 * 1024
MIN_VALID_FRAMES = 3
KEYPOINTS = 17

_LIMB_TIPS = (9, 10, 15, 16)
_SHOULDERS = (5, 6)
_HIPS = (11, 12)


def _open_read_only(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.execute("PRAGMA query_only = ON")
    return conn


class _DeadDriveCache:
    def __init__(self) -> None:
        self._live: set[str] = set()
        self._dead: set[str] = set()

    def alive(self, path: str) -> bool:
        drive = os.path.splitdrive(path)[0]
        if drive in self._live:
            return True
        if drive in self._dead:
            return False
        try:
            os.stat(drive + os.sep)
        except OSError:
            self._dead.add(drive)
            return False
        self._live.add(drive)
        return True


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _extractor_hash() -> str:
    payload = (
        FEATURE_VERSION.encode("utf-8")
        + Path(__file__).read_bytes()
        + POSE_MODEL.read_bytes()
    )
    return hashlib.sha256(payload).hexdigest()


def _gather_rows(db_path: Path, drives: _DeadDriveCache) -> tuple[list[dict], dict[str, int]]:
    conn = _open_read_only(db_path)
    conn.row_factory = sqlite3.Row
    # Effective training label: a favorite event dominates (the app always
    # logs a like right after a favorite, so recency alone would erase it);
    # otherwise the latest non-undone feedback rating wins.
    rows = conn.execute(
        """
        SELECT cg.candidate_id,
               cg.artifact_path,
               cg.source_video_sha256,
               CASE WHEN EXISTS (
                   SELECT 1 FROM preference_events f
                   WHERE f.target_id = cg.candidate_id
                     AND f.target_type = 'candidate_gif'
                     AND f.event_kind = 'feedback'
                     AND f.undone_at IS NULL
                     AND f.rating = 'favorite'
               ) THEN 'favorite'
               ELSE (
                   SELECT pe.rating FROM preference_events pe
                   WHERE pe.target_id = cg.candidate_id
                     AND pe.target_type = 'candidate_gif'
                     AND pe.event_kind = 'feedback'
                     AND pe.undone_at IS NULL
                     AND pe.rating != 'favorite'
                   ORDER BY pe.created_at DESC, pe.event_id DESC
                   LIMIT 1
               )
               END AS rating
        FROM candidate_gifs cg
        WHERE EXISTS (
            SELECT 1 FROM preference_events pe3
            WHERE pe3.target_id = cg.candidate_id
              AND pe3.target_type = 'candidate_gif'
              AND pe3.event_kind = 'feedback'
              AND pe3.undone_at IS NULL
        )
        ORDER BY cg.candidate_id
        """
    ).fetchall()
    conn.close()

    entries: list[dict] = []
    skip_reasons: dict[str, int] = {}
    for row in rows:
        artifact = row["artifact_path"]
        if not artifact or not artifact.lower().endswith(".gif"):
            skip_reasons["not_gif"] = skip_reasons.get("not_gif", 0) + 1
            continue
        if not drives.alive(artifact):
            skip_reasons["drive_unreachable"] = skip_reasons.get("drive_unreachable", 0) + 1
            continue
        if not os.path.exists(artifact):
            skip_reasons["file_missing"] = skip_reasons.get("file_missing", 0) + 1
            continue
        try:
            if os.stat(artifact).st_size > MAX_GIF_BYTES:
                skip_reasons["oversize"] = skip_reasons.get("oversize", 0) + 1
                continue
        except OSError:
            skip_reasons["stat_failed"] = skip_reasons.get("stat_failed", 0) + 1
            continue
        entries.append(
            {
                "candidate_id": row["candidate_id"],
                "artifact_path": artifact,
                "source_video_sha256": row["source_video_sha256"],
                "rating": row["rating"],
            }
        )
    return entries, skip_reasons


def count_torso_border_clipped(
    people: list,
    width: int,
    height: int,
) -> int:
    """Count people with any confident torso joint at the frame border.

    Keypoints live in the analysis-frame coordinate system, so ``width`` and
    ``height`` must be the analysis dimensions, never the source media
    dimensions.
    """

    clipped = 0
    for person in people:
        for index in _SHOULDERS + _HIPS:
            x, y, conf = person.keypoint(index)
            if conf >= KEYPOINT_SCORE_MIN and (
                x <= 2 or y <= 2 or x >= width - 2 or y >= height - 2
            ):
                clipped += 1
                break
    return clipped


def _extract_features(
    backend: RealSpikeBackend,
    artifact: Path,
) -> tuple[dict[str, float] | None, str | None, int, int, int]:
    """Return (features, missing_reason, width, height, frame_count)."""

    frames = backend._extract_gif_frames(artifact)
    if len(frames) < MIN_VALID_FRAMES:
        source_dims = source_media_dims(artifact)
        if source_dims:
            width, height = source_dims
        else:
            analysis_height, analysis_width = frames[0].shape[:2]
            width, height = int(analysis_width), int(analysis_height)
        return None, "pose_coverage_low", width, height, len(frames)
    analysis_height, analysis_width = frames[0].shape[:2]
    # Result dims must describe the source media, not the rescaled
    # analysis frames; area ratios below are computed on analysis frames
    # and are aspect-preserving scale-invariant.
    source_dims = source_media_dims(artifact)
    width, height = source_dims if source_dims else (
        int(analysis_width), int(analysis_height)
    )

    persons = [backend._detect_persons(frame) for frame in frames]
    valid_people = [
        frame_persons[0]
        for frame_persons in persons
        if frame_persons and _pose_valid(frame_persons[0])
    ]
    if len(valid_people) < MIN_VALID_FRAMES:
        if not any(persons):
            return None, "no_person_detected", width, height, len(frames)
        return None, "pose_coverage_low", width, height, len(frames)

    torso_scales: list[float] = []
    extensions: list[float] = []
    visibilities: list[float] = []
    centers: list[tuple[float, float]] = []
    verticalities: list[float] = []
    for person in valid_people:
        sx = (person.keypoint(_SHOULDERS[0])[0] + person.keypoint(_SHOULDERS[1])[0]) / 2
        sy = (person.keypoint(_SHOULDERS[0])[1] + person.keypoint(_SHOULDERS[1])[1]) / 2
        hx = (person.keypoint(_HIPS[0])[0] + person.keypoint(_HIPS[1])[0]) / 2
        hy = (person.keypoint(_HIPS[0])[1] + person.keypoint(_HIPS[1])[1]) / 2
        torso = float(np.hypot(sx - hx, sy - hy))
        if torso < 4.0:
            continue
        torso_scales.append(torso)
        centers.append(((sx + hx) / 2, (sy + hy) / 2))
        verticalities.append(abs(float(np.arctan2(sx - hx, hy - sy))))

        tips = [
            person.keypoint(index)
            for index in _LIMB_TIPS
            if person.keypoint(index)[2] >= KEYPOINT_SCORE_MIN
        ]
        if len(tips) >= 2:
            xs = [tip[0] for tip in tips]
            extensions.append((max(xs) - min(xs)) / torso)
        visibilities.append(
            sum(
                1 for index in range(KEYPOINTS)
                if person.keypoint(index)[2] >= KEYPOINT_SCORE_MIN
            ) / KEYPOINTS
        )
    clipped = count_torso_border_clipped(valid_people, analysis_width, analysis_height)

    if len(torso_scales) < MIN_VALID_FRAMES:
        return None, "pose_coverage_low", width, height, len(frames)

    torso_scale = float(np.median(torso_scales))
    speeds = [
        float(np.hypot(x1 - x0, y1 - y0) / torso_scale)
        for (x0, y0), (x1, y1) in zip(centers, centers[1:])
    ]
    speed_arr = np.asarray(speeds, dtype=np.float64) if speeds else np.zeros(1)
    peak_speed = float(np.percentile(speed_arr, 95))
    if len(speed_arr) >= 2:
        jerk = float(np.mean(np.abs(np.diff(speed_arr))))
        smoothness = float(np.clip(1.0 - jerk / (peak_speed + 1e-6), 0.0, 1.0))
    else:
        smoothness = 0.0

    ratios = [p.area / max(analysis_width * analysis_height, 1.0) for p in valid_people]
    features = {
        "pose_valid_ratio": round(len(valid_people) / len(frames), 6),
        "subject_area_ratio": round(float(np.median(ratios)), 6),
        "extension_mean": round(float(np.mean(extensions)), 6) if extensions else 0.0,
        "extension_peak": round(float(np.percentile(extensions, 90)), 6) if extensions else 0.0,
        "extension_range": round(
            float(np.percentile(extensions, 90) - np.percentile(extensions, 10)), 6
        )
        if len(extensions) >= 2
        else 0.0,
        "torso_speed_p95": round(peak_speed, 6),
        "motion_smoothness": round(smoothness, 6),
        "visibility_ratio": round(float(np.mean(visibilities)), 6) if visibilities else 0.0,
        "border_clip_ratio": round(clipped / len(valid_people), 6),
        "verticality_mean": round(float(np.mean(verticalities)), 6) if verticalities else 0.0,
    }
    return features, None, width, height, len(frames)


def _backup(db_path: Path) -> tuple[Path, str]:
    backup_dir = REPO / "data" / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    backup_path = backup_dir / f"library.before-curve-backfill.{stamp}.db"
    source = _open_read_only(db_path)
    target = sqlite3.connect(str(backup_path))
    source.backup(target)
    target.close()
    source.close()
    return backup_path, _sha256_file(backup_path)


def _load_checkpoint(path: Path) -> dict:
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def _save_checkpoint(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=1), encoding="utf-8")


def _feature_row(
    entry: dict,
    availability: str,
    features_json: str | None,
    missing_reason: str | None,
    extractor: str,
    artifact_sha: str,
    wall_ms: float,
    width: int | None,
    height: int | None,
    frames: int | None,
) -> dict:
    return {
        "candidate_id": entry["candidate_id"],
        "feature_version": FEATURE_VERSION,
        "availability": availability,
        "features_json": features_json,
        "missing_reason": missing_reason,
        "extractor_sha256": extractor,
        "artifact_sha256": artifact_sha,
        "wall_clock_ms": wall_ms,
        "source_video_sha256": entry["source_video_sha256"],
        "width": width,
        "height": height,
        "frame_count": frames,
    }


def repair_dims(db_path: Path, drives: _DeadDriveCache) -> int:
    """Rewrite width/height metadata of stored rows from the source files.

    Earlier runs recorded rescaled analysis-frame dimensions; the feature
    values are unaffected (area ratios are scale-invariant), only the
    recorded media dimensions are corrected. Returns the number of repaired
    rows.
    """

    conn = _open_read_only(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT cf.candidate_id, cg.artifact_path, cf.width, cf.height "
        "FROM curve_features cf "
        "JOIN candidate_gifs cg ON cg.candidate_id = cf.candidate_id "
        "WHERE cf.feature_version = :feature_version",
        {"feature_version": FEATURE_VERSION},
    ).fetchall()
    conn.close()

    writer = sqlite3.connect(str(db_path), timeout=30)
    repaired = 0
    try:
        for row in rows:
            artifact = row["artifact_path"]
            if not artifact or not drives.alive(artifact):
                continue
            if not os.path.exists(artifact):
                continue
            dims = source_media_dims(Path(artifact))
            if dims is None:
                continue
            if dims != (row["width"], row["height"]):
                writer.execute(
                    "UPDATE curve_features SET width = :width, height = :height "
                    "WHERE candidate_id = :candidate_id "
                    "AND feature_version = :feature_version",
                    {
                        "width": dims[0],
                        "height": dims[1],
                        "candidate_id": row["candidate_id"],
                        "feature_version": FEATURE_VERSION,
                    },
                )
                repaired += 1
                if repaired % 500 == 0:
                    writer.commit()
                    print(f"  repaired {repaired} rows so far", flush=True)
        writer.commit()
    finally:
        writer.close()
    return repaired


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--db", type=Path, default=REPO / "dist" / "GifAgentUI" / "data" / "library.db"
    )
    parser.add_argument("--execute", action="store_true", help="write rows; default is dry-run")
    parser.add_argument("--limit", type=int, default=0, help="process at most N GIFs")
    parser.add_argument(
        "--checkpoint", type=Path, default=REPO / "data" / "curve_backfill_checkpoint.json"
    )
    parser.add_argument("--fresh", action="store_true", help="ignore prior checkpoint progress")
    parser.add_argument(
        "--repair-dims", action="store_true",
        help="rewrite stored width/height metadata from the source files and exit",
    )
    args = parser.parse_args()

    drives = _DeadDriveCache()
    if args.repair_dims:
        repaired = repair_dims(args.db, drives)
        print(f"repaired {repaired} rows' media dimensions")
        return 0
    entries, skip_reasons = _gather_rows(args.db, drives)
    rating_counts: dict[str, int] = {}
    for entry in entries:
        rating_counts[entry["rating"]] = rating_counts.get(entry["rating"], 0) + 1

    print(f"labeled rows with existing files: {len(entries)}")
    print(f"per rating: {rating_counts}")
    print(f"skips: {skip_reasons}")
    print(f"projected compute (0B mean 761 ms/GIF): {len(entries) * 761 / 3_600_000:.2f} h")

    if not args.execute:
        print("DRY-RUN complete; nothing was written.")
        return 0

    checkpoint = {} if args.fresh else _load_checkpoint(args.checkpoint)
    extractor = _extractor_hash()
    print(f"extractor_sha256: {extractor}")

    conn = sqlite3.connect(str(args.db), timeout=30)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS curve_features (
            candidate_id TEXT NOT NULL,
            feature_version TEXT NOT NULL,
            availability TEXT NOT NULL CHECK (availability IN ('AVAILABLE', 'UNAVAILABLE')),
            features_json TEXT,
            missing_reason TEXT,
            extractor_sha256 TEXT NOT NULL,
            artifact_sha256 TEXT NOT NULL,
            wall_clock_ms REAL,
            source_video_sha256 TEXT,
            width INTEGER,
            height INTEGER,
            frame_count INTEGER,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            PRIMARY KEY (candidate_id, feature_version)
        )
        """
    )
    conn.commit()
    done = {
        row[0]
        for row in conn.execute(
            "SELECT candidate_id FROM curve_features WHERE feature_version = :feature_version",
            {"feature_version": FEATURE_VERSION},
        ).fetchall()
    }
    pending = [e for e in entries if e["candidate_id"] not in done]
    if checkpoint.get("last_candidate_id"):
        pending = [
            e for e in pending if e["candidate_id"] > checkpoint["last_candidate_id"]
        ]
    if args.limit:
        pending = pending[: args.limit]
    print(f"to process: {len(pending)} (already done: {len(done)})")

    if not checkpoint.get("backup_sha256"):
        backup_path, backup_sha = _backup(args.db)
        print(f"backup: {backup_path}")
        print(f"backup sha256: {backup_sha}")
        checkpoint["backup_path"] = str(backup_path)
        checkpoint["backup_sha256"] = backup_sha
        checkpoint["feature_version"] = FEATURE_VERSION
        checkpoint["extractor_sha256"] = extractor
        _save_checkpoint(args.checkpoint, checkpoint)

    backend = RealSpikeBackend()
    available = 0
    unavailable = 0
    batch: list[dict] = []
    started = time.perf_counter_ns()

    def flush() -> None:
        if batch:
            conn.executemany(
                """
                INSERT OR REPLACE INTO curve_features (
                    candidate_id, feature_version, availability, features_json,
                    missing_reason, extractor_sha256, artifact_sha256,
                    wall_clock_ms, source_video_sha256, width, height,
                    frame_count, created_at
                ) VALUES (
                    :candidate_id, :feature_version, :availability, :features_json,
                    :missing_reason, :extractor_sha256, :artifact_sha256,
                    :wall_clock_ms, :source_video_sha256, :width, :height,
                    :frame_count, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
                """,
                batch,
            )
            conn.commit()
            batch.clear()

    for index, entry in enumerate(pending):
        artifact = Path(entry["artifact_path"])
        start_ns = time.perf_counter_ns()
        try:
            artifact_sha = _sha256_file(artifact)
            features, missing, width, height, frames = _extract_features(
                backend, artifact
            )
            wall_ms = (time.perf_counter_ns() - start_ns) / 1_000_000
            if features is None:
                unavailable += 1
                row = _feature_row(
                    entry, "UNAVAILABLE", None, missing, extractor,
                    artifact_sha, wall_ms, width, height, frames,
                )
            else:
                available += 1
                row = _feature_row(
                    entry, "AVAILABLE", json.dumps(features, allow_nan=False),
                    None, extractor, artifact_sha, wall_ms, width, height, frames,
                )
        except Exception as exc:
            wall_ms = (time.perf_counter_ns() - start_ns) / 1_000_000
            unavailable += 1
            row = _feature_row(
                entry, "UNAVAILABLE", None, "decode_failed", extractor,
                "unhashable", wall_ms, None, None, None,
            )
            print(f"  error {entry['candidate_id']}: {exc}", flush=True)
        batch.append(row)

        if len(batch) >= BATCH_SIZE:
            flush()
            checkpoint["last_candidate_id"] = entry["candidate_id"]
            checkpoint["processed"] = checkpoint.get("processed", 0) + BATCH_SIZE
            checkpoint["available"] = checkpoint.get("available", 0) + available
            checkpoint["unavailable"] = checkpoint.get("unavailable", 0) + unavailable
            available = unavailable = 0
            _save_checkpoint(args.checkpoint, checkpoint)
            if (index + 1) % 250 == 0:
                elapsed_s = (time.perf_counter_ns() - started) / 1e9
                rate = (index + 1) / max(elapsed_s, 1e-6)
                remaining_h = (len(pending) - index - 1) / max(rate, 1e-6) / 3600
                print(
                    f"  {index + 1}/{len(pending)} done ({rate:.2f} gif/s, "
                    f"~{remaining_h:.1f} h remaining)",
                    flush=True,
                )

    flush()
    if pending:
        checkpoint["last_candidate_id"] = pending[-1]["candidate_id"]
    checkpoint["finished_at"] = datetime.now(UTC).isoformat()
    _save_checkpoint(args.checkpoint, checkpoint)
    total_in_table = conn.execute(
        "SELECT COUNT(*) FROM curve_features WHERE feature_version = :feature_version",
        {"feature_version": FEATURE_VERSION},
    ).fetchone()[0]
    conn.close()
    print(
        f"backfill complete: +{len(pending)} rows "
        f"(available {available + checkpoint.get('available', 0)}, "
        f"unavailable {unavailable + checkpoint.get('unavailable', 0)}); "
        f"table now holds {total_in_table} rows for {FEATURE_VERSION}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
