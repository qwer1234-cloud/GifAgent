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


def _rank_clips_with_preference(clips: list[dict], cfg: dict) -> list[dict] | None:
    """Blend published preference scores into export ranking.

    Returns None when the library connection cannot be opened so callers
    can fall back to adult MoE / VLM order.
    """
    from app.services.reranker import (
        PreferenceReranker,
        blend_export_scores,
        clip_scenario_keys,
    )

    try:
        reranker_conn = get_connection()
    except Exception as exc:
        print(f"  Preference Memory: skipped ({exc})")
        return None

    reranker = PreferenceReranker(reranker_conn)
    base_weight = float(cfg.get("base_score_weight", 0.50))
    preference_weight = float(cfg.get("preference_score_weight", 0.50))

    def score_clip(clip):
        base_payload = _clip_base_export_payload(clip, cfg) or {}
        base_score = base_payload.get("final_score")
        if base_score is None:
            base_score = clip.get("gif_worthiness") or 0.0
        text = _clip_embedding_text(clip)
        vector = None
        if text:
            try:
                vector = compute_text_embedding(text)
            except Exception:
                vector = None
        if not vector:
            return base_payload or None
        breakdown = reranker.score(
            candidate_vector=vector,
            base_rag_similarity=float(clip.get("gif_worthiness") or 0.0),
            scenario_keys=clip_scenario_keys(clip),
            profile_version=None,
            enabled=True,
        )
        profile_score = breakdown.get("profile_score")
        if profile_score is None:
            return base_payload or None
        blended = dict(base_payload)
        blended["final_score"] = blend_export_scores(
            float(base_score),
            float(profile_score),
            base_weight,
            preference_weight,
        )
        blended["profile_score"] = profile_score
        blended["score_profile_version"] = breakdown.get(
            "preference_profile_version"
        )
        return blended

    try:
        ranked = rank_clips_for_export(clips, score_clip)
    finally:
        reranker_conn.close()
    print(
        "Preference Memory: ranked all candidates with "
        f"base={base_weight:.2f}, preference={preference_weight:.2f}"
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


def _assign_candidate_identities(clips: list[dict], video_path: str) -> None:
    """Give every clip a stable identity before the shared quality boundary."""
    video_name = os.path.splitext(os.path.basename(video_path))[0]
    for index, clip in enumerate(clips):
        candidate_id = clip.get("candidate_id") or clip.get("clip_id")
        if not isinstance(candidate_id, str) or not candidate_id:
            identity = (
                f"{video_name}:{clip.get('start_ts')}:"
                f"{clip.get('end_ts')}:{index}"
            )
            candidate_id = hashlib.sha256(
                identity.encode("utf-8")
            ).hexdigest()[:16]
        clip["candidate_id"] = candidate_id
        clip.setdefault("clip_id", candidate_id)


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
