def _clip(ts: float, score: float) -> dict:
    return {
        "start_ts": ts,
        "end_ts": ts,
        "gif_worthiness": score,
        "best_frame": {"timestamp": ts},
    }


def test_temporal_dedup_keeps_highest_scored_clip_per_time_window():
    from app.services.clip_dedup import temporal_dedup_clips

    clips = [
        _clip(10, 0.70),
        _clip(14, 0.90),
        _clip(30, 0.60),
        _clip(37, 0.80),
    ]

    deduped = temporal_dedup_clips(clips, min_gap_s=8)

    assert [c["best_frame"]["timestamp"] for c in deduped] == [14, 37]


def test_temporal_dedup_can_be_disabled():
    from app.services.clip_dedup import temporal_dedup_clips

    clips = [_clip(10, 0.70), _clip(14, 0.90)]

    assert temporal_dedup_clips(clips, min_gap_s=0) == clips


def test_embedding_dedup_without_gap_collapses_distant_caption_twins():
    from app.services.clip_dedup import embedding_dedup_clips

    clips = [_clip(10, 0.70), _clip(200, 0.60)]
    embeddings = [[1.0, 0.0], [1.0, 0.0]]

    kept, groups = embedding_dedup_clips(
        clips, embeddings, threshold=0.9, max_gap_s=0,
    )

    assert [c["best_frame"]["timestamp"] for c in kept] == [10]
    assert groups[0]["duplicates"] == [1]


def test_embedding_dedup_max_gap_keeps_distant_caption_twins():
    from app.services.clip_dedup import embedding_dedup_clips

    clips = [_clip(10, 0.70), _clip(200, 0.60)]
    embeddings = [[1.0, 0.0], [1.0, 0.0]]

    kept, groups = embedding_dedup_clips(
        clips, embeddings, threshold=0.9, max_gap_s=15,
    )

    assert [c["best_frame"]["timestamp"] for c in kept] == [10, 200]
    assert groups == []


def test_embedding_dedup_max_gap_still_collapses_nearby_twins():
    from app.services.clip_dedup import embedding_dedup_clips

    clips = [_clip(10, 0.70), _clip(12, 0.60)]
    embeddings = [[1.0, 0.0], [1.0, 0.0]]

    kept, _groups = embedding_dedup_clips(
        clips, embeddings, threshold=0.9, max_gap_s=15,
    )

    assert [c["best_frame"]["timestamp"] for c in kept] == [10]
