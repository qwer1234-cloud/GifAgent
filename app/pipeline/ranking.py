"""Export ranking: preference blend, adult MoE, clip identities."""
from __future__ import annotations

import hashlib
import math
import os

from app.db import get_connection
from app.services.embedding import compute_text_embedding
from app.services.export_ranking import make_adult_moe_scorer, rank_clips_for_export


def _quality_ranking_weights(adaptive: dict) -> dict[str, float]:
    """Freeze the adult/cinematic mix used after quality MoE evaluation."""
    adult_weight = float(adaptive.get("quality_ranking_adult_weight", 0.80))
    cinematic_weight = float(adaptive.get("quality_ranking_cinematic_weight", 0.20))
    if not math.isfinite(adult_weight) or not math.isfinite(cinematic_weight):
        raise ValueError("quality ranking weights must be finite")
    if not 0.0 <= adult_weight <= 1.0 or not 0.0 <= cinematic_weight <= 1.0:
        raise ValueError("quality ranking weights must be in [0, 1]")
    if abs(adult_weight + cinematic_weight - 1.0) > 1e-6:
        raise ValueError("quality ranking weights must sum to 1.0")
    return {
        "quality_ranking_adult_weight": adult_weight,
        "quality_ranking_cinematic_weight": cinematic_weight,
    }


def _clip_base_export_payload(clip: dict, cfg: dict) -> dict | None:
    """Non-preference export scores used as the blend base."""
    if cfg.get("score_prompt_mode") == "adult":
        return make_adult_moe_scorer(
            cfg["quality_ranking_adult_weight"],
            cfg["quality_ranking_cinematic_weight"],
        )(clip)
    return None


def _curve_video_context(reranker_conn, cfg: dict) -> dict | None:
    """Resolve the per-video curve authority once, or None when curve is off.

    The snapshot and the (optional) model are frozen for the whole video so
    mid-run profile or deployment changes cannot split a batch. Shadow mode
    may explicitly name a DRAFT model for diagnostics via
    ``curve_shadow_model_version``; it never becomes production-qualified.
    """
    if not cfg.get("curve_preference_enabled"):
        return None
    mode = str(cfg.get("curve_preference_mode", "off") or "off").strip().lower()
    if mode not in ("shadow", "active"):
        return None

    from dataclasses import replace

    from app.services.curve_online import load_curve_runtime_snapshot
    from app.services.curve_preference import load_curve_model

    snapshot = load_curve_runtime_snapshot(reranker_conn, cfg)
    model_version = snapshot.model_version
    if mode == "shadow" and model_version is None:
        # Shadow diagnostics may explicitly name a DRAFT model without any
        # deployment; the loaded version MUST reach the scoring snapshot or
        # the shared scorer would treat every curve input as partial.
        model_version = cfg.get("curve_shadow_model_version") or None
    model = None
    if model_version:
        try:
            model = load_curve_model(
                reranker_conn,
                str(model_version),
                allowed_statuses=frozenset({"DRAFT", "PUBLISHED"}),
            )
        except ValueError:
            model = None
    if model is not None and snapshot.model_version != model.model_version:
        snapshot = replace(snapshot, model_version=model.model_version)
    return {"snapshot": snapshot, "model": model}


def _curve_probability_from_clip(curve_ctx: dict, clip: dict) -> float | None:
    """Curve probability from captured source features under the frozen model.

    Domain mismatch (a GIF-domain model vs curve-source features) fails
    closed: no probability, no curve influence.
    """
    from app.services.curve_preference import EvidenceUnavailable, curve_probability

    model = curve_ctx.get("model")
    features = clip.get("source_features")
    if model is None or not features:
        return None
    feature_domain = str(
        clip.get("source_feature_version", "curve-source-v1") or "curve-source-v1"
    )
    if model.feature_version != feature_domain:
        return None
    try:
        return curve_probability(model, features)
    except EvidenceUnavailable:
        return None


def _rank_clips_with_preference(clips: list[dict], cfg: dict) -> list[dict] | None:
    """Blend published preference scores into export ranking.

    Returns None when the library connection cannot be opened so callers
    can fall back to adult MoE / VLM order. The per-candidate score flows
    through the shared ``score_preference_export_candidate`` so online
    ranking, shadow diagnostics, and qualification replay share one formula.
    """
    from app.services.curve_online import score_curve_candidate
    from app.services.reranker import (
        PreferenceReranker,
        clip_scenario_keys,
        preference_embedding_text,
        score_preference_export_candidate,
    )

    try:
        reranker_conn = get_connection()
    except Exception as exc:
        print(f"  Preference Memory: skipped ({exc})")
        return None

    reranker = PreferenceReranker(reranker_conn)
    base_weight = float(cfg.get("base_score_weight", 0.50))
    preference_weight = float(cfg.get("preference_score_weight", 0.50))
    skipped_empty_caption = 0
    curve_ctx = _curve_video_context(reranker_conn, cfg)

    def score_clip(clip):
        nonlocal skipped_empty_caption
        base_payload = _clip_base_export_payload(clip, cfg) or {}
        base_score = base_payload.get("final_score")
        if base_score is None:
            base_score = clip.get("gif_worthiness") or 0.0
        text = _clip_preference_embedding_text(clip)
        if not text:
            skipped_empty_caption += 1
        vector = None
        if text:
            try:
                vector = compute_text_embedding(text)
            except Exception:
                vector = None
        if not vector:
            return base_payload or None
        scenario = clip_scenario_keys(clip)
        result = score_preference_export_candidate(
            reranker=reranker,
            candidate_vector=vector,
            gif_worthiness=float(clip.get("gif_worthiness") or 0.0),
            scenario_keys=scenario,
            profile_version=None,
            base_export_score=float(base_score),
            base_score_weight=base_weight,
            preference_score_weight=preference_weight,
        )
        # Freeze the EXACT vector bytes this scoring run used onto the clip
        # only AFTER the shared scorer returned: if scoring had raised, the
        # common ranker swallows the error and falls back -- such a vector
        # was never a scoring input and must not be recorded as one.
        import base64 as _b64
        import hashlib as _hashlib

        import numpy as _np

        _arr = _np.asarray(vector, dtype=_np.float32)
        clip["preference_vector_b64"] = _b64.b64encode(_arr.tobytes()).decode("ascii")
        clip["preference_vector_sha256"] = _hashlib.sha256(_arr.tobytes()).hexdigest()
        clip["preference_text_sha256"] = _hashlib.sha256(text.encode("utf-8")).hexdigest()
        clip["scoring_input_committed"] = True
        blended = dict(base_payload)
        blended["final_score"] = result["final_score"]
        blended["profile_score"] = result.get("profile_score")
        blended["score_profile_version"] = result.get("score_profile_version")
        if curve_ctx is not None:
            probability = _curve_probability_from_clip(curve_ctx, clip)
            if probability is not None:
                scores = score_curve_candidate(
                    reranker_conn,
                    curve_ctx["snapshot"],
                    {
                        "base_score": float(base_score),
                        "gif_worthiness": float(clip.get("gif_worthiness") or 0.0),
                        "candidate_vector": vector,
                        "scenario_keys": scenario,
                        "curve_probability": probability,
                    },
                )
                blended["curve_route"] = scores["route"]
                blended["curve_baseline_score"] = scores["baseline_score"]
                blended["curve_nominal_score"] = scores["nominal_score"]
                blended["curve_effective_score"] = scores["effective_score"]
                blended["curve_inactive_reasons"] = list(scores["inactive_reasons"])
                if scores["route"] == "ACTIVE":
                    blended["final_score"] = scores["production_score"]
        return blended

    try:
        ranked = rank_clips_for_export(clips, score_clip)
    finally:
        reranker_conn.close()
    print(
        "Preference Memory: ranked all candidates with "
        f"base={base_weight:.2f}, preference={preference_weight:.2f}"
        + (
            f", skipped {skipped_empty_caption} empty-caption clips"
            if skipped_empty_caption
            else ""
        )
        + (
            f", curve mode={curve_ctx['snapshot'].mode}"
            if curve_ctx is not None
            else ""
        )
    )
    return ranked


def _rank_pipeline_clips(clips: list[dict], cfg: dict) -> list[dict]:
    """Apply preference ranking when enabled, else adult MoE / VLM order."""
    if cfg.get("preference_memory_enabled"):
        ranked = _rank_clips_with_preference(clips, cfg)
        if ranked is not None:
            return ranked
    if cfg.get("score_prompt_mode") == "adult":
        ranked = rank_clips_for_export(
            clips,
            make_adult_moe_scorer(
                cfg["quality_ranking_adult_weight"],
                cfg["quality_ranking_cinematic_weight"],
            ),
        )
        print(
            "Adult MoE ranking: "
            f"{cfg['quality_ranking_adult_weight']:.2f}*adult"
            f"({0.40:.2f}*gif_worthiness+{0.60:.2f}*sex_act) + "
            f"{cfg['quality_ranking_cinematic_weight']:.2f}*cinematic"
        )
        return ranked
    return rank_clips_for_export(clips, lambda _clip: None)


def _assign_candidate_identities(
    clips: list[dict], video_path: str, cfg: dict | None = None
) -> None:
    """Give every clip a stable identity before the shared quality boundary."""
    from app.services.candidate_capture import assign_candidate_capture_identities

    capture_enabled = bool(cfg.get("candidate_capture_enabled", False)) if cfg else False
    capture_version = str(cfg.get("candidate_capture_version", "capture-v1")) if cfg else "capture-v1"
    assign_candidate_capture_identities(
        clips=clips,
        video_path=video_path,
        capture_enabled=capture_enabled,
        capture_version=capture_version,
    )


def _planned_output_count(
    n: int, output_ratio: float, max_output: int
) -> int:
    """Return the export cap after quality routing, matching direct mode."""
    if n <= 0:
        return 0
    output_count = max(1, int(n * float(output_ratio)))
    if int(max_output) > 0:
        output_count = min(output_count, int(max_output))
    return output_count


def _clip_caption(clip: dict) -> str:
    """Return the VLM caption used to gate preference embedding."""
    best = clip.get("best_frame") if isinstance(clip.get("best_frame"), dict) else {}
    caption = best.get("caption") or clip.get("caption") or ""
    return str(caption).strip()


def _clip_embedding_text(clip: dict) -> str:
    best = clip.get("best_frame")
    frame = best if isinstance(best, dict) else {}
    return " ".join(
        filter(
            None,
            [
                frame.get("caption") or clip.get("caption") or "",
                frame.get("emotional_core") or clip.get("emotional_core") or "",
                frame.get("scene_type") or "",
            ],
        )
    )


def _clip_preference_embedding_text(clip: dict) -> str:
    """Caption-gated text for preference scoring.

    Empty caption must not embed leftover emotional_core/scene_type; those
    clips keep the adult MoE / VLM base score instead of a generic profile hit.
    Dedup still uses ``_clip_embedding_text``.
    """
    from app.services.reranker import preference_embedding_text

    best = clip.get("best_frame")
    frame = best if isinstance(best, dict) else {}
    caption = _clip_caption(clip)
    emotion = frame.get("emotional_core") or clip.get("emotional_core") or ""
    scene = frame.get("scene_type") or clip.get("scene_type") or ""
    return preference_embedding_text(caption=caption, emotion=emotion, scene_type=scene)


def _compute_clip_embeddings(clips: list[dict]) -> list:
    embeddings = []
    for clip in clips:
        text = _clip_embedding_text(clip)
        if not text:
            embeddings.append(None)
            continue
        try:
            embeddings.append(compute_text_embedding(text))
        except Exception:
            embeddings.append(None)
    return embeddings
