from scripts.test_video_batch import (
    checkpoint_entry_can_be_reused,
    discover_videos,
    normalize_checkpoint_for_resume,
)


def test_checkpoint_reuses_successful_entries_only():
    assert checkpoint_entry_can_be_reused({"status": "ok"})
    assert checkpoint_entry_can_be_reused({"status": "dedup_skipped"})

    assert not checkpoint_entry_can_be_reused({"status": "failed"})
    assert not checkpoint_entry_can_be_reused({"status": "timeout"})
    assert not checkpoint_entry_can_be_reused({})
    assert not checkpoint_entry_can_be_reused(None)


def test_normalize_checkpoint_moves_retryable_entries_out_of_completed():
    checkpoint = {
        "completed": {
            "done": {"status": "ok"},
            "duplicate": {"status": "dedup_skipped"},
            "failed_video": {"status": "failed", "exit_code": 1},
            "slow_video": {"status": "timeout"},
        },
    }

    normalize_checkpoint_for_resume(checkpoint)

    assert set(checkpoint["completed"]) == {"done", "duplicate"}
    assert set(checkpoint["retryable"]) == {"failed_video", "slow_video"}


def test_discover_videos_handles_glob_metacharacters_in_directory(tmp_path):
    video_dir = tmp_path / "Tushy.17.08.19.Abella.Danger.XXX.2160p.MP4-KTR[rarbg]"
    video_dir.mkdir()
    video_path = video_dir / "tushy.17.08.19.abella.danger.4k.mp4"
    video_path.write_bytes(b"placeholder")

    assert discover_videos(str(video_dir), ".mp4,.mkv") == [str(video_path)]
