"""Best-effort library ingest for published pipeline GIFs.

GIF export / materialize must succeed even when the library DB or embedding
service is unavailable. Callers never rebuild preference profiles here.
"""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
from typing import Any

from app.services.candidate_capture import link_publication
from app.services.candidates import CandidateService

logger = logging.getLogger(__name__)

_SOURCE_SHA_SENTINELS = frozenset({"not_checked", "unavailable"})


def library_ingest_enabled(cfg: dict[str, Any] | None = None) -> bool:
    """Return whether pipeline materialize should write candidate_gifs.

    ``GIFAGENT_LIBRARY_INGEST=0`` disables ingest (pytest default). Unset env
    follows ``cfg['library_ingest_enabled']``, defaulting to True in production.
    """
    env = os.environ.get("GIFAGENT_LIBRARY_INGEST")
    if env is not None:
        return env.strip().lower() in {"1", "true", "yes", "on"}
    if cfg is None:
        return True
    return bool(cfg.get("library_ingest_enabled", True))


def library_text_from_clip(clip: dict[str, Any]) -> dict[str, Any]:
    """Extract VLM caption/tags used for candidate_gifs + embedding text."""
    best_frame = clip.get("best_frame")
    frame = best_frame if isinstance(best_frame, dict) else {}
    caption = str(
        frame.get("caption") or clip.get("caption") or frame.get("summary") or ""
    ).strip()
    emotion = str(
        frame.get("emotional_core")
        or clip.get("emotional_core")
        or frame.get("emotion")
        or clip.get("emotion")
        or ""
    ).strip()
    scene_type = str(frame.get("scene_type") or clip.get("scene_type") or "").strip()
    tags = clip.get("tags") or frame.get("tags") or []
    if isinstance(tags, str):
        tags = [tags]
    if not isinstance(tags, list):
        tags = []
    clean_tags = [str(tag).strip() for tag in tags if str(tag).strip()]
    worthiness = clip.get("gif_worthiness")
    final_score = clip.get("final_score")
    if final_score is None:
        final_score = worthiness
    return {
        "vlm_summary": {
            "caption": caption,
            "summary": caption,
            "emotion": emotion,
            "emotional_core": emotion,
            "scene_type": scene_type,
        },
        "tags": clean_tags,
        "gif_worthiness": worthiness,
        "final_score": final_score,
    }


def _source_video_sha256(video_path: str, clips: list[dict[str, Any]]) -> str:
    """Prefer quality-lineage file hash; never read video bytes here."""
    for clip in clips:
        parent = clip.get("parent_source")
        if not isinstance(parent, dict):
            continue
        sha = parent.get("source_file_sha256")
        if (
            isinstance(sha, str)
            and len(sha) == 64
            and sha.lower() not in _SOURCE_SHA_SENTINELS
        ):
            return sha.lower()
    normalized = os.path.normpath(os.path.abspath(video_path)).encode("utf-8")
    return hashlib.sha256(normalized).hexdigest()


def published_clip_to_run_candidate(
    clip: dict[str, Any],
    *,
    video_path: str,
    source_video_sha256: str,
) -> dict[str, Any]:
    """Map a published materialize clip dict onto CandidateService payload."""
    library = clip.get("library_text")
    if not isinstance(library, dict) or not library.get("vlm_summary"):
        library = library_text_from_clip(clip)
    vlm_summary = library.get("vlm_summary") or {}
    tags = library.get("tags") or []
    clip_id = str(clip.get("clip_id") or "clip")
    start_sec = float(clip.get("start_ts") or clip.get("start_sec") or 0.0)
    end_sec = float(clip.get("end_ts") or clip.get("end_sec") or 0.0)
    artifact = clip.get("formal_path") or clip.get("path") or clip.get("gif_path")
    worthiness = library.get("gif_worthiness")
    if worthiness is None:
        worthiness = clip.get("gif_worthiness")
    final_score = library.get("final_score")
    if final_score is None:
        final_score = clip.get("final_score", worthiness)
    return {
        "run_candidate_id": clip_id,
        "source_video_sha256": source_video_sha256,
        "source_video_path": video_path,
        "start_sec": start_sec,
        "end_sec": end_sec,
        "artifact_path": artifact,
        "preview_path": artifact,
        "vlm_summary": vlm_summary,
        "tags": tags,
        "base_rag_similarity": worthiness,
        "final_score": final_score,
    }


def ingest_published_clips(
    conn: Any,
    *,
    video_path: str,
    clips: list[dict[str, Any]],
    run_id: str = "pipeline-materialize",
    embed_fn: Any = None,
) -> dict[str, int]:
    """Insert published GIFs into candidate_gifs + FTS + vectors.

    Per-clip failures are logged and skipped. Does not call build_profile.
    A capture record that cannot receive its publication link is marked
    NEEDS_REPAIR instead of reporting full closure success.
    """
    sha = _source_video_sha256(video_path, clips)
    service = CandidateService(conn, embed_fn=embed_fn)
    ingested = 0
    skipped = 0
    needs_repair = 0
    for clip in clips:
        try:
            payload = published_clip_to_run_candidate(
                clip,
                video_path=video_path,
                source_video_sha256=sha,
            )
            res = service.materialize_run_candidate(run_id, payload)
            cand_id = getattr(res, "candidate_id", None) or (
                res.get("candidate_id") if isinstance(res, dict) else None
            )
            record_id = clip.get("analysis_record_id") or clip.get("record_id")
            gif_sha = clip.get("sha256") or clip.get("gif_sha256") or clip.get("artifact_sha256")
            if not gif_sha:
                art_path = payload.get("artifact_path")
                if art_path and os.path.isfile(art_path):
                    hasher = hashlib.sha256()
                    with open(art_path, "rb") as f:
                        for chunk in iter(lambda: f.read(65536), b""):
                            hasher.update(chunk)
                    gif_sha = hasher.hexdigest().lower()
            if record_id:
                if not cand_id or not gif_sha:
                    _mark_capture_needs_repair(conn, record_id, "missing_candidate_or_gif_hash")
                    needs_repair += 1
                else:
                    try:
                        # The actual candidate ID, the GIF byte hash, AND the
                        # curve-source feature ledger entry are bound in one
                        # explicit transaction: once a GIF is linked, its
                        # source features are trainable without any manual
                        # backfill step.
                        conn.execute("BEGIN IMMEDIATE")
                        link_publication(conn, record_id, cand_id, gif_sha)
                        _record_source_features(conn, record_id, cand_id, gif_sha)
                        conn.commit()
                    except Exception as link_exc:
                        conn.rollback()
                        _mark_capture_needs_repair(conn, record_id, str(link_exc)[:200])
                        needs_repair += 1
                        logger.warning(
                            "Publication link failed for record %s: %s",
                            record_id,
                            link_exc,
                        )
            ingested += 1
        except Exception as exc:
            skipped += 1
            logger.warning(
                "Library ingest skipped for clip %s: %s",
                clip.get("clip_id"),
                exc,
            )
    return {
        "ingested": ingested,
        "skipped": skipped,
        "needs_repair": needs_repair,
    }


def _mark_capture_needs_repair(conn: Any, record_id: str, reason: str) -> None:
    """Flag a capture record NEEDS_REPAIR and append a publication_failed
    event; the original evidence is never rewritten."""
    try:
        conn.execute(
            "UPDATE candidate_analysis_records SET capture_status = 'NEEDS_REPAIR' "
            "WHERE record_id = ?",
            (record_id,),
        )
        from app.services.candidate_capture import append_candidate_analysis_event

        append_candidate_analysis_event(
            conn,
            record_id,
            "publication_failed",
            {"reason": reason, "stage": "library_ingest"},
            evidence_root=Path("data/candidate_evidence"),
        )
        conn.commit()
    except Exception:
        logger.warning(
            "Could not mark capture record %s NEEDS_REPAIR", record_id, exc_info=True
        )


def ingest_succeeded_gifs_into_library(
    *,
    video_path: str,
    clips: list[dict[str, Any]],
    cfg: dict[str, Any] | None = None,
) -> None:
    """Best-effort hook for ``_stage_materialize``. Never raises."""
    if not clips:
        return
    if not library_ingest_enabled(cfg):
        return
    try:
        from app.db import get_connection
        from app.services.preference_schema import apply_preference_schema

        conn = get_connection()
        try:
            apply_preference_schema(conn)
            result = ingest_published_clips(
                conn, video_path=video_path, clips=clips
            )
            print(
                "  Library ingest: "
                f"{result['ingested']} candidate(s), skipped={result['skipped']}"
                + (
                    f", publication_needs_repair={result['needs_repair']}"
                    if result.get("needs_repair")
                    else ""
                )
            )
        finally:
            conn.close()
    except Exception as exc:
        print(f"  Library ingest skipped ({exc})")


def _record_source_features(
    conn: Any,
    record_id: str,
    candidate_id: str,
    gif_sha256: str,
) -> None:
    """Write the capture's AVAILABLE curve-source features into the
    ``curve_features`` training ledger (same transaction as the publication
    link). UNAVAILABLE features are never entered as fake rows.

    The evidence hash binds the ledger row to the immutable capture payload:
    the features written here are exactly the bytes the capture froze.
    """
    import json as _json
    from pathlib import Path as _Path

    row = conn.execute(
        "SELECT payload_path, payload_sha256, source_video_sha256 "
        "FROM candidate_analysis_records WHERE record_id = ?",
        (record_id,),
    ).fetchone()
    if row is None:
        return
    payload_path, payload_sha256, source_sha = row[0], row[1], row[2]
    payload_bytes = _Path(payload_path).read_bytes()
    # The capture evidence is immutable: the bytes on disk must hash to the
    # value recorded at capture time, otherwise the payload was tampered with
    # and NOTHING from it may enter the training ledger.
    actual_sha = hashlib.sha256(payload_bytes).hexdigest()
    if actual_sha != payload_sha256:
        raise ValueError(
            f"capture payload hash mismatch for record {record_id}: "
            f"expected {payload_sha256}, found {actual_sha}"
        )
    payload = _json.loads(payload_bytes.decode("utf-8"))
    feature = payload.get("source_feature") or {}
    if feature.get("status") != "AVAILABLE":
        return
    features = feature.get("features") or {}
    timestamps = feature.get("actual_timestamps") or []
    from app.services.curve_source_features import validate_window_feature_values

    strict_reasons = validate_window_feature_values(features, timestamps)
    if strict_reasons:
        raise ValueError(
            f"capture feature evidence invalid for record {record_id}: "
            f"{strict_reasons}"
        )
    if not feature.get("extractor_sha256") or not feature.get("feature_version"):
        raise ValueError(
            f"capture feature identity incomplete for record {record_id}"
        )
    # Second verification of the extraction input binding: the recorded
    # input hash must reproduce from the REAL source sha, the frozen window,
    # and the extractor identity -- otherwise the features were not computed
    # from the video bytes this record claims.
    from app.services.curve_source_features import (
        CurveWindow as _CurveWindow,
        window_input_hash as _window_input_hash,
    )

    recomputed_input = _window_input_hash(
        str(payload.get("source_video_sha256") or ""),
        _CurveWindow(
            preexport_key=str(payload.get("preexport_key") or ""),
            start_us=int(payload.get("start_us") or 0),
            end_us=int(payload.get("end_us") or 0),
        ),
        feature_version=str(feature.get("feature_version")),
        extractor_sha256=str(feature["extractor_sha256"]),
    )
    if str(feature.get("input_hash") or "") != recomputed_input:
        raise ValueError(
            f"capture feature input hash mismatch for record {record_id}"
        )
    # The formal ledger vector IS the frozen scoring vector: byte-for-byte,
    # normalized by the production rule, with the preference text hash as the
    # ledger identity. Scoring, capture, and the preference profile therefore
    # share one vector domain -- no second embedding computation.
    identity = payload.get("preference_identity") or {}
    if identity.get("scoring_input_committed") and identity.get(
        "vector_float32_b64"
    ):
        import base64 as b64mod

        import numpy as np

        from app.services.vector_math import vector_to_blob

        frozen = np.frombuffer(
            b64mod.b64decode(identity["vector_float32_b64"], validate=True),
            dtype=np.float32,
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO candidate_vectors (
                candidate_id, vector_type, embedding_model, embedding_dim,
                vector_blob, normalized, source_text_hash, text_schema_version
            ) VALUES (?, 'clip', ?, 768, ?, 1, ?, 1)
            """,
            (
                candidate_id,
                str(identity.get("embedding_model") or "nomic-embed-text:latest"),
                vector_to_blob(frozen, embedding_dim=768),
                str(identity.get("embedding_text_sha256") or ""),
            ),
        )
    conn.execute(
        """
        INSERT OR REPLACE INTO curve_features (
            candidate_id, feature_version, availability, features_json,
            missing_reason, extractor_sha256, artifact_sha256, wall_clock_ms,
            source_video_sha256, frame_count
        ) VALUES (?, ?, 'AVAILABLE', ?, NULL, ?, ?, ?, ?, ?)
        """,
        (
            candidate_id,
            str(feature.get("feature_version") or "curve-source-v1"),
            _json.dumps(features, sort_keys=True),
            str(feature["extractor_sha256"]),
            str(gif_sha256),
            float(feature.get("wall_clock_ms") or 0.0),
            str(source_sha or ""),
            len(timestamps),
        ),
    )