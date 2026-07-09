from app.ui.candidate_review import summarize_checkpoint_status


def test_status_prefers_last_run_over_historical_retryable_backlog():
    checkpoint = {
        "completed": {
            "old_ok": {"status": "ok"},
        },
        "retryable": {
            f"old_failed_{idx}": {"status": "failed", "exit_code": 1}
            for idx in range(82)
        },
        "last_run": {
            "planned": 1,
            "processed": 1,
            "succeeded": 1,
            "failed": 0,
            "dedup_skipped": 0,
        },
    }

    status = summarize_checkpoint_status(checkpoint)

    assert status["completed"] == 1
    assert status["failed"] == 0
    assert status["total"] == 1
