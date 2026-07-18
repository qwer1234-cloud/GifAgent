from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.task_engine import TaskRepository, connect_task_db
from app.task_engine.legacy_import import (
    ImportReport,
    import_legacy_state,
    plan_legacy_import,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "legacy"
QUEUE = FIXTURE_DIR / "batch_queue.json"
STATE = FIXTURE_DIR / "batch_queue_state.json"
CHECKPOINT = FIXTURE_DIR / "batch_checkpoint.json"


def make_repo(tmp_path):
    conn = connect_task_db(tmp_path / "task.db")
    return TaskRepository(conn), conn


def run_import(repo, tmp_path):
    return import_legacy_state(
        repo,
        queue_path=QUEUE,
        state_path=STATE,
        checkpoint_path=CHECKPOINT,
        backup_dir=tmp_path / "backups",
    )


def table_count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def stage_rows(conn):
    return conn.execute(
        """SELECT v.path, s.stage_name, s.status, s.input_key, s.output_key
           FROM task_stages s JOIN task_videos v ON v.video_id = s.video_id"""
    ).fetchall()


def test_import_returns_expected_counts(tmp_path):
    repo, _ = make_repo(tmp_path)
    report = run_import(repo, tmp_path)
    assert report.jobs_created == 2
    assert report.videos_reused == 2
    assert report.videos_pending == 2
    assert len(report.backups) == 3
    assert report.migration_id


def test_backups_are_timestamped_byte_for_byte_copies(tmp_path):
    repo, _ = make_repo(tmp_path)
    report = run_import(repo, tmp_path)
    backups = sorted(Path(p) for p in report.backups)
    assert len(backups) == 3
    sources = {p.name: p for p in (QUEUE, STATE, CHECKPOINT)}
    backed_up_names = set()
    for backup in backups:
        assert backup.parent == tmp_path / "backups"
        for name, src in sources.items():
            if backup.name.startswith(f"{name}."):
                assert backup.name != name
                assert backup.read_bytes() == src.read_bytes()
                backed_up_names.add(name)
    assert backed_up_names == set(sources)


def test_terminal_successes_become_succeeded_materialize_stages(tmp_path):
    repo, conn = make_repo(tmp_path)
    run_import(repo, tmp_path)
    rows = {
        row["path"]: row
        for row in stage_rows(conn)
        if row["status"] == "succeeded"
    }
    assert set(rows) == {
        "C:/videos/alpha/alpha_ok.mp4",
        "C:/videos/alpha/alpha_dup.mp4",
    }
    for row in rows.values():
        assert row["stage_name"] == "materialize"
        assert row["output_key"] == "legacy-import"
        assert row["input_key"].startswith("legacy:")
    assert rows["C:/videos/alpha/alpha_ok.mp4"]["input_key"] == "legacy:fp-alpha-ok"


def test_failures_become_pending_stages(tmp_path):
    repo, conn = make_repo(tmp_path)
    run_import(repo, tmp_path)
    rows = {
        row["path"]: row
        for row in stage_rows(conn)
        if row["status"] == "pending"
    }
    assert set(rows) == {
        "D:/videos/beta/beta_fail.mp4",
        "C:/videos/alpha/loose_timeout.mp4",
    }
    for row in rows.values():
        assert row["stage_name"] == "materialize"
        assert row["output_key"] is None


def test_canonical_directories_are_deduplicated(tmp_path):
    repo, conn = make_repo(tmp_path)
    run_import(repo, tmp_path)
    jobs = conn.execute(
        "SELECT directory, config_json, job_limit, extensions FROM task_jobs"
    ).fetchall()
    assert len(jobs) == 2
    by_dir = {row["directory"]: row for row in jobs}
    assert set(by_dir) == {"C:/videos/alpha", "D:/videos/beta"}
    alpha = by_dir["C:/videos/alpha"]
    config = json.loads(alpha["config_json"])
    assert config["source"] == "legacy-import"
    assert config["legacy_job_id"] == "job-alpha"
    assert config["limit"] == 0
    assert config["extensions"] == ".mp4,.mkv"
    alpha_videos = conn.execute(
        """SELECT COUNT(*) FROM task_videos v
           JOIN task_jobs j ON j.job_id = v.job_id
           WHERE j.directory = 'C:/videos/alpha'"""
    ).fetchone()[0]
    assert alpha_videos == 3


def test_second_call_returns_same_report_and_creates_no_rows(tmp_path):
    repo, conn = make_repo(tmp_path)
    first = run_import(repo, tmp_path)
    counts_before = {
        t: table_count(conn, t)
        for t in ("task_jobs", "task_videos", "task_stages", "task_events")
    }
    backups_before = sorted(os.listdir(tmp_path / "backups"))
    second = run_import(repo, tmp_path)
    assert second == first
    for table, count in counts_before.items():
        assert table_count(conn, table) == count
    assert sorted(os.listdir(tmp_path / "backups")) == backups_before


def test_legacy_source_files_are_untouched(tmp_path):
    repo, _ = make_repo(tmp_path)
    before = {
        p: (p.read_bytes(), os.stat(p).st_mtime_ns)
        for p in (QUEUE, STATE, CHECKPOINT)
    }
    run_import(repo, tmp_path)
    for path, (data, mtime) in before.items():
        assert path.read_bytes() == data
        assert os.stat(path).st_mtime_ns == mtime


def test_cli_dry_run_prints_counts_and_writes_nothing(tmp_path):
    db_path = tmp_path / "cli.db"
    cmd = [
        sys.executable,
        "scripts/import_legacy_task_state.py",
        "--queue", str(QUEUE),
        "--state", str(STATE),
        "--checkpoint", str(CHECKPOINT),
        "--db", str(db_path),
        "--dry-run",
    ]
    result = subprocess.run(
        cmd, cwd=REPO_ROOT, capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr
    assert "jobs_planned=2" in result.stdout
    assert "jobs_created=" not in result.stdout
    assert "videos_reused=2" in result.stdout
    assert "videos_pending=2" in result.stdout
    assert not db_path.exists()
    assert not (tmp_path / "backups").exists()


def test_cli_import_prints_report(tmp_path):
    db_path = tmp_path / "cli.db"
    cmd = [
        sys.executable,
        "scripts/import_legacy_task_state.py",
        "--queue", str(QUEUE),
        "--state", str(STATE),
        "--checkpoint", str(CHECKPOINT),
        "--db", str(db_path),
    ]
    result = subprocess.run(
        cmd, cwd=REPO_ROOT, capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr
    assert "jobs_created=2" in result.stdout
    assert "videos_reused=2" in result.stdout
    assert "videos_pending=2" in result.stdout
    assert db_path.exists()


# --- planner-level unit tests (planner is pure) ---------------------------


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def make_sources(tmp_path, queue=None, state=None, checkpoint=None):
    queue_path = tmp_path / "queue.json"
    state_path = tmp_path / "state.json"
    checkpoint_path = tmp_path / "checkpoint.json"
    if queue is not None:
        write_json(queue_path, queue)
    if state is not None:
        write_json(state_path, state)
    if checkpoint is not None:
        write_json(checkpoint_path, checkpoint)
    return queue_path, state_path, checkpoint_path


def test_planner_empty_queue_uses_checkpoint_last_run_dir(tmp_path):
    queue_path, state_path, checkpoint_path = make_sources(
        tmp_path,
        checkpoint={
            "completed": {"stem_a": {"status": "ok"}},
            "last_run": {"status": "complete", "dir": "E:/data/originals"},
        },
    )
    plan = plan_legacy_import(queue_path, state_path, checkpoint_path)
    assert len(plan.jobs) == 1
    assert plan.jobs[0].directory == "E:/data/originals"
    assert [v.stem for v in plan.jobs[0].videos] == ["stem_a"]
    assert plan.videos_reused == 1


def test_planner_explicit_directory_fallback_places_all_stems(tmp_path):
    queue_path, state_path, checkpoint_path = make_sources(
        tmp_path,
        checkpoint={
            "completed": {
                "stem_a": {"status": "ok"},
                "stem_b": {"status": "dedup_skipped"},
            },
            "retryable": {"stem_c": {"status": "failed"}},
        },
    )
    plan = plan_legacy_import(
        queue_path, state_path, checkpoint_path, directory="E:/data/originals"
    )
    assert len(plan.jobs) == 1
    assert plan.jobs[0].directory == "E:/data/originals"
    assert {v.stem for v in plan.jobs[0].videos} == {"stem_a", "stem_b", "stem_c"}
    assert plan.videos_reused == 2
    assert plan.videos_pending == 1


def test_planner_unplaceable_stems_error_mentions_directory_option(tmp_path):
    queue_path, state_path, checkpoint_path = make_sources(
        tmp_path,
        checkpoint={"completed": {"stem_a": {"status": "ok"}}},
    )
    with pytest.raises(ValueError, match="--directory"):
        plan_legacy_import(queue_path, state_path, checkpoint_path)


def test_planner_stem_claimed_by_two_jobs_first_job_wins(tmp_path):
    queue_path, state_path, checkpoint_path = make_sources(
        tmp_path,
        queue={
            "jobs": [
                {"job_id": "j1", "directory": "C:/a", "videos": ["shared"]},
                {"job_id": "j2", "directory": "D:/b", "videos": ["shared"]},
            ]
        },
        checkpoint={"completed": {"shared": {"status": "ok"}}},
    )
    plan = plan_legacy_import(queue_path, state_path, checkpoint_path)
    assert [v.stem for v in plan.jobs[0].videos] == ["shared"]
    assert [v.stem for v in plan.jobs[1].videos] == []


def test_planner_merged_duplicate_directory_job_contributes_video_claims(tmp_path):
    queue_path, state_path, checkpoint_path = make_sources(
        tmp_path,
        queue={
            "jobs": [
                {"job_id": "j1", "directory": "C:/videos/alpha"},
                {"job_id": "j2", "directory": "c:\\videos\\alpha\\",
                 "videos": ["claimed_by_dupe"]},
            ]
        },
        checkpoint={"completed": {"claimed_by_dupe": {"status": "ok"}}},
    )
    plan = plan_legacy_import(queue_path, state_path, checkpoint_path)
    assert len(plan.jobs) == 1
    assert plan.jobs[0].legacy_job_id == "j1"
    assert [v.stem for v in plan.jobs[0].videos] == ["claimed_by_dupe"]


def test_planner_missing_checkpoint_raises_file_not_found(tmp_path):
    queue_path, state_path, checkpoint_path = make_sources(tmp_path)
    with pytest.raises(FileNotFoundError):
        plan_legacy_import(queue_path, state_path, checkpoint_path)


def test_planner_malformed_json_raises_clear_error(tmp_path):
    queue_path, state_path, checkpoint_path = make_sources(
        tmp_path, checkpoint={"completed": {"s": {"status": "ok"}}}
    )
    queue_path.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        plan_legacy_import(queue_path, state_path, checkpoint_path)


def test_planner_unicode_stem_round_trips(tmp_path):
    queue_path, state_path, checkpoint_path = make_sources(
        tmp_path,
        checkpoint={"completed": {"视频_💦": {"status": "ok"}}},
    )
    plan = plan_legacy_import(
        queue_path, state_path, checkpoint_path, directory="E:/data/originals"
    )
    assert [v.stem for v in plan.jobs[0].videos] == ["视频_💦"]


def test_state_completed_shape_falls_back(tmp_path):
    # Older exports used {"completed": {job_id: {...}}} instead of {"jobs": ...}.
    queue_path, state_path, checkpoint_path = make_sources(
        tmp_path,
        queue={"jobs": [{"job_id": "j1", "directory": "C:/a"}]},
        state={"completed": {"j1": {"status": "completed"}}},
        checkpoint={"completed": {"s": {"status": "ok"}}},
    )
    plan = plan_legacy_import(queue_path, state_path, checkpoint_path)
    assert plan.jobs[0].legacy_job_status == "completed"


def test_import_with_explicit_directory_creates_single_job(tmp_path):
    queue_path, state_path, checkpoint_path = make_sources(
        tmp_path,
        checkpoint={
            "completed": {"stem_a": {"status": "ok"}},
            "retryable": {"stem_b": {"status": "timeout"}},
        },
    )
    repo, conn = make_repo(tmp_path)
    report = import_legacy_state(
        repo,
        queue_path=queue_path,
        state_path=state_path,
        checkpoint_path=checkpoint_path,
        backup_dir=tmp_path / "backups",
        directory="E:/data/originals/",
    )
    assert report.jobs_created == 1
    assert report.videos_reused == 1
    assert report.videos_pending == 1
    paths = {
        row["path"]
        for row in conn.execute("SELECT path FROM task_videos").fetchall()
    }
    # Trailing separator in the directory must not produce double slashes.
    assert paths == {
        "E:/data/originals/stem_a.mp4",
        "E:/data/originals/stem_b.mp4",
    }
