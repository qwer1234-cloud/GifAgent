"""Tests for the adaptive pipeline's stage timing collector."""

import json
import threading
import time

import pytest

from app.services.stage_timing import StageTimings


def test_fresh_collector_reports_nothing():
    assert StageTimings().to_dict() == {}


def test_totals_accumulate_across_samples():
    timings = StageTimings()
    timings.record("extract", 10.0)
    timings.record("extract", 30.0)

    payload = timings.to_dict()

    assert payload["totals_ms"]["extract"] == pytest.approx(40.0)
    assert payload["counts"]["extract"] == 2


def test_p50_is_the_median_of_recorded_samples():
    timings = StageTimings()
    for ms in (5.0, 1.0, 100.0):
        timings.record("vlm", ms)

    assert timings.to_dict()["p50_ms"]["vlm"] == pytest.approx(5.0)


def test_span_measures_elapsed_wall_time():
    timings = StageTimings()

    with timings.span("gif_export"):
        time.sleep(0.02)

    payload = timings.to_dict()
    assert payload["counts"]["gif_export"] == 1
    assert payload["totals_ms"]["gif_export"] >= 10.0


def test_span_records_the_sample_then_reraises():
    timings = StageTimings()

    with pytest.raises(RuntimeError, match="ffmpeg died"):
        with timings.span("extract"):
            raise RuntimeError("ffmpeg died")

    assert timings.to_dict()["counts"]["extract"] == 1


def test_to_dict_is_json_serializable_and_key_ordered():
    timings = StageTimings()
    for name in ("vlm", "extract", "gif_export"):
        timings.record(name, 1.0)

    payload = timings.to_dict()

    assert list(payload) == ["counts", "p50_ms", "totals_ms"]
    assert list(payload["totals_ms"]) == ["extract", "gif_export", "vlm"]
    assert list(payload["p50_ms"]) == ["extract", "gif_export", "vlm"]
    assert list(payload["counts"]) == ["extract", "gif_export", "vlm"]
    assert json.loads(json.dumps(payload)) == payload


def test_a_metric_without_samples_is_omitted_rather_than_null():
    timings = StageTimings()
    timings.record("extract", 1.0)

    payload = timings.to_dict()

    assert "vlm" not in payload["totals_ms"]
    assert "vlm_output_tokens" not in payload


def test_non_finite_samples_never_reach_the_manifest():
    timings = StageTimings()
    timings.record("extract", float("nan"))
    timings.record("extract", float("inf"))
    timings.record("extract", 5.0)

    payload = timings.to_dict()

    assert payload["counts"]["extract"] == 1
    assert payload["totals_ms"]["extract"] == pytest.approx(5.0)
    assert "NaN" not in json.dumps(payload)
    assert "Infinity" not in json.dumps(payload)


def test_observe_vlm_tracks_latency_and_output_tokens():
    timings = StageTimings()
    timings.observe_vlm(eval_count=120, total_ms=800.0)
    timings.observe_vlm(eval_count=80, total_ms=400.0)

    payload = timings.to_dict()

    assert payload["vlm_output_tokens"] == 200
    assert payload["totals_ms"]["vlm"] == pytest.approx(1200.0)
    assert payload["counts"]["vlm"] == 2


def test_observe_vlm_tolerates_a_missing_eval_count():
    timings = StageTimings()
    timings.observe_vlm(eval_count=None, total_ms=500.0)

    payload = timings.to_dict()

    assert payload["vlm_output_tokens"] == 0
    assert payload["counts"]["vlm"] == 1


def test_concurrent_records_are_not_lost():
    timings = StageTimings()

    def worker() -> None:
        for _ in range(200):
            timings.record("vlm", 1.0)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    payload = timings.to_dict()
    assert payload["counts"]["vlm"] == 1600
    assert payload["totals_ms"]["vlm"] == pytest.approx(1600.0)
