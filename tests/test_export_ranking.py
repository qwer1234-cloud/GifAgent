from app.services.export_ranking import rank_clips_for_export


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
