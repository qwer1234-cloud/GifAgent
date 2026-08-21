from app.services.export_ranking import rank_clips_for_export
import pytest


def test_rank_clips_for_export_applies_preference_score_before_top_n_selection():
    clips = [
        {"id": "high-base", "gif_worthiness": 0.90},
        {"id": "preference-favored", "gif_worthiness": 0.70},
    ]

    ranked = rank_clips_for_export(
        clips,
        lambda clip: {
            "final_score": 0.50 if clip["id"] == "high-base" else 0.95,
            "profile_score": 0.99 if clip["id"] == "preference-favored" else 0.10,
        },
    )

    assert [clip["id"] for clip in ranked[:1]] == ["preference-favored"]
    assert ranked[0]["final_score"] == 0.95


def test_rank_clips_for_export_falls_back_to_vlm_score_when_preference_is_unavailable():
    clips = [{"id": "no-caption", "gif_worthiness": 0.72}]

    ranked = rank_clips_for_export(clips, lambda _clip: None)

    assert ranked[0]["final_score"] == 0.72


def test_adult_export_score_prefers_sex_act_over_cinematic_worthiness():
    from app.services.export_ranking import adult_export_score, rank_clips_for_export

    clips = [
        {
            "id": "kitchen",
            "gif_worthiness": 0.70,
            "best_frame": {"sex_act": 0.0},
        },
        {
            "id": "sex",
            "gif_worthiness": 0.62,
            "best_frame": {"sex_act": 0.90},
        },
    ]
    ranked = rank_clips_for_export(clips, adult_export_score)
    assert [clip["id"] for clip in ranked] == ["sex", "kitchen"]
    assert ranked[0]["final_score"] == pytest.approx(0.40 * 0.62 + 0.60 * 0.90)


def test_adult_moe_blend_keeps_sex_ahead_of_prettier_non_sex():
    from app.services.export_ranking import make_adult_moe_scorer, rank_clips_for_export

    clips = [
        {
            "id": "pretty-kitchen",
            "gif_worthiness": 0.80,
            "best_frame": {"sex_act": 0.0},
            "quality_assessment": {
                "evidence": [{
                    "signal_family": "cinematic_classifier",
                    "status": "AVAILABLE",
                    "scores": {"cinematic_score": 0.95, "color_balance": 0.95},
                }]
            },
        },
        {
            "id": "dark-sex",
            "gif_worthiness": 0.62,
            "best_frame": {"sex_act": 0.90},
            "quality_assessment": {
                "evidence": [{
                    "signal_family": "cinematic_classifier",
                    "status": "AVAILABLE",
                    "scores": {"cinematic_score": 0.40, "color_balance": 0.40},
                }]
            },
        },
    ]
    ranked = rank_clips_for_export(clips, make_adult_moe_scorer(0.80, 0.20))
    assert [clip["id"] for clip in ranked] == ["dark-sex", "pretty-kitchen"]
    adult_sex = 0.40 * 0.62 + 0.60 * 0.90
    adult_kitchen = 0.40 * 0.80 + 0.60 * 0.0
    assert ranked[0]["final_score"] == pytest.approx(0.80 * adult_sex + 0.20 * 0.40)
    assert ranked[1]["final_score"] == pytest.approx(0.80 * adult_kitchen + 0.20 * 0.95)


def test_adult_moe_uses_neutral_cinematic_when_assessment_missing():
    from app.services.export_ranking import adult_moe_export_score

    result = adult_moe_export_score({
        "gif_worthiness": 1.0,
        "best_frame": {"sex_act": 1.0},
    })
    assert result["cinematic_score"] == 0.5
    assert result["final_score"] == pytest.approx(0.80 * 1.0 + 0.20 * 0.5)
