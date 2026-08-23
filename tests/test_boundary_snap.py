"""Task 14: sub-second export boundary snapping."""

from __future__ import annotations

from pathlib import Path

from app.services.boundary_snap import guard_result_from_cut_times, snap_window
from scripts.test_video_adaptive import extract_config
from app.services.temporal_evidence import TemporalEvidenceCache
from app.services.transition_guard import (
    BoundaryEvidence,
    TransitionGuardResult,
)
from tests.test_transition_guard import (
    BASE_CFG,
    write_hard_cut_video,
    write_moving_subject_video,
    write_static_video,
)


def _cut_at(ts: float) -> TransitionGuardResult:
    boundary = BoundaryEvidence(
        timestamp_s=ts,
        boundary_type="hard_cut",
        confidence=1.0,
        histogram_distance=1.0,
        edge_distance=1.0,
        luma_change=1.0,
        compensated_residual=1.0,
        inlier_ratio=0.0,
        translate_x=0.0,
        translate_y=0.0,
        scale=1.0,
    )
    return TransitionGuardResult(
        transition_action="split",
        segments=(),
        boundaries=(boundary,),
        hard_cut_count=1,
        soft_transition_count=0,
        motion_type="cut",
        transition_risk=1.0,
        guard_reason="hard_cut",
    )


def test_mid_motion_snaps_toward_nearby_minimum(tmp_path: Path):
    video = write_moving_subject_video(tmp_path / "moving.mp4")
    cache = TemporalEvidenceCache()
    result = snap_window(
        str(video), 1.0, 3.0,
        radius_s=0.6,
        guard_result=None,
        config=BASE_CFG,
        cache=cache,
    )
    assert result.snap_action in {"snapped", "kept"}
    if result.snap_action == "snapped":
        assert abs(result.start_s - 1.0) <= 0.6
        assert result.end_s - result.start_s >= BASE_CFG["transition_min_duration_s"]


def test_snap_never_crosses_confirmed_hard_cut(tmp_path: Path):
    video = write_hard_cut_video(tmp_path / "cut.mp4")
    cache = TemporalEvidenceCache()
    # Cut is at 3.0s (24 frames @ 8 fps). Start just after the cut so the
    # lowest motion on the far side is across the confirmed boundary.
    result = snap_window(
        str(video), 3.2, 5.2,
        radius_s=0.6,
        guard_result=_cut_at(3.0),
        config=BASE_CFG,
        cache=cache,
    )
    assert result.start_s >= 3.0
    assert result.snap_action in {"snapped", "kept"}


def test_snap_never_enters_boundary_margin(tmp_path: Path):
    video = write_hard_cut_video(tmp_path / "cut_margin.mp4")
    cache = TemporalEvidenceCache()
    cfg = {**BASE_CFG, "transition_boundary_margin_s": 0.4}
    result = snap_window(
        str(video), 3.6, 5.6,
        radius_s=0.6,
        guard_result=_cut_at(3.0),
        config=cfg,
        cache=cache,
    )
    assert abs(result.start_s - 3.0) >= 0.4 - 1e-6


def test_guarded_export_window_is_unchanged(tmp_path: Path):
    video = write_moving_subject_video(tmp_path / "guarded.mp4")
    result = snap_window(
        str(video), 1.1, 3.3,
        radius_s=0.6,
        guard_result=None,
        config=BASE_CFG,
        cache=TemporalEvidenceCache(),
        guarded_export_window=True,
    )
    assert result.snap_action == "kept"
    assert (result.start_s, result.end_s) == (1.1, 3.3)
    assert result.reason == "guarded_export_window"


def test_too_short_after_snap_keeps_original(tmp_path: Path):
    video = write_static_video(tmp_path / "short.mp4")
    cfg = {**BASE_CFG, "transition_min_duration_s": 10.0}
    result = snap_window(
        str(video), 0.5, 2.5,
        radius_s=0.6,
        guard_result=None,
        config=cfg,
        cache=TemporalEvidenceCache(),
    )
    assert result.snap_action == "kept"
    assert (result.start_s, result.end_s) == (0.5, 2.5)


def test_decode_failure_returns_unavailable(tmp_path: Path):
    missing = tmp_path / "missing.mp4"
    result = snap_window(
        str(missing), 1.0, 3.0,
        radius_s=0.6,
        guard_result=None,
        config=BASE_CFG,
        cache=TemporalEvidenceCache(),
    )
    assert result.snap_action == "unavailable"
    assert (result.start_s, result.end_s) == (1.0, 3.0)
    assert result.reason.startswith("decode_failed")


def test_extract_config_defaults_leave_boundary_snap_off():
    cfg = extract_config({"adaptive": {}})
    assert cfg["boundary_snap_enabled"] is False
    assert cfg["boundary_snap_radius_s"] == 0.6


def test_guard_result_from_cut_times_rebuilds_hard_cuts():
    guard = guard_result_from_cut_times([3.0, "4.5", 3.0, "x"])
    assert guard is not None
    assert guard.hard_cut_count == 2
    assert [item.timestamp_s for item in guard.boundaries] == [3.0, 4.5]
    assert guard_result_from_cut_times([]) is None
