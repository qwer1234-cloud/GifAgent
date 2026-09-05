"""Curve preference qualification: source-isolated profile snapshots (Task 3),
evidence freezing (Task 4), paired metrics/gates (Task 5) and run bundles
(Task 6)."""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

from app.services.preference_schema import apply_preference_schema
from app.services.preference_types import ProfileBuildConfig

TRAIN_SOURCES = ("train-src-1", "train-src-2", "train-src-3")
HOLDOUT_SOURCES = ("holdout-src-1",)
BASE_TS = datetime(2026, 8, 1, tzinfo=timezone.utc)

QUALIFICATION_CONFIG = ProfileBuildConfig(
    recency_enabled=True,
    scenario_min_feedback=4,
)


def _unit_vector(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    vec = rng.random(768).astype(np.float32)
    norm = float(np.linalg.norm(vec))
    return (vec / norm).astype(np.float32)


def _ts(seconds: int) -> str:
    return (BASE_TS + timedelta(seconds=seconds)).isoformat()


def _insert_candidate(
    conn: sqlite3.Connection,
    *,
    candidate_id: str,
    source: str,
    tags: list[str],
    scenario_keys: list[str],
) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO candidate_gifs
           (candidate_id, source_run_id, source_run_candidate_id,
            source_video_sha256, source_video_path, start_sec, end_sec,
            status, tags_json, scenario_keys_json)
           VALUES (:candidate_id, 'run-1', :rcid, :source, :path, 0.0, 5.0,
                   'candidate', :tags, :scenario)""",
        {
            "candidate_id": candidate_id,
            "rcid": f"rc-{candidate_id}",
            "source": source,
            "path": f"/v/{source}.mp4",
            "tags": json.dumps(tags),
            "scenario": json.dumps(scenario_keys),
        },
    )


def _insert_event(
    conn: sqlite3.Connection,
    *,
    event_id: str,
    target_id: str,
    rating: str,
    source: str,
    scenario_keys: list[str],
    created_at: str,
    event_kind: str = "feedback",
    supersedes: str | None = None,
) -> None:
    conn.execute(
        """INSERT INTO preference_events
           (event_id, target_type, target_id, rating, source_video_sha256,
            scenario_keys_json, created_at, event_kind, supersedes_event_id)
           VALUES (:event_id, 'candidate_gif', :target_id, :rating, :source,
                   :scenario, :created_at, :event_kind, :supersedes)""",
        {
            "event_id": event_id,
            "target_id": target_id,
            "rating": rating,
            "source": source,
            "scenario": json.dumps(scenario_keys),
            "created_at": created_at,
            "event_kind": event_kind,
            "supersedes": supersedes,
        },
    )


def _insert_vector(conn: sqlite3.Connection, *, candidate_id: str, seed: int) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO candidate_vectors
           (candidate_id, vector_type, embedding_model, embedding_dim,
            vector_blob, normalized)
           VALUES (:candidate_id, 'clip', 'nomic-embed-text:latest', 768,
                   :blob, 1)""",
        {"candidate_id": candidate_id, "blob": _unit_vector(seed).tobytes()},
    )


def _tick(counter: list[int]) -> str:
    counter[0] += 1
    return _ts(counter[0] * 60)


def build_qualification_db(
    *,
    with_holdout_clone: bool = False,
    correction_rating: str = "favorite",
    train_vector_override: int | None = None,
    train_tag_override: bool = False,
) -> sqlite3.Connection:
    """Deterministic 4-source database: 3 train sources + 1 holdout source.

    24 train likes (one later corrected), 12 train dislikes, 4 holdout
    likes and 2 holdout dislikes with holdout-only tags/scenario keys.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_preference_schema(conn)
    tick = [0]

    for i in range(24):
        tags = ["tag-a", f"tag-{i % 4}"] if not (train_tag_override and i == 2) else [
            "tag-override",
            f"tag-{i % 4}",
        ]
        source = TRAIN_SOURCES[i % 3]
        _insert_candidate(
            conn,
            candidate_id=f"cand-train-like-{i}",
            source=source,
            tags=tags,
            scenario_keys=["emotion:joy", f"tag:{i % 4}"],
        )
        _insert_event(
            conn,
            event_id=f"ev-train-like-{i}",
            target_id=f"cand-train-like-{i}",
            rating="like",
            source=source,
            scenario_keys=["emotion:joy", f"tag:{i % 4}"],
            created_at=_tick(tick),
        )
        _insert_vector(conn, candidate_id=f"cand-train-like-{i}", seed=100 + i)
    # Correct the first train like; the correction supersedes the original.
    _insert_event(
        conn,
        event_id="ev-train-correction-0",
        target_id="cand-train-like-0",
        rating=correction_rating,
        source=TRAIN_SOURCES[0],
        scenario_keys=[],
        created_at=_tick(tick),
        event_kind="correction",
        supersedes="ev-train-like-0",
    )
    for i in range(12):
        source = TRAIN_SOURCES[i % 3]
        _insert_candidate(
            conn,
            candidate_id=f"cand-train-dislike-{i}",
            source=source,
            tags=["tag-b", f"tag-{i % 4}"],
            scenario_keys=["emotion:sad", f"tag:{i % 4}"],
        )
        _insert_event(
            conn,
            event_id=f"ev-train-dislike-{i}",
            target_id=f"cand-train-dislike-{i}",
            rating="dislike",
            source=source,
            scenario_keys=["emotion:sad", f"tag:{i % 4}"],
            created_at=_tick(tick),
        )
        _insert_vector(
            conn, candidate_id=f"cand-train-dislike-{i}", seed=200 + i
        )
    for i in range(4):
        _insert_candidate(
            conn,
            candidate_id=f"cand-holdout-like-{i}",
            source=HOLDOUT_SOURCES[0],
            tags=["holdout-only-tag"],
            scenario_keys=["emotion:desire", "tag:holdout-only-tag"],
        )
        _insert_event(
            conn,
            event_id=f"ev-holdout-like-{i}",
            target_id=f"cand-holdout-like-{i}",
            rating="like",
            source=HOLDOUT_SOURCES[0],
            scenario_keys=["emotion:desire", "tag:holdout-only-tag"],
            created_at=_tick(tick),
        )
        _insert_vector(conn, candidate_id=f"cand-holdout-like-{i}", seed=300 + i)
    for i in range(2):
        _insert_candidate(
            conn,
            candidate_id=f"cand-holdout-dislike-{i}",
            source=HOLDOUT_SOURCES[0],
            tags=["holdout-only-tag"],
            scenario_keys=["emotion:desire", "tag:holdout-only-tag"],
        )
        _insert_event(
            conn,
            event_id=f"ev-holdout-dislike-{i}",
            target_id=f"cand-holdout-dislike-{i}",
            rating="dislike",
            source=HOLDOUT_SOURCES[0],
            scenario_keys=["emotion:desire", "tag:holdout-only-tag"],
            created_at=_tick(tick),
        )
        _insert_vector(conn, candidate_id=f"cand-holdout-dislike-{i}", seed=400 + i)
    if with_holdout_clone:
        # A holdout candidate whose vector blob is byte-identical to a train
        # candidate's blob.  It must never leak into the isolated profile.
        _insert_candidate(
            conn,
            candidate_id="cand-holdout-clone",
            source=HOLDOUT_SOURCES[0],
            tags=["clone-tag"],
            scenario_keys=["emotion:desire"],
        )
        _insert_event(
            conn,
            event_id="ev-holdout-clone",
            target_id="cand-holdout-clone",
            rating="like",
            source=HOLDOUT_SOURCES[0],
            scenario_keys=["emotion:desire"],
            created_at=_tick(tick),
        )
        conn.execute(
            """INSERT INTO candidate_vectors
               (candidate_id, vector_type, embedding_model, embedding_dim,
                vector_blob, normalized)
               SELECT 'cand-holdout-clone', 'clip', embedding_model,
                      embedding_dim, vector_blob, normalized
               FROM candidate_vectors WHERE candidate_id = :train_id""",
            {"train_id": "cand-train-like-1"},
        )
    if train_vector_override is not None:
        _insert_vector(
            conn, candidate_id="cand-train-like-2", seed=train_vector_override
        )
    conn.commit()
    return conn


def _snapshot(conn: sqlite3.Connection, config: ProfileBuildConfig | None = None):
    from app.services.preference_profile_snapshot import (
        build_source_isolated_profile_snapshot,
    )

    return build_source_isolated_profile_snapshot(
        conn,
        excluded_source_videos=set(HOLDOUT_SOURCES),
        config=config or QUALIFICATION_CONFIG,
    )


# ---------------------------------------------------------------------------
# Task 3: leakage isolation
# ---------------------------------------------------------------------------


def test_snapshot_excludes_holdout_sources_from_every_profile_output():
    conn = build_qualification_db()

    snapshot = _snapshot(conn)

    manifest = snapshot.manifest
    event_sources = {event["source_video_sha256"] for event in manifest["events"]}
    assert event_sources == set(TRAIN_SOURCES)
    assert not (event_sources & set(HOLDOUT_SOURCES))
    vector_ids = {vector["candidate_id"] for vector in manifest["vectors"]}
    assert vector_ids
    assert all(not vid.startswith("cand-holdout") for vid in vector_ids)

    profile_rows = manifest["profile_rows"]
    global_rows = [row for row in profile_rows if row["scope"] == "global"]
    assert len(global_rows) == 1
    assert global_rows[0]["like_count"] == 24
    assert global_rows[0]["dislike_count"] == 12

    scenario_keys = {
        row["scenario_key"] for row in profile_rows if row["scope"] == "scenario"
    }
    assert "emotion:joy" in scenario_keys
    assert "emotion:sad" in scenario_keys
    assert "emotion:desire" not in scenario_keys

    global_tags = global_rows[0]["tag_weights"]
    assert "holdout-only-tag" not in global_tags
    assert "tag:holdout-only-tag" not in global_tags

    # Snapshot tuple records the exclusion deterministically.
    assert snapshot.excluded_source_videos == HOLDOUT_SOURCES


def test_identical_holdout_vector_cannot_enter_profile():
    base = build_qualification_db()
    base_snapshot = _snapshot(base)

    cloned = build_qualification_db(with_holdout_clone=True)
    cloned_snapshot = _snapshot(cloned)

    # The clone's byte-identical vector must not change any centroid,
    # event, count or tag weight.
    assert cloned_snapshot.manifest_sha256 == base_snapshot.manifest_sha256
    vector_ids = {v["candidate_id"] for v in base_snapshot.manifest["vectors"]}
    assert "cand-holdout-clone" not in vector_ids
    assert "cand-holdout-clone" not in {
        e["target_id"] for e in base_snapshot.manifest["events"]
    }


def test_snapshot_builder_ignores_published_profile_and_does_not_publish():
    conn = build_qualification_db()
    conn.execute(
        """INSERT INTO preference_profile_current (slot, profile_version, published_at)
           VALUES ('current', 'profile_published_elsewhere', '2026-08-01T00:00:00')"""
    )
    conn.commit()

    snapshot = _snapshot(conn)

    assert snapshot.profile_version != "profile_published_elsewhere"
    row = conn.execute(
        "SELECT profile_version FROM preference_profile_current WHERE slot = 'current'"
    ).fetchone()
    assert row["profile_version"] == "profile_published_elsewhere"


def test_snapshot_is_deterministic_for_the_same_database_snapshot():
    first_conn = build_qualification_db()
    first = _snapshot(first_conn)
    again_same_conn = _snapshot(first_conn)
    second_conn = build_qualification_db()
    second = _snapshot(second_conn)

    assert again_same_conn.manifest_sha256 == first.manifest_sha256
    assert second.manifest_sha256 == first.manifest_sha256
    assert second.profile_version == first.profile_version
    assert second.config_json == first.config_json


@pytest.mark.parametrize(
    "kwargs",
    [
        {"train_vector_override": 999},
        {"correction_rating": "dislike"},
        {"train_tag_override": True},
    ],
)
def test_snapshot_sha_changes_when_training_inputs_change(kwargs):
    baseline = _snapshot(build_qualification_db())
    variant = _snapshot(build_qualification_db(**kwargs))

    assert variant.manifest_sha256 != baseline.manifest_sha256


def test_snapshot_sha_changes_when_config_changes():
    baseline = _snapshot(build_qualification_db())
    variant = _snapshot(
        build_qualification_db(),
        config=ProfileBuildConfig(
            recency_enabled=True,
            scenario_min_feedback=4,
            dislike_weight=1.5,
        ),
    )

    assert variant.manifest_sha256 != baseline.manifest_sha256
    assert "dislike_weight" in variant.config_json


def test_snapshot_manifest_binds_centroid_blobs_and_hashes():
    conn = build_qualification_db()
    snapshot = _snapshot(conn)

    row = next(
        r for r in snapshot.manifest["profile_rows"] if r["scope"] == "global"
    )
    blob = base64.b64decode(row["liked_centroid_blob_b64"])
    assert hashlib.sha256(blob).hexdigest() == row["liked_centroid_sha256"]
    stored = conn.execute(
        """SELECT liked_centroid_blob FROM preference_profiles
           WHERE profile_version = :v AND scope = 'global'""",
        {"v": snapshot.profile_version},
    ).fetchone()
    assert hashlib.sha256(stored["liked_centroid_blob"]).hexdigest() == (
        row["liked_centroid_sha256"]
    )


def test_snapshot_manifest_binds_effective_events_and_vector_hashes():
    conn = build_qualification_db()
    snapshot = _snapshot(conn)

    events = {e["event_id"]: e for e in snapshot.manifest["events"]}
    correction = events["ev-train-correction-0"]
    assert correction["rating"] == "favorite"
    assert "ev-train-like-0" not in events  # superseded event is not effective

    vectors = {v["candidate_id"]: v for v in snapshot.manifest["vectors"]}
    blob = conn.execute(
        """SELECT vector_blob FROM candidate_vectors
           WHERE candidate_id = :cid AND vector_type = 'clip'""",
        {"cid": "cand-train-like-1"},
    ).fetchone()["vector_blob"]
    assert vectors["cand-train-like-1"]["vector_blob_sha256"] == (
        hashlib.sha256(blob).hexdigest()
    )
    assert vectors["cand-train-like-1"]["embedding_model"] == (
        "nomic-embed-text:latest"
    )
    assert vectors["cand-train-like-1"]["embedding_dim"] == 768


def test_snapshot_raises_when_profile_gates_block_build():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_preference_schema(conn)
    _insert_candidate(
        conn,
        candidate_id="cand-one",
        source=TRAIN_SOURCES[0],
        tags=["tag-a"],
        scenario_keys=["emotion:joy"],
    )
    _insert_event(
        conn,
        event_id="ev-one",
        target_id="cand-one",
        rating="like",
        source=TRAIN_SOURCES[0],
        scenario_keys=["emotion:joy"],
        created_at=_ts(60),
    )
    conn.commit()

    with pytest.raises(ValueError, match="blocked"):
        _snapshot(conn)


def test_snapshot_records_event_watermark_and_exclusions_in_manifest():
    conn = build_qualification_db()
    snapshot = _snapshot(conn)

    assert snapshot.event_watermark == snapshot.manifest["event_watermark"]
    assert snapshot.manifest["excluded_source_videos"] == sorted(HOLDOUT_SOURCES)
    assert snapshot.manifest_sha256 == hashlib.sha256(
        json.dumps(
            snapshot.manifest,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


# ---------------------------------------------------------------------------
# Task 4: enrichment parsing, evidence freezing, and input validation
# ---------------------------------------------------------------------------

from app.services.curve_preference_qualification import (  # noqa: E402
    EnrichmentError,
    QualificationInputs,
    build_qualification_inputs,
    parse_enrichment_response,
    score_evidence_from_summary,
)

VALID_VLM_JSON = json.dumps(
    {
        "caption": "a test clip",
        "emotional_core": "joy",
        "tags": ["Tag A", "tag a", "tag-b"],
        "scene_type": "Bedroom Closeup",
        "sex_act": 120,
        "gif_worthiness": -5,
        "reason": "fine",
    }
)


def test_parse_enrichment_response_accepts_plain_json():
    summary = parse_enrichment_response(
        VALID_VLM_JSON, model="test-model", enriched_at="2026-09-05T00:00:00+00:00"
    )

    assert summary["caption"] == "a test clip"
    assert summary["emotional_core"] == "joy"
    assert summary["tags"] == ["tag a", "tag-b"]
    assert summary["scene_type"] == "bedroom_closeup"
    assert summary["sex_act"] == 100
    assert summary["gif_worthiness"] == 0
    assert summary["enriched_by"] == "test-model"
    assert summary["enriched_at"] == "2026-09-05T00:00:00+00:00"


def test_parse_enrichment_response_accepts_fenced_json():
    fenced = "```json\n" + VALID_VLM_JSON + "\n```"

    summary = parse_enrichment_response(
        fenced, model="m", enriched_at="t"
    )

    assert summary["caption"] == "a test clip"


def test_parse_enrichment_response_rejects_refusal_missing_caption_and_invalid():
    for raw in (
        "I cannot help with that request.",
        json.dumps({"emotional_core": "joy"}),
        "not a json object at all",
    ):
        with pytest.raises(EnrichmentError):
            parse_enrichment_response(raw, model="m", enriched_at="t")


def test_score_evidence_from_summary_requires_both_fields():
    assert score_evidence_from_summary({"gif_worthiness": 74, "sex_act": 63}) == {
        "gif_worthiness": 74,
        "sex_act": 63,
    }
    assert score_evidence_from_summary({"gif_worthiness": 74}) is None
    assert score_evidence_from_summary({}) is None


def _write_gif(tmp_path: Path, name: str, content: bytes) -> Path:
    gif = tmp_path / name
    gif.write_bytes(content)
    return gif


def _attach_holdout_row(
    conn: sqlite3.Connection,
    tmp_path: Path,
    *,
    candidate_id: str = "cand-hold-0",
    source: str = HOLDOUT_SOURCES[0],
    rating: str = "like",
    event_id: str = "ev-hold-0",
    gif_content: bytes = b"GIF89a-payload-0",
    features: dict | None = None,
    baseline: float | None = None,
    with_vlm: bool = True,
    with_vector: bool = True,
) -> tuple[dict, Path]:
    """Create one fully consistent holdout row plus its manifest entry."""

    gif = _write_gif(tmp_path, f"{candidate_id}.gif", gif_content)
    features = features or {"extension_mean": 0.5}
    _insert_candidate(
        conn, candidate_id=candidate_id, source=source, tags=["tag-a"],
        scenario_keys=["emotion:joy"],
    )
    conn.execute(
        "UPDATE candidate_gifs SET artifact_path = :path WHERE candidate_id = :cid",
        {"path": str(gif), "cid": candidate_id},
    )
    _insert_event(
        conn, event_id=event_id, target_id=candidate_id, rating=rating,
        source=source, scenario_keys=["emotion:joy"], created_at=_ts(600),
    )
    conn.execute(
        """INSERT INTO curve_features (candidate_id, feature_version, availability,
           features_json, extractor_sha256, artifact_sha256, wall_clock_ms,
           source_video_sha256, width, height, frame_count)
           VALUES (:cid, 'curve-v1', 'AVAILABLE', :features, 'ex', :asha, 900.0,
                   :source, 720, 405, 24)""",
        {
            "cid": candidate_id,
            "features": json.dumps(features),
            "asha": hashlib.sha256(gif_content).hexdigest(),
            "source": source,
        },
    )
    if with_vlm:
        conn.execute(
            "UPDATE candidate_gifs SET vlm_summary_json = :summary WHERE candidate_id = :cid",
            {
                "summary": json.dumps(
                    {"caption": "c", "gif_worthiness": 74, "sex_act": 63}
                ),
                "cid": candidate_id,
            },
        )
    if with_vector:
        _insert_vector(conn, candidate_id=candidate_id, seed=7)

    manifest_row = {
        "candidate_id": candidate_id,
        "rating": rating,
        "features": features,
        "baseline": baseline,
        "artifact_sha256": hashlib.sha256(gif_content).hexdigest(),
        "event_id": event_id,
        "event_created_at": _ts(600),
        "source": source,
    }
    return manifest_row, gif


def test_build_inputs_happy_path_binds_every_field(tmp_path):
    conn = build_qualification_db()
    manifest_row, _gif = _attach_holdout_row(conn, tmp_path)

    inputs = build_qualification_inputs(conn, manifest_rows=[manifest_row])

    assert inputs.blocking == []
    assert len(inputs.records) == 1
    record = inputs.records[0]
    assert record["candidate_id"] == "cand-hold-0"
    assert record["rating"] == "like"
    assert record["event_id"] == "ev-hold-0"
    assert record["artifact_sha256"] == manifest_row["artifact_sha256"]
    assert record["curve_features"] == {"extension_mean": 0.5}
    assert record["curve_features_sha256"] == hashlib.sha256(
        json.dumps({"extension_mean": 0.5}, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert record["vlm_evidence"] == {"gif_worthiness": 74, "sex_act": 63}
    assert record["vlm_evidence_sha256"]
    expected_base = 0.80 * (0.40 * 0.74 + 0.60 * 0.63) + 0.20 * 0.5
    assert record["base_export_score"] == pytest.approx(expected_base)
    assert record["candidate_vector_sha256"]
    assert record["profile_fallback_reason"] is None
    assert inputs.cinematic_fallback_count == 1
    assert inputs.enrichment_count == 0
    # Deterministic manifest hash over sorted records.
    assert inputs.input_manifest_sha256 == build_qualification_inputs(
        conn, manifest_rows=[manifest_row]
    ).input_manifest_sha256


def test_build_inputs_blocks_on_missing_and_hash_mismatched_artifact(tmp_path):
    conn = build_qualification_db()
    row_ok, gif = _attach_holdout_row(
        conn, tmp_path, candidate_id="cand-ok", event_id="ev-hold-ok"
    )
    row_missing, _ = _attach_holdout_row(
        conn, tmp_path, candidate_id="cand-gone", gif_content=b"GIF89a-gone",
        event_id="ev-hold-gone",
    )
    (tmp_path / "cand-gone.gif").unlink()
    row_mismatch, _ = _attach_holdout_row(
        conn, tmp_path, candidate_id="cand-mismatch", gif_content=b"GIF89a-mismatch",
        event_id="ev-hold-mismatch",
    )
    row_mismatch = dict(row_mismatch, artifact_sha256="f" * 64)

    inputs = build_qualification_inputs(
        conn, manifest_rows=[row_ok, row_missing, row_mismatch]
    )

    codes = {b["code"] for b in inputs.blocking}
    assert codes == {"artifact_missing", "artifact_hash_mismatch"}
    assert {r["candidate_id"] for r in inputs.records} == {"cand-ok"}


def test_build_inputs_reports_identity_and_feature_mismatch(tmp_path):
    conn = build_qualification_db()
    row_ok, _ = _attach_holdout_row(
        conn, tmp_path, candidate_id="cand-ok", event_id="ev-hold-ok"
    )
    row_source, _ = _attach_holdout_row(
        conn, tmp_path, candidate_id="cand-src", source=HOLDOUT_SOURCES[0],
        event_id="ev-hold-src",
    )
    row_source = dict(row_source, source="different-source")
    row_features, _ = _attach_holdout_row(
        conn, tmp_path, candidate_id="cand-feat", features={"extension_mean": 0.9},
        event_id="ev-hold-feat",
    )
    row_features = dict(row_features, features={"extension_mean": 0.1})

    inputs = build_qualification_inputs(
        conn, manifest_rows=[row_ok, row_source, row_features]
    )

    codes = {b["code"] for b in inputs.blocking}
    assert codes == {"candidate_identity_mismatch"}


def test_build_inputs_blocks_when_effective_event_changed(tmp_path):
    conn = build_qualification_db()
    manifest_row, _ = _attach_holdout_row(conn, tmp_path, candidate_id="cand-ev")
    _insert_event(
        conn, event_id="ev-correction", target_id="cand-ev", rating="dislike",
        source=HOLDOUT_SOURCES[0], scenario_keys=["emotion:joy"],
        created_at=_ts(900), event_kind="correction", supersedes="ev-hold-0",
    )

    inputs = build_qualification_inputs(conn, manifest_rows=[manifest_row])

    codes = {b["code"] for b in inputs.blocking}
    assert codes == {"effective_event_changed"}
    assert inputs.post_manifest_event_changes == ["cand-ev"]


def test_build_inputs_enriches_missing_vlm_into_work_db_only(tmp_path):
    conn = build_qualification_db()
    manifest_row, gif = _attach_holdout_row(
        conn, tmp_path, candidate_id="cand-novlm", with_vlm=False, with_vector=False
    )

    def fake_enrich(path: str) -> str:
        assert path == str(gif)
        return json.dumps(
            {"caption": "enriched", "gif_worthiness": 55, "sex_act": 30}
        )

    calls: list[str] = []

    def fake_vector(candidate_id: str) -> bool:
        calls.append(candidate_id)
        conn.execute(
            """INSERT OR REPLACE INTO candidate_vectors
               (candidate_id, vector_type, embedding_model, embedding_dim,
                vector_blob, normalized)
               VALUES (?, 'clip', 'nomic-embed-text:latest', 768, ?, 1)""",
            (candidate_id, _unit_vector(11).tobytes()),
        )
        conn.commit()
        return True

    before_changes = conn.total_changes
    inputs = build_qualification_inputs(
        conn, manifest_rows=[manifest_row], enrich_fn=fake_enrich,
        vector_fn=fake_vector,
    )
    assert conn.total_changes > before_changes  # work.db was written

    assert inputs.blocking == []
    assert inputs.enrichment_count == 1
    assert inputs.records[0]["vlm_evidence"] == {"gif_worthiness": 55, "sex_act": 30}
    assert calls == ["cand-novlm"]
    stored = conn.execute(
        "SELECT vlm_summary_json FROM candidate_gifs WHERE candidate_id='cand-novlm'"
    ).fetchone()[0]
    assert json.loads(stored)["gif_worthiness"] == 55


def test_build_inputs_blocks_when_enrichment_fails(tmp_path):
    conn = build_qualification_db()
    manifest_row, _ = _attach_holdout_row(
        conn, tmp_path, candidate_id="cand-badvlm", with_vlm=False
    )

    def failing_enrich(path: str) -> str:
        raise RuntimeError("vlm unavailable")

    inputs = build_qualification_inputs(
        conn, manifest_rows=[manifest_row], enrich_fn=failing_enrich,
    )

    codes = {b["code"] for b in inputs.blocking}
    assert codes == {"base_score_coverage_incomplete"}
    assert inputs.records == []


def test_build_inputs_blocks_when_vector_unavailable(tmp_path):
    conn = build_qualification_db()
    manifest_row, _ = _attach_holdout_row(
        conn, tmp_path, candidate_id="cand-novec", with_vector=False
    )

    def failing_vector(candidate_id: str) -> bool:
        return False

    inputs = build_qualification_inputs(
        conn, manifest_rows=[manifest_row],
        vector_fn=failing_vector,
    )

    codes = {b["code"] for b in inputs.blocking}
    assert codes == {"base_score_coverage_incomplete"}


def test_build_inputs_records_sorted_and_never_touch_source(tmp_path):
    conn = build_qualification_db()
    row_b, _ = _attach_holdout_row(
        conn, tmp_path, candidate_id="cand-b", gif_content=b"GIF89a-b",
        event_id="ev-hold-b",
    )
    row_a, _ = _attach_holdout_row(
        conn, tmp_path, candidate_id="cand-a", gif_content=b"GIF89a-a",
        event_id="ev-hold-a",
    )

    inputs = build_qualification_inputs(conn, manifest_rows=[row_b, row_a])

    ids = [r["candidate_id"] for r in inputs.records]
    assert ids == sorted(ids)
    assert inputs.input_manifest_sha256 != build_qualification_inputs(
        conn, manifest_rows=[row_a]
    ).input_manifest_sha256
