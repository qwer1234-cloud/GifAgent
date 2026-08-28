from types import SimpleNamespace

from scripts import test_video_adaptive as adaptive
from app.pipeline import ranking as pipeline_ranking


def _adult_cfg(**overrides):
    cfg = {
        "preference_memory_enabled": False,
        "score_prompt_mode": "adult",
        "quality_ranking_adult_weight": 0.80,
        "quality_ranking_cinematic_weight": 0.20,
        "base_score_weight": 0.50,
        "preference_score_weight": 0.50,
    }
    cfg.update(overrides)
    return cfg


def _clips():
    return [
        {
            "id": "base",
            "gif_worthiness": 0.90,
            "best_frame": {"caption": "kitchen", "sex_act": 0.1},
        },
        {
            "id": "favored",
            "gif_worthiness": 0.60,
            "best_frame": {"caption": "favored scene", "sex_act": 0.1},
        },
    ]


def test_rank_pipeline_clips_uses_adult_moe_when_preference_is_off():
    ranked = adaptive._rank_pipeline_clips(_clips(), _adult_cfg())
    assert [clip["id"] for clip in ranked] == ["base", "favored"]


def test_rank_pipeline_clips_blends_preference_when_enabled(monkeypatch):
    class FakeReranker:
        def __init__(self, _conn):
            pass

        def score(self, **kwargs):
            vector = kwargs["candidate_vector"]
            return {
                "profile_score": float(vector[0]),
                "preference_profile_version": "profile_test",
            }

    monkeypatch.setattr(pipeline_ranking, "get_connection", lambda: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr("app.services.reranker.PreferenceReranker", FakeReranker)
    monkeypatch.setattr(
        pipeline_ranking,
        "compute_text_embedding",
        lambda text: [1.0] + [0.0] * 767 if "favored" in text else [0.0] * 768,
    )

    ranked = adaptive._rank_pipeline_clips(
        _clips(), _adult_cfg(preference_memory_enabled=True)
    )
    assert ranked[0]["id"] == "favored"
    assert ranked[0]["profile_score"] == 1.0
    assert ranked[0]["score_profile_version"] == "profile_test"


def test_rank_pipeline_clips_falls_back_when_library_is_unavailable(monkeypatch):
    def fail_connect():
        raise RuntimeError("library unavailable")

    monkeypatch.setattr(pipeline_ranking, "get_connection", fail_connect)
    ranked = adaptive._rank_pipeline_clips(
        _clips(), _adult_cfg(preference_memory_enabled=True)
    )
    assert [clip["id"] for clip in ranked] == ["base", "favored"]
