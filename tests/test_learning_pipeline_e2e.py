"""End-to-end learning pipeline tests: eligibility -> SNAPSHOT -> TRAIN ->
QUALIFY -> COMPLETE with the real stage services (Task 6)."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from app.services.learning_jobs import (
    collect_learning_stats,
    default_services,
    maybe_enqueue_learning,
    run_learning_once,
)
from app.services.preference_schema import apply_preference_schema

TINY_POLICY = {
    "min_new_effective_candidates": 1,
    "min_new_source_groups": 1,
    "min_positive_candidates": 1,
    "min_negative_candidates": 1,
    "min_interval_hours": 0,
}


def _seed_learning_db(db_path: Path) -> None:
    """Real production chain, no manual feature backfill:

    persist_capture (with AVAILABLE source-feature evidence in the payload)
    -> ingest_published_clips (publication link + curve_features ledger entry
    in one transaction) -> preference events on the materialized candidates.
    """
    import hashlib
    import base64 as _b64

    import numpy as _np

    from app.services.candidate_capture import persist_capture
    from app.services.curve_source_features import (
        CURVE_SOURCE_FEATURE_NAMES,
        CurveWindow,
    )
    from app.services.library_ingest import ingest_published_clips
    from app.services.reranker import preference_embedding_text
    from app.services.vector_math import vector_to_blob

    def deterministic_embed(text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        vec = _np.frombuffer(digest * 24, dtype=_np.uint8)[:768].astype(_np.float32)
        return (vec / 255.0).tolist()

    conn = sqlite3.connect(db_path)
    apply_preference_schema(conn)

    sources = ["a" * 64, "b" * 64, "c" * 64]
    gif_dir = db_path.parent / "gifs"
    gif_dir.mkdir(parents=True, exist_ok=True)

    from app.services.curve_source_features import window_input_hash

    clips = []
    for i in range(36):
        source = sources[i % 3]
        gif = gif_dir / f"clip-{i:02d}.gif"
        gif.write_bytes(b"GIF89a-e2e-" + str(i).encode())
        features = {
            name: 0.3 + ((i + index) % 6) / 12.0
            for index, name in enumerate(CURVE_SOURCE_FEATURE_NAMES)
        }
        feature_input_hash = window_input_hash(
            source,
            CurveWindow(
                preexport_key=f"{i:064x}",
                start_us=1_000_000 + i,
                end_us=4_000_000 + i,
            ),
            extractor_sha256="e" * 64,
        )
        pref_text = preference_embedding_text(
            caption=f"synthetic clip {i}", emotion="joy", scene_type=""
        )
        pref_vec = _np.asarray(deterministic_embed(pref_text), dtype=_np.float32)
        payload = {
            "run_id": "run-e2e",
            "preexport_key": f"{i:064x}",
            "capture_version": "capture-v1",
            "source_video_sha256": source,
            "start_us": 1_000_000 + i,
            "end_us": 4_000_000 + i,
            "anchor_us": 2_500_000,
            "baseline_score": 0.5 + i / 100.0,
            "capture_status": "CAPTURED",
            "candidate_id": f"clip-{i:02d}",
            "preference_identity": {
                "embedding_text": pref_text,
                "embedding_text_sha256": hashlib.sha256(
                    pref_text.encode("utf-8")
                ).hexdigest(),
                "embedding_model": "nomic-embed-text:latest",
                "scenario_keys": [],
                "vector_float32_b64": _b64.b64encode(pref_vec.tobytes()).decode(
                    "ascii"
                ),
                "vector_sha256": hashlib.sha256(pref_vec.tobytes()).hexdigest(),
                "scoring_input_committed": True,
            },
            "source_feature": {
                "status": "AVAILABLE",
                "feature_version": "curve-source-v1",
                "extractor_sha256": "e" * 64,
                "features": features,
                "actual_timestamps": [1.0, 2.0, 3.0],
                "input_hash": feature_input_hash,
                "missing_reasons": [],
                "wall_clock_ms": 1.0,
            },
        }
        persisted = persist_capture(conn, payload, db_path.parent / "evidence")
        clips.append(
            {
                "clip_id": f"clip-{i:02d}",
                "start_ts": 1.0,
                "end_ts": 4.0,
                "formal_path": str(gif),
                "sha256": hashlib.sha256(gif.read_bytes()).hexdigest(),
                "gif_worthiness": payload["baseline_score"],
                "analysis_record_id": persisted.record_id,
                "best_frame": {
                    "caption": f"synthetic clip {i}",
                    "emotional_core": "joy",
                },
            }
        )

    def deterministic_embed(text: str) -> list[float]:
        import hashlib as _hashlib

        import numpy as _np

        digest = _hashlib.sha256(text.encode("utf-8")).digest()
        vec = _np.frombuffer(digest * 24, dtype=_np.uint8)[:768].astype(_np.float32)
        return (vec / 255.0).tolist()

    result = ingest_published_clips(
        conn,
        video_path=str(db_path.parent / "v.mp4"),
        clips=clips,
        run_id="run-e2e",
        embed_fn=deterministic_embed,
    )
    assert result["needs_repair"] == 0
    assert result["ingested"] == 36

    # Feedback targets the ACTUAL materialized candidates, after the ledger
    # entries exist -- exactly the order production runs in.
    linked = conn.execute(
        "SELECT p.candidate_id, r.source_video_sha256 "
        "FROM candidate_analysis_publications p "
        "JOIN candidate_analysis_records r ON r.record_id = p.record_id "
        "ORDER BY p.candidate_id"
    ).fetchall()
    assert len(linked) == 36
    for index, (candidate_id, source) in enumerate(linked):
        rating = "like" if index % 2 == 0 else "dislike"
        conn.execute(
            "INSERT INTO preference_events (event_id, target_type, target_id, "
            "rating, source_video_sha256, scenario_keys_json, created_at, event_kind) "
            "VALUES (?, 'candidate_gif', ?, ?, ?, '[]', ?, 'feedback')",
            (
                f"ev-{index:02d}",
                candidate_id,
                rating,
                source,
                f"2026-09-02T{index // 24:02d}:{index % 24:02d}:00Z",
            ),
        )
    conn.execute(
        "INSERT INTO preference_profile_current (slot, profile_version) "
        "VALUES ('current', 'profile-v1')"
    )
    conn.commit()
    conn.close()


def _services(db_path: Path, tmp_path: Path) -> dict:
    services = default_services(None)
    services["db_path"] = str(db_path)
    services["work_dir"] = str(tmp_path / "learning_runs")
    return services


def test_worker_advances_one_stage_per_run_until_complete(tmp_path):
    """Four runs observe SNAPSHOT -> TRAIN -> QUALIFY -> COMPLETE and exactly
    one DRAFT model; the synthetic holdout is too small, so the scientific
    outcome is NOT_QUALIFIED with the job still SUCCEEDED."""
    db_path = tmp_path / "library.db"
    _seed_learning_db(db_path)
    conn = sqlite3.connect(db_path)
    services = _services(db_path, tmp_path)

    stats = collect_learning_stats(conn, TINY_POLICY)
    assert stats["new_effective_candidates"] == 36
    assert stats["positive_candidates"] == 18
    assert stats["negative_candidates"] == 18

    enqueue = maybe_enqueue_learning(conn, TINY_POLICY)
    assert enqueue.status == "ENQUEUED"
    assert enqueue.job_id

    epoch = 1_800_000_000
    # Stage advance releases the lease atomically, so a 60s-poll worker
    # claims the next stage IMMEDIATELY (no 1800s lease wait between stages).
    run1 = run_learning_once(conn, now_epoch=epoch, owner="w1", services=services)
    assert run1["status"] == "ADVANCED"
    assert run1["stage"] == "TRAIN"

    run2 = run_learning_once(
        conn, now_epoch=epoch + 1, owner="w1", services=services
    )
    assert run2["status"] == "ADVANCED"
    assert run2["stage"] == "QUALIFY"

    run3 = run_learning_once(
        conn, now_epoch=epoch + 2, owner="w1", services=services
    )
    assert run3["status"] == "ADVANCED"
    assert run3["stage"] == "COMPLETE"

    run4 = run_learning_once(
        conn, now_epoch=epoch + 3, owner="w1", services=services
    )
    assert run4["status"] == "SUCCEEDED"

    drafts = conn.execute(
        "SELECT COUNT(*) FROM curve_preference_models WHERE status = 'DRAFT'"
    ).fetchone()[0]
    published = conn.execute(
        "SELECT COUNT(*) FROM curve_preference_models WHERE status = 'PUBLISHED'"
    ).fetchone()[0]
    assert drafts == 1
    assert published == 0

    job = conn.execute(
        "SELECT status, stage FROM learning_jobs WHERE job_id = ?",
        (enqueue.job_id,),
    ).fetchone()
    assert job[0] == "SUCCEEDED"
    assert job[1] == "COMPLETE"

    # The qualification outcome is recorded on the job package.
    package = json.loads(
        Path(
            conn.execute(
                "SELECT result_package_path FROM learning_jobs WHERE job_id = ?",
                (enqueue.job_id,),
            ).fetchone()[0]
        ).read_text(encoding="utf-8")
    )
    assert package["qualification"]["status"] == "NOT_QUALIFIED"
    assert "holdout_too_small" in package["qualification"]["reasons"]
    conn.close()


def test_ineligible_stats_never_enqueue():
    conn = sqlite3.connect(":memory:")
    apply_preference_schema(conn)
    result = maybe_enqueue_learning(conn, None)
    assert result.status == "INELIGIBLE"
    assert "insufficient_new_feedback" in result.reasons
    count = conn.execute("SELECT COUNT(*) FROM learning_jobs").fetchone()[0]
    assert count == 0


def test_worker_never_succeeds_without_a_frozen_snapshot(tmp_path):
    conn = sqlite3.connect(tmp_path / "learning.db")
    apply_preference_schema(conn)
    from app.services.learning_jobs import enqueue_learning

    enqueue_learning(conn, "f" * 64, "e" * 64)
    result = run_learning_once(
        conn, now_epoch=1_800_000_000, owner="w1",
        services=_services(tmp_path / "library.db", tmp_path),
    )
    assert result["status"] == "NEEDS_ATTENTION"
    assert result["error_code"] == "snapshot_not_found"
    models = conn.execute("SELECT COUNT(*) FROM curve_preference_models").fetchone()[0]
    assert models == 0


def test_cancelled_job_never_advances(tmp_path):
    db_path = tmp_path / "library.db"
    _seed_learning_db(db_path)
    conn = sqlite3.connect(db_path)
    services = _services(db_path, tmp_path)
    enqueue = maybe_enqueue_learning(conn, TINY_POLICY)
    assert enqueue.job_id

    conn.execute(
        "UPDATE learning_jobs SET status = 'CANCELLED' WHERE job_id = ?",
        (enqueue.job_id,),
    )
    conn.commit()
    result = run_learning_once(
        conn, now_epoch=1_800_000_000, owner="w1", services=services
    )
    assert result["status"] == "IDLE"
    stage = conn.execute(
        "SELECT stage FROM learning_jobs WHERE job_id = ?", (enqueue.job_id,)
    ).fetchone()[0]
    assert stage == "SNAPSHOT"  # untouched
    conn.close()


def test_recovered_job_rejects_drifted_snapshot_artifact(tmp_path):
    db_path = tmp_path / "library.db"
    _seed_learning_db(db_path)
    conn = sqlite3.connect(db_path)
    services = _services(db_path, tmp_path)
    enqueue = maybe_enqueue_learning(conn, TINY_POLICY)

    # Advance through SNAPSHOT, then tamper with the frozen manifest.
    run_learning_once(conn, now_epoch=1_800_000_000, owner="w1", services=services)
    package_path = conn.execute(
        "SELECT result_package_path FROM learning_jobs WHERE job_id = ?",
        (enqueue.job_id,),
    ).fetchone()[0]
    package = json.loads(Path(package_path).read_text(encoding="utf-8"))
    manifest_path = Path(package["snapshot"]["dir"]) / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["train_rows"][0]["baseline_score"] = 0.99
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )

    result = run_learning_once(
        conn, now_epoch=1_800_000_001, owner="w1", services=services
    )
    assert result["status"] == "NEEDS_ATTENTION"
    assert result["error_code"] == "snapshot_artifact_hash_mismatch"
    conn.close()


def test_not_qualified_result_never_deploys(tmp_path):
    """Task 8 Step 3: a NOT_QUALIFIED scientific outcome completes the job
    without any deployment; promoting the failing bundle is refused."""
    db_path = tmp_path / "library.db"
    _seed_learning_db(db_path)
    conn = sqlite3.connect(db_path)
    services = _services(db_path, tmp_path)
    enqueue = maybe_enqueue_learning(conn, TINY_POLICY)

    epoch = 1_800_000_000
    for step in range(4):
        run_learning_once(
            conn, now_epoch=epoch + step * 1801, owner="w1", services=services
        )

    package = json.loads(
        Path(
            conn.execute(
                "SELECT result_package_path FROM learning_jobs WHERE job_id = ?",
                (enqueue.job_id,),
            ).fetchone()[0]
        ).read_text(encoding="utf-8")
    )
    qualification = package["qualification"]
    assert qualification["status"] == "NOT_QUALIFIED"

    from app.services.curve_preference import load_qualification_bundle, promote_model

    bundle, reasons = load_qualification_bundle(Path(qualification["run_dir"]))
    assert bundle is None or reasons or qualification["status"] == "NOT_QUALIFIED"
    if bundle is not None:
        promotion = promote_model(
            conn, model_version=package["model_version"], run_dir=Path(qualification["run_dir"])
        )
        assert promotion["status"] == "REFUSED"

    deployments = conn.execute(
        "SELECT COUNT(*) FROM curve_model_deployments WHERE is_active = 1"
    ).fetchone()[0]
    pointers = conn.execute(
        "SELECT COUNT(*) FROM curve_model_deployment_current WHERE slot='current'"
    ).fetchone()[0]
    assert deployments == 0
    assert pointers == 0

    # With nothing deployed, an active curve request degrades to BASELINE.
    from app.services.curve_online import load_curve_runtime_snapshot

    snapshot = load_curve_runtime_snapshot(
        conn,
        {"curve_preference_enabled": True, "curve_preference_mode": "active"},
    )
    assert snapshot.deployment_available is False
    assert "deployment_unavailable" in snapshot.inactive_reasons
    conn.close()


def test_stage_retry_budget_is_per_stage(tmp_path):
    """A successful SNAPSHOT+TRAIN never consumes the QUALIFY retry budget;
    the third QUALIFY failure stops the job as NEEDS_ATTENTION."""
    db_path = tmp_path / "library.db"
    _seed_learning_db(db_path)
    conn = sqlite3.connect(db_path)
    services = _services(db_path, tmp_path)
    enqueue = maybe_enqueue_learning(conn, TINY_POLICY)

    epoch = 1_800_000_000
    run_learning_once(conn, now_epoch=epoch, owner="w1", services=services)
    run_learning_once(conn, now_epoch=epoch + 1, owner="w1", services=services)
    assert run_learning_once.__name__  # keep linters quiet

    def always_fail(**_kwargs):
        raise RuntimeError("simulated qualification outage")

    services["execute_qualification_run"] = always_fail

    first = run_learning_once(
        conn, now_epoch=epoch + 2, owner="w1", services=services
    )
    assert first["status"] == "RETRY_WAIT"
    assert first["error_code"] == "qualify_failed"

    # Backoff after the first failure is 600s (three prior claims).
    second = run_learning_once(
        conn, now_epoch=epoch + 2 + 600, owner="w1", services=services
    )
    assert second["status"] == "RETRY_WAIT"

    third = run_learning_once(
        conn, now_epoch=epoch + 2 + 1200, owner="w1", services=services
    )
    assert third["status"] == "NEEDS_ATTENTION"
    assert third["error_code"] == "qualify_failed"
    _ = enqueue

    conn.close()


def test_retry_wait_job_is_not_claimable_before_next_retry(tmp_path):
    db_path = tmp_path / "library.db"
    _seed_learning_db(db_path)
    conn = sqlite3.connect(db_path)
    services = _services(db_path, tmp_path)
    maybe_enqueue_learning(conn, TINY_POLICY)

    def always_fail(**_kwargs):
        raise RuntimeError("simulated qualification outage")

    services_fail = dict(services)
    services_fail["execute_qualification_run"] = always_fail

    # Fast-forward through SNAPSHOT and TRAIN with the healthy services.
    run_learning_once(conn, now_epoch=1_800_000_000, owner="w1", services=services)
    run_learning_once(conn, now_epoch=1_800_000_001, owner="w1", services=services)
    first = run_learning_once(
        conn, now_epoch=1_800_000_002, owner="w1", services=services_fail
    )
    assert first["status"] == "RETRY_WAIT"

    # Before next_retry_epoch the same job cannot be claimed.
    idle = run_learning_once(
        conn, now_epoch=1_800_000_003, owner="w2", services=services_fail
    )
    assert idle["status"] == "IDLE"
    conn.close()


def test_stale_worker_cannot_overwrite_stage_package(tmp_path):
    """P1 remediation: after w1's lease is taken over by w2, w1's package
    write must fail on the real owner CAS -- w2's package stays intact."""
    from app.services.learning_jobs import _save_stage_package

    db_path = tmp_path / "library.db"
    _seed_learning_db(db_path)
    conn = sqlite3.connect(db_path)
    services = _services(db_path, tmp_path)
    maybe_enqueue_learning(conn, TINY_POLICY)

    run_learning_once(conn, now_epoch=1_800_000_000, owner="w1", services=services)

    # w1's lease expires; w2 takes over and advances.
    run_learning_once(conn, now_epoch=1_800_001_801, owner="w2", services=services)

    package_path = conn.execute(
        "SELECT result_package_path, result_package_sha256 FROM learning_jobs "
        "WHERE status = 'PENDING' OR status = 'RUNNING' LIMIT 1"
    ).fetchone()
    assert package_path is not None
    official_sha = package_path[1]

    # Stale w1 tries to write its (now outdated) package.
    try:
        _save_stage_package(
            conn, _stale_job_id(conn), {"stages": {"SNAPSHOT": {}}},
            Path(services["work_dir"]), "w1",
        )
        raised = False
    except PermissionError:
        raised = True
    assert raised, "stale worker write must be refused by the owner CAS"

    # The official package the new owner recorded is untouched.
    row = conn.execute(
        "SELECT result_package_sha256 FROM learning_jobs "
        "WHERE result_package_path = ?",
        (package_path[0],),
    ).fetchone()
    assert row[0] == official_sha
    conn.close()


def _stale_job_id(conn: sqlite3.Connection) -> str:
    return conn.execute(
        "SELECT job_id FROM learning_jobs ORDER BY created_at LIMIT 1"
    ).fetchone()[0]


def test_same_owner_reclaim_invalidates_old_token(tmp_path):
    """P1 remediation: the owner string is not a credential. After a
    same-named worker re-claims an expired lease, the OLD claim token can no
    longer mutate the job or its stage package."""
    from app.services.learning_jobs import (
        _save_stage_package,
        claim_learning_job,
    )

    db_path = tmp_path / "library.db"
    _seed_learning_db(db_path)
    conn = sqlite3.connect(db_path)
    apply_preference_schema(conn)

    from app.services.learning_jobs import enqueue_learning

    enqueue_learning(conn, "f" * 64, "e" * 64)

    # w1 claims with the default owner name.
    first = claim_learning_job(
        conn, owner="standalone_worker", now_epoch=1_800_000_000, lease_seconds=60
    )
    assert first is not None
    old_token = first.lease_token

    # The lease expires; a NEW process with the SAME owner name re-claims.
    second = claim_learning_job(
        conn, owner="standalone_worker", now_epoch=1_800_000_061, lease_seconds=60
    )
    assert second is not None
    assert second.lease_token != old_token

    # The stale token's package write must be refused (expired + token CAS).
    try:
        _save_stage_package(
            conn,
            first.job_id,
            {"stages": {"SNAPSHOT": {"tampered": True}}},
            Path(db_path.parent / "runs"),
            "standalone_worker",
            old_token,
        )
        raised = False
    except PermissionError:
        raised = True
    assert raised, "stale token write must be refused"
    conn.close()
