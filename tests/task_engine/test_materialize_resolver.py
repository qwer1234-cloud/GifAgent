"""Phase 0 RED tests: stage-driven materialize resolver and envelope.

Fourth review (2026-07-18) §3.1 + §3.3 + §4 + §6:

* ``resolve_materialize_inputs`` must query from succeeded ``gif_clip``
  *stages* (not from ``task_artifacts``).  A succeeded clip missing any
  artifact must cause the resolver to FAIL, not silently return an empty
  set.
* The materialize input envelope must carry every terminal gif_clip
  status (succeeded / needs_attention / cancelled / failed), not only
  the succeeded clips that happened to have artifacts.

These tests are written against the target resolver/envelope contract
(``MaterializeInputs`` dataclass) and are expected to FAIL before the
P0-1 / P1-1 fixes land.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import pytest


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _setup_video(conn, tmp_path) -> str:
    """Insert a job + video and return the video_id."""
    conn.execute(
        "INSERT INTO task_jobs (job_id, directory, directory_key, config_json, "
        "status, created_at, updated_at) "
        "VALUES ('j1', ?, ?, '{}', 'running', ?, ?)",
        (str(tmp_path), str(tmp_path), _now(), _now()),
    )
    conn.execute(
        "INSERT INTO task_videos (video_id, job_id, path, fingerprint, status, "
        "created_at, updated_at) "
        "VALUES ('v1', 'j1', '/tmp/v.mp4', 'fp', 'running', ?, ?)",
        (_now(), _now()),
    )
    conn.commit()
    return "v1"


def _insert_gif_clip_stage(conn, stage_id: str, clip_id: str, status: str) -> None:
    conn.execute(
        "INSERT INTO task_stages (stage_id, video_id, stage_name, clip_id, "
        "input_key, status, attempt_count, created_at, updated_at) "
        "VALUES (?, 'v1', 'gif_clip', ?, ?, ?, 1, ?, ?)",
        (stage_id, clip_id, f"from:rank_dedup:clip:{clip_id}", status, _now(), _now()),
    )
    conn.commit()


def _write_gif(tmp_path, clip_id: str, data: bytes | None = None) -> tuple[str, str, int]:
    data = data or f"GIF89a-data-{clip_id}".encode()
    p = tmp_path / f"output_{clip_id}.gif"
    p.write_bytes(data)
    return str(p), hashlib.sha256(data).hexdigest(), len(data)


def _write_manifest(tmp_path, clip_id: str, gif_path: str, gif_sha: str) -> str:
    p = tmp_path / f"gif_clip_manifest_{clip_id}.json"
    p.write_text(json.dumps({
        "schema_version": 1, "stage": "gif_clip", "clip_id": clip_id,
        "gif_path": gif_path, "gif_name": f"output_{clip_id}.gif",
        "sha256": gif_sha, "start_ts": 10.0, "end_ts": 15.0,
    }))
    return str(p)


def _insert_artifact(conn, stage_id, kind, path, clip_id, sha, size) -> None:
    from app.task_engine.artifacts import make_artifact_id
    aid = make_artifact_id(stage_id=stage_id, artifact_kind=kind,
                           clip_id=clip_id, normalized_path=str(path))
    conn.execute(
        "INSERT INTO task_artifacts (artifact_id, job_id, video_id, stage_name, "
        "clip_id, path, sha256, size_bytes, provenance_json, created_at, "
        "stage_id, artifact_kind) "
        "VALUES (?, 'j1', 'v1', 'gif_clip', ?, ?, ?, ?, '{}', ?, ?, ?)",
        (aid, clip_id, str(path), sha, size, _now(), stage_id, kind),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# §3.1: succeeded clip missing artifacts must FAIL the resolver
# ---------------------------------------------------------------------------


class TestMaterializeRejectsIncompleteSucceededClip:
    """A succeeded gif_clip stage without a complete artifact pair must
    cause ``resolve_materialize_inputs`` to raise, not return an empty set."""

    def test_materialize_rejects_succeeded_clip_without_artifacts(self, tmp_path):
        from app.task_engine.schema import connect_task_db
        from app.task_engine.artifacts import resolve_materialize_inputs

        conn = connect_task_db(tmp_path / "task.db")
        _setup_video(conn, tmp_path)
        _insert_gif_clip_stage(conn, "gc-1", "clip-1", "succeeded")
        # No artifacts inserted at all.

        with pytest.raises(ValueError):
            resolve_materialize_inputs(conn, "v1")
        conn.close()

    def test_materialize_rejects_succeeded_clip_missing_gif_file(self, tmp_path):
        from app.task_engine.schema import connect_task_db
        from app.task_engine.artifacts import resolve_materialize_inputs

        conn = connect_task_db(tmp_path / "task.db")
        _setup_video(conn, tmp_path)
        _insert_gif_clip_stage(conn, "gc-1", "clip-1", "succeeded")

        # Only the manifest exists; the gif_file is missing.
        gif_path, gif_sha, gif_size = _write_gif(tmp_path, "clip-1")
        manifest_path = _write_manifest(tmp_path, "clip-1", gif_path, gif_sha)
        _insert_artifact(conn, "gc-1", "gif_clip_manifest", manifest_path,
                         "clip-1",
                         hashlib.sha256(open(manifest_path, "rb").read()).hexdigest(),
                         len(open(manifest_path, "rb").read()))

        with pytest.raises(ValueError):
            resolve_materialize_inputs(conn, "v1")
        conn.close()

    def test_materialize_rejects_succeeded_clip_missing_manifest(self, tmp_path):
        from app.task_engine.schema import connect_task_db
        from app.task_engine.artifacts import resolve_materialize_inputs

        conn = connect_task_db(tmp_path / "task.db")
        _setup_video(conn, tmp_path)
        _insert_gif_clip_stage(conn, "gc-1", "clip-1", "succeeded")

        # Only the gif_file exists; the manifest is missing.
        gif_path, gif_sha, gif_size = _write_gif(tmp_path, "clip-1")
        _insert_artifact(conn, "gc-1", "gif_file", gif_path, "clip-1",
                         gif_sha, gif_size)

        with pytest.raises(ValueError):
            resolve_materialize_inputs(conn, "v1")
        conn.close()

    def test_materialize_rejects_succeeded_clip_with_duplicate_gif_file(
        self, tmp_path,
    ):
        from app.task_engine.schema import connect_task_db
        from app.task_engine.artifacts import resolve_materialize_inputs

        conn = connect_task_db(tmp_path / "task.db")
        _setup_video(conn, tmp_path)
        _insert_gif_clip_stage(conn, "gc-1", "clip-1", "succeeded")

        gif_path, gif_sha, gif_size = _write_gif(tmp_path, "clip-1")
        manifest_path = _write_manifest(tmp_path, "clip-1", gif_path, gif_sha)
        _insert_artifact(conn, "gc-1", "gif_file", gif_path, "clip-1",
                         gif_sha, gif_size)
        # Duplicate gif_file for the same clip (different path).
        dup_path = tmp_path / "dup.gif"
        dup_path.write_bytes(b"GIF89a-dup")
        _insert_artifact(conn, "gc-1", "gif_file", str(dup_path), "clip-1",
                         hashlib.sha256(b"GIF89a-dup").hexdigest(),
                         len(b"GIF89a-dup"))
        man_sha = hashlib.sha256(open(manifest_path, "rb").read()).hexdigest()
        _insert_artifact(conn, "gc-1", "gif_clip_manifest", manifest_path,
                         "clip-1", man_sha,
                         len(open(manifest_path, "rb").read()))

        with pytest.raises(ValueError):
            resolve_materialize_inputs(conn, "v1")
        conn.close()


# ---------------------------------------------------------------------------
# §3.3: envelope must include ALL terminal gif_clip statuses
# ---------------------------------------------------------------------------


class TestMaterializeEnvelopeAllTerminalStates:
    """One succeeded, one needs_attention, one cancelled gif_clip stage.

    The materialize input envelope's ``stage_statuses`` must contain all
    three terminal states (with stage_id / clip_id / status / attempt_count
    / last_error), not only the succeeded clip.
    """

    def test_envelope_includes_succeeded_needs_attention_and_cancelled(
        self, tmp_path,
    ):
        from app.task_engine.schema import connect_task_db
        from app.task_engine.artifacts import (
            build_materialize_input_envelope,
            resolve_materialize_inputs,
        )

        conn = connect_task_db(tmp_path / "task.db")
        _setup_video(conn, tmp_path)

        # clip-succ: succeeded with valid artifact pair.
        _insert_gif_clip_stage(conn, "gc-succ", "clip-succ", "succeeded")
        gif_path, gif_sha, gif_size = _write_gif(tmp_path, "clip-succ")
        manifest_path = _write_manifest(tmp_path, "clip-succ", gif_path, gif_sha)
        _insert_artifact(conn, "gc-succ", "gif_file", gif_path, "clip-succ",
                         gif_sha, gif_size)
        _insert_artifact(conn, "gc-succ", "gif_clip_manifest", manifest_path,
                         "clip-succ",
                         hashlib.sha256(open(manifest_path, "rb").read()).hexdigest(),
                         len(open(manifest_path, "rb").read()))

        # clip-att: needs_attention (no artifacts required).
        _insert_gif_clip_stage(conn, "gc-att", "clip-att", "needs_attention")
        # clip-can: cancelled (no artifacts required).
        _insert_gif_clip_stage(conn, "gc-can", "clip-can", "cancelled")

        mat = resolve_materialize_inputs(conn, "v1")
        envelope = build_materialize_input_envelope(mat, "v1")

        statuses = {s["clip_id"]: s["status"] for s in envelope["stage_statuses"]}
        assert statuses.get("clip-succ") == "succeeded", statuses
        assert statuses.get("clip-att") == "needs_attention", statuses
        assert statuses.get("clip-can") == "cancelled", statuses
        assert len(envelope["stage_statuses"]) == 3, envelope["stage_statuses"]

        # Each entry must carry stage_id and attempt_count for reproducibility.
        for s in envelope["stage_statuses"]:
            assert "stage_id" in s and s["stage_id"], s
            assert "attempt_count" in s, s
            assert "last_error" in s, s

        conn.close()

    def test_zero_clip_resolver_returns_explicit_empty(self, tmp_path):
        """A video with no gif_clip stages, backed by a rank_dedup manifest
        declaring clip_count=0, resolves to an explicit empty result
        (zero_clip=True).  P1-1: the manifest is now required to prove the
        zero-clip result is genuine (no lost fan-out)."""
        from app.task_engine.schema import connect_task_db
        from app.task_engine.artifacts import resolve_materialize_inputs

        conn = connect_task_db(tmp_path / "task.db")
        _setup_video(conn, tmp_path)
        _insert_rank_dedup_zero_manifest(conn, tmp_path, clip_count=0)
        # No gif_clip stages at all -> zero-clip.

        mat = resolve_materialize_inputs(conn, "v1")
        assert mat.zero_clip is True
        assert len(mat.artifacts.get("gif_file", ())) == 0
        assert len(mat.stage_statuses) == 0
        conn.close()


# ---------------------------------------------------------------------------
# §5 (P1-1): zero-clip must require a full gif_clip scan + a zero-declaring
# rank_dedup manifest.  Non-terminal gif_clip stages must NOT silently
# become a false zero-clip success.
# ---------------------------------------------------------------------------


def _insert_rank_dedup_zero_manifest(conn, tmp_path, video_id="v1", clip_count=0):
    """Insert a succeeded rank_dedup stage + manifest artifact."""
    import hashlib
    from app.task_engine.artifacts import make_artifact_id
    from app.task_engine.fingerprints import sha256_file

    rd_stage_id = "rd-zero"
    work = tmp_path / "rd_work"
    work.mkdir(parents=True, exist_ok=True)
    p = work / "rank_dedup_manifest.json"
    p.write_text(json.dumps({
        "schema_version": 1, "stage": "rank_dedup",
        "clip_count": clip_count, "clips": [], "output_key": "rank_dedup",
    }))
    sha = sha256_file(p)
    aid = make_artifact_id(
        stage_id=rd_stage_id, artifact_kind="rank_dedup_manifest",
        clip_id=None, normalized_path=str(p),
    )
    conn.execute(
        "INSERT INTO task_stages (stage_id, video_id, stage_name, clip_id, "
        "input_key, status, created_at, updated_at) "
        "VALUES (?, ?, 'rank_dedup', NULL, 'from:synthesize', 'succeeded', ?, ?)",
        (rd_stage_id, video_id, _now(), _now()),
    )
    conn.execute(
        "INSERT INTO task_artifacts (artifact_id, job_id, video_id, stage_name, "
        "clip_id, path, sha256, size_bytes, provenance_json, created_at, "
        "stage_id, artifact_kind) "
        "VALUES (?, 'j1', ?, 'rank_dedup', NULL, ?, ?, ?, '{}', ?, ?, 'rank_dedup_manifest')",
        (aid, video_id, str(p), sha, p.stat().st_size, _now(), rd_stage_id),
    )
    conn.commit()


class TestZeroClipFullStageGuard:
    """P1-1 (fifth-review §5): the resolver must scan ALL gif_clip stages."""

    def test_materialize_rejects_pending_gif_clip_as_zero_clip(self, tmp_path):
        """A pending gif_clip stage means fan-out is not done - the resolver
        must NOT report zero_clip=True (that would create a false success)."""
        from app.task_engine.schema import connect_task_db
        from app.task_engine.artifacts import resolve_materialize_inputs

        conn = connect_task_db(tmp_path / "task.db")
        _setup_video(conn, tmp_path)
        _insert_rank_dedup_zero_manifest(conn, tmp_path)
        # A pending gif_clip exists - fan-out not complete.
        _insert_gif_clip_stage(conn, "gc-pending", "clip-p", "pending")

        with pytest.raises(ValueError, match="non-terminal|pending"):
            resolve_materialize_inputs(conn, "v1")
        conn.close()

    def test_materialize_rejects_retry_wait_gif_clip_as_zero_clip(self, tmp_path):
        from app.task_engine.schema import connect_task_db
        from app.task_engine.artifacts import resolve_materialize_inputs

        conn = connect_task_db(tmp_path / "task.db")
        _setup_video(conn, tmp_path)
        _insert_rank_dedup_zero_manifest(conn, tmp_path)
        _insert_gif_clip_stage(conn, "gc-rw", "clip-r", "retry_wait")

        with pytest.raises(ValueError, match="non-terminal|retry_wait"):
            resolve_materialize_inputs(conn, "v1")
        conn.close()

    def test_zero_clip_requires_rank_manifest_declaring_zero(self, tmp_path):
        """Zero-clip must be backed by a rank_dedup manifest that actually
        declares clip_count=0.  Without that manifest, the resolver cannot
        prove the materialize was created from a true zero-clip result."""
        from app.task_engine.schema import connect_task_db
        from app.task_engine.artifacts import resolve_materialize_inputs

        conn = connect_task_db(tmp_path / "task.db")
        _setup_video(conn, tmp_path)
        # No gif_clip stages AND no rank_dedup manifest -> cannot prove
        # zero-clip; reject instead of silently returning zero_clip=True.
        with pytest.raises(ValueError, match="rank_dedup|zero"):
            resolve_materialize_inputs(conn, "v1")
        conn.close()

    def test_zero_clip_with_rank_manifest_declaring_clips_rejected(self, tmp_path):
        """A rank_dedup manifest declaring clip_count=2 but no gif_clip
        stages means stages were lost - must NOT become zero_clip=True."""
        from app.task_engine.schema import connect_task_db
        from app.task_engine.artifacts import resolve_materialize_inputs

        conn = connect_task_db(tmp_path / "task.db")
        _setup_video(conn, tmp_path)
        _insert_rank_dedup_zero_manifest(conn, tmp_path, clip_count=2)

        with pytest.raises(ValueError):
            resolve_materialize_inputs(conn, "v1")
        conn.close()

    def test_zero_clip_succeeds_with_declaring_manifest(self, tmp_path):
        """Happy zero-clip: no gif_clip stages + rank_dedup manifest
        declaring clip_count=0 -> zero_clip=True."""
        from app.task_engine.schema import connect_task_db
        from app.task_engine.artifacts import resolve_materialize_inputs

        conn = connect_task_db(tmp_path / "task.db")
        _setup_video(conn, tmp_path)
        _insert_rank_dedup_zero_manifest(conn, tmp_path, clip_count=0)

        mat = resolve_materialize_inputs(conn, "v1")
        assert mat.zero_clip is True
        assert len(mat.artifacts.get("gif_file", ())) == 0
        assert len(mat.stage_statuses) == 0
        conn.close()
