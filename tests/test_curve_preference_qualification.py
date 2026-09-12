"""Curve preference qualification: source-isolated profile snapshots (Task 3),
evidence freezing (Task 4), paired metrics/gates (Task 5) and run bundles
(Task 6)."""

from __future__ import annotations

import base64
import hashlib
import json
import math
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
        "sex_act": 80,
        "gif_worthiness": 65,
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
    assert summary["sex_act"] == 80
    assert summary["gif_worthiness"] == 65
    assert summary["enriched_by"] == "test-model"
    assert summary["enriched_at"] == "2026-09-05T00:00:00+00:00"


def test_parse_enrichment_response_accepts_fenced_json():
    fenced = "```json\n" + VALID_VLM_JSON + "\n```"

    summary = parse_enrichment_response(
        fenced, model="m", enriched_at="t"
    )

    assert summary["caption"] == "a test clip"


def test_parse_score_semantics_and_rejections():
    from app.services.curve_preference_qualification import _parse_score

    # Integer vs float 1.0 semantics
    assert _parse_score(1, "test") == 1
    assert _parse_score(1.0, "test") == 100
    assert _parse_score(0, "test") == 0
    assert _parse_score(0.0, "test") == 0
    assert _parse_score(85, "test") == 85
    assert _parse_score(0.85, "test") == 85
    assert _parse_score(100, "test") == 100

    # Strict rejection of invalid inputs
    for invalid in ("60", "1", 120, -5, 1.1, 101, True, False, None, float("nan"), float("inf"), float("-inf")):
        with pytest.raises(EnrichmentError):
            _parse_score(invalid, "test")


def test_parse_enrichment_response_rejects_refusal_missing_caption_and_invalid():
    for raw in (
        "I cannot help with that request.",
        json.dumps({"emotional_core": "joy"}),
        "not a json object at all",
        json.dumps({"caption": "c", "gif_worthiness": 120, "sex_act": 50}),
        json.dumps({"caption": "c", "gif_worthiness": -5, "sex_act": 50}),
        json.dumps({"caption": "c", "gif_worthiness": "80", "sex_act": 50}),
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
    assert score_evidence_from_summary({"gif_worthiness": 120, "sex_act": 50}) is None


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

    def fake_vector(vector_conn: sqlite3.Connection, candidate_id: str) -> bool:
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

    def failing_vector(vector_conn: sqlite3.Connection, candidate_id: str) -> bool:
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


def test_two_pass_protocol_produces_score_sets_and_confidence(tmp_path):
    from app.services.curve_preference import CurvePreferenceModel
    from app.services.curve_preference_qualification import (
        run_two_pass_qualification_scores,
    )
    from app.services.reranker import PreferenceReranker

    conn = build_qualification_db()
    manifest_rows = []
    for index in range(6):
        positive = index % 2 == 0
        rating = "like" if positive else "dislike"
        features = {"extension_mean": 0.9 if positive else 0.1}
        row, _gif = _attach_holdout_row(
            conn, tmp_path,
            candidate_id=f"cand-2p-{index}",
            source=HOLDOUT_SOURCES[0] if index < 3 else "holdout-src-2",
            rating=rating,
            event_id=f"ev-2p-{index}",
            gif_content=b"GIF89a-2p-" + str(index).encode(),
            features=features,
        )
        manifest_rows.append(row)

    model = CurvePreferenceModel(
        model_version="m-2p",
        feature_version="curve-v1",
        extractor_sha256="ex",
        feature_names=("extension_mean",),
        mean=np.array([0.5]),
        std=np.array([0.2]),
        coefficients=np.array([1.0]),
        intercept=0.0,
        training_manifest_sha256="t",
        holdout_manifest_sha256="h",
        training_source_set_sha256="t2",
        holdout_source_set_sha256="h2",
        training_manifest={"rows": [{"candidate_id": "x"}] * 250},
        holdout_manifest={"rows": []},
    )
    conn.execute(
        "INSERT INTO preference_profile_builds ("
        "profile_version, event_watermark, embedding_model, embedding_dim, "
        "effective_feedback_count, source_video_count, config_json, status"
        ") VALUES ('prof-2p','wm','nomic-embed-text:latest',768,10,2,'{}','completed')"
    )
    liked = _unit_vector(3)
    conn.execute(
        "INSERT INTO preference_profiles ("
        "profile_version, scope, scenario_key, like_count, dislike_count, "
        "neutral_count, confidence, liked_centroid_blob, disliked_centroid_blob, "
        "tag_weights_json) VALUES ("
        "'prof-2p','global',NULL,10,0,0,0.9,?,NULL,'{}')",
        (liked.tobytes(),),
    )
    conn.commit()

    inputs = build_qualification_inputs(conn, manifest_rows=manifest_rows)
    assert inputs.blocking == []
    two_pass = run_two_pass_qualification_scores(
        work_conn=conn,
        model=model,
        records=inputs.records,
        profile_version="prof-2p",
        reranker=PreferenceReranker(conn),
    )

    assert set(two_pass.baseline) == {f"cand-2p-{i}" for i in range(6)}
    assert set(two_pass.nominal) == set(two_pass.baseline)
    assert set(two_pass.effective) == set(two_pass.baseline)
    assert 0.0 <= two_pass.effective_confidence <= 1.0
    assert two_pass.nominal_comparison.pair_count == 4
    # The curve feature is perfectly label-aligned so the nominal variant
    # must beat chance within source groups.
    assert two_pass.nominal_comparison.proposed_pairwise_accuracy > 0.5
    assert all(math.isfinite(v) for v in two_pass.effective.values())


# ---------------------------------------------------------------------------
# Task 5: paired source-grouped metrics, two-pass confidence, gates
# ---------------------------------------------------------------------------

from app.services.curve_preference_qualification import (  # noqa: E402
    GroupedScoreRow,
    _open_read_only,
    decide_curve_qualification,
    evaluate_source_grouped_scores,
    execute_qualification_run,
    logical_source_identity,
)


def _grouped_rows(
    *,
    sources: list[str],
    ratings: list[str],
    baseline: list[float],
    proposed: list[float],
) -> list[GroupedScoreRow]:
    return [
        GroupedScoreRow(
            candidate_id=f"cand-{index}",
            source=source,
            rating=rating,
            baseline=base,
            proposed=proposed,
        )
        for index, (source, rating, base, proposed) in enumerate(
            zip(sources, ratings, baseline, proposed)
        )
    ]


def test_grouped_metrics_perfect_curve_over_bad_baseline_qualifies():
    rows = _grouped_rows(
        sources=["s1", "s1", "s2", "s2"],
        ratings=["like", "dislike", "like", "dislike"],
        baseline=[0.0, 1.0, 0.0, 1.0],
        proposed=[1.0, 0.0, 1.0, 0.0],
    )

    comparison = evaluate_source_grouped_scores(
        rows, baseline_field="baseline", proposed_field="proposed",
        bootstrap_seed=20260905, bootstrap_reps=200,
    )

    assert comparison.baseline_pairwise_accuracy == 0.0
    assert comparison.proposed_pairwise_accuracy == 1.0
    assert comparison.pairwise_delta == 1.0
    assert comparison.pairwise_delta_lcb95 == 1.0
    assert comparison.tie_rate == 0.0
    assert comparison.pair_count == 2
    assert comparison.source_pair_groups == 2
    assert comparison.non_finite_count == 0
    assert comparison.ndcg_at_20_delta > 0

def test_grouped_metrics_bad_curve_fails():
    rows = _grouped_rows(
        sources=["s1", "s1", "s2", "s2"],
        ratings=["like", "dislike", "like", "dislike"],
        baseline=[1.0, 0.0, 1.0, 0.0],
        proposed=[0.0, 1.0, 0.0, 1.0],
    )

    comparison = evaluate_source_grouped_scores(
        rows, baseline_field="baseline", proposed_field="proposed",
        bootstrap_seed=20260905, bootstrap_reps=200,
    )

    assert comparison.proposed_pairwise_accuracy == 0.0
    assert comparison.pairwise_delta_lcb95 < 0


def test_grouped_metrics_ties_reported_separately_and_never_win():
    rows = _grouped_rows(
        sources=["s1", "s1"],
        ratings=["like", "dislike"],
        baseline=[0.5, 0.5],
        proposed=[0.5, 0.5],
    )

    comparison = evaluate_source_grouped_scores(
        rows, baseline_field="baseline", proposed_field="proposed",
        bootstrap_seed=20260905, bootstrap_reps=50,
    )

    assert comparison.tie_rate == 1.0
    assert comparison.proposed_pairwise_accuracy == 0.0
    assert comparison.pair_count == 1


def test_grouped_metrics_ordering_invariant_and_non_finite_counted():
    rows = _grouped_rows(
        sources=["s1", "s1", "s2"],
        ratings=["like", "dislike", "like"],
        baseline=[0.2, 0.8, float("nan")],
        proposed=[0.8, 0.2, 0.5],
    )
    shuffled = list(reversed(rows))

    first = evaluate_source_grouped_scores(
        rows, baseline_field="baseline", proposed_field="proposed",
        bootstrap_seed=7, bootstrap_reps=100,
    )
    second = evaluate_source_grouped_scores(
        shuffled, baseline_field="baseline", proposed_field="proposed",
        bootstrap_seed=7, bootstrap_reps=100,
    )

    assert first.pairwise_delta == second.pairwise_delta
    assert first.pairwise_delta_lcb95 == second.pairwise_delta_lcb95
    assert first.non_finite_count == 1
    assert first.comparable_row_count == 2


def _decide_inputs(**overrides) -> dict:
    base = dict(
        model_status="DRAFT",
        model_manifest_hashes_ok=True,
        source_overlap=0,
        holdout_row_count=2510,
        train_class_counts=(7154, 1093),
        holdout_class_counts=(2149, 361),
        blocking=[],
        nominal_pair_count=12938,
        nominal_pairwise_delta_lcb95=0.05,
        effective_pairwise_delta_lcb95=0.03,
        effective_ndcg_delta=0.001,
        non_finite_count=0,
    )
    base.update(overrides)
    return base


def test_decide_qualifies_good_curve():
    decision = decide_curve_qualification(**_decide_inputs())

    assert decision.status == "QUALIFIED"
    assert decision.reasons == []


def test_decide_fails_bad_curve_against_good_baseline():
    decision = decide_curve_qualification(
        **_decide_inputs(
            nominal_pairwise_delta_lcb95=-0.1,
            effective_pairwise_delta_lcb95=-0.1,
            effective_ndcg_delta=-0.01,
        )
    )

    assert decision.status == "NOT_QUALIFIED"
    assert "nominal_pairwise_lcb_not_positive" in decision.reasons
    assert "effective_pairwise_lcb_not_positive" in decision.reasons
    assert "effective_ndcg_regression" in decision.reasons


def test_decide_source_overlap_blocks_even_with_excellent_delta():
    decision = decide_curve_qualification(**_decide_inputs(source_overlap=3))

    assert decision.status == "NOT_QUALIFIED"
    assert "source_overlap" in decision.reasons
    assert "nominal_pairwise_lcb_not_positive" not in decision.reasons


def test_decide_high_point_estimate_with_lcb_at_or_below_zero_fails():
    at_zero = decide_curve_qualification(
        **_decide_inputs(nominal_pairwise_delta_lcb95=0.0,
                         effective_pairwise_delta_lcb95=0.0)
    )
    below = decide_curve_qualification(
        **_decide_inputs(nominal_pairwise_delta_lcb95=-0.02,
                         effective_pairwise_delta_lcb95=-0.02)
    )

    assert at_zero.status == "NOT_QUALIFIED"
    assert "nominal_pairwise_lcb_not_positive" in at_zero.reasons
    assert below.status == "NOT_QUALIFIED"


def test_decide_nominal_pass_effective_fail():
    decision = decide_curve_qualification(
        **_decide_inputs(
            nominal_pairwise_delta_lcb95=0.05,
            effective_pairwise_delta_lcb95=-0.01,
        )
    )

    assert decision.status == "NOT_QUALIFIED"
    assert "nominal_pairwise_lcb_not_positive" not in decision.reasons
    assert "effective_pairwise_lcb_not_positive" in decision.reasons


def test_decide_effective_pairwise_pass_with_ndcg_regression_fails():
    decision = decide_curve_qualification(
        **_decide_inputs(effective_ndcg_delta=-0.005)
    )

    assert decision.status == "NOT_QUALIFIED"
    assert "effective_ndcg_regression" in decision.reasons


def test_decide_collects_all_precondition_failures():
    decision = decide_curve_qualification(
        **_decide_inputs(
            model_status="PUBLISHED",
            model_manifest_hashes_ok=False,
            source_overlap=2,
            holdout_row_count=10,
            train_class_counts=(0, 100),
            holdout_class_counts=(0, 10),
            blocking=[
                {"code": "artifact_missing", "candidate_id": "c1", "detail": ""},
                {"code": "artifact_hash_mismatch", "candidate_id": "c2", "detail": ""},
                {"code": "candidate_identity_mismatch", "candidate_id": "c3", "detail": ""},
                {"code": "effective_event_changed", "candidate_id": "c4", "detail": ""},
                {"code": "base_score_coverage_incomplete", "candidate_id": "c5", "detail": ""},
            ],
        )
    )

    assert decision.status == "NOT_QUALIFIED"
    for code in (
        "model_not_draft", "model_manifest_hash_mismatch", "source_overlap",
        "holdout_too_small", "missing_class", "artifact_missing",
        "artifact_hash_mismatch", "candidate_identity_mismatch",
        "effective_event_changed", "base_score_coverage_incomplete",
    ):
        assert code in decision.reasons, code
    # Outcome metrics are not even evaluated.
    assert "nominal_pairwise_lcb_not_positive" not in decision.reasons


def test_decide_non_finite_and_missing_pairs_fail():
    no_pairs = decide_curve_qualification(**_decide_inputs(nominal_pair_count=0))
    non_finite = decide_curve_qualification(**_decide_inputs(non_finite_count=2))

    assert "no_within_source_pairs" in no_pairs.reasons
    assert no_pairs.status == "NOT_QUALIFIED"
    assert "non_finite_metric" in non_finite.reasons


# ---------------------------------------------------------------------------
# Task 6: CLI orchestration, snapshot safety, immutable bundles
# ---------------------------------------------------------------------------

from app.services.curve_preference import CurvePreferenceModel  # noqa: E402


def _file_source_db(tmp_path: Path, *, n_holdout: int = 32) -> Path:
    """Materialize the qualification fixture as a file DB plus a DRAFT model."""

    from scripts.train_curve_preference_model import build_manifest

    memory_conn = build_qualification_db()
    disk_path = tmp_path / "source.db"
    disk = sqlite3.connect(str(disk_path))
    memory_conn.backup(disk)
    disk.commit()

    gif_dir = tmp_path / "gifs"
    gif_dir.mkdir(exist_ok=True)

    holdout_rows: list[dict] = []
    for index in range(n_holdout):
        candidate_id = f"cand-q-{index:02d}"
        positive = (index // 2) % 2 == 0
        rating = "like" if positive else "dislike"
        features = {"extension_mean": 0.9 if positive else 0.1}
        gif = gif_dir / f"{candidate_id}.gif"
        gif.write_bytes(b"GIF89a-q-" + str(index).encode())
        disk.execute(
            """INSERT OR REPLACE INTO candidate_gifs
               (candidate_id, source_run_id, source_run_candidate_id,
                source_video_sha256, source_video_path, start_sec, end_sec,
                artifact_path, status, tags_json, scenario_keys_json,
                vlm_summary_json)
               VALUES (:cid, 'run-q', :rcid, :source, :svp, 0.0, 5.0,
                       :artifact, 'candidate', '[]', '[]', :summary)""",
            {
                "cid": candidate_id,
                "rcid": f"rc-{candidate_id}",
                "source": f"holdout-src-{index % 2}",
                "svp": f"/v/holdout-{index % 2}.mp4",
                "artifact": str(gif),
                "summary": json.dumps(
                    {"caption": "c", "gif_worthiness": 50 + (index % 40),
                     "sex_act": 30 + (index % 40)}
                ),
            },
        )
        disk.execute(
            """INSERT INTO preference_events
               (event_id, target_type, target_id, rating, source_video_sha256,
                scenario_keys_json, created_at, event_kind)
               VALUES (:event_id, 'candidate_gif', :cid, :rating, :source,
                       '[]', :created_at, 'feedback')""",
            {
                "event_id": f"ev-q-{index:02d}",
                "cid": candidate_id,
                "rating": rating,
                "source": f"holdout-src-{index % 2}",
                "created_at": _ts(2000 + index * 60),
            },
        )
        disk.execute(
            """INSERT INTO curve_features (candidate_id, feature_version,
               availability, features_json, extractor_sha256, artifact_sha256,
               wall_clock_ms, source_video_sha256, width, height, frame_count)
               VALUES (:cid, 'curve-v1', 'AVAILABLE', :features, 'ex', :asha,
                       900.0, :source, 720, 405, 24)""",
            {
                "cid": candidate_id,
                "features": json.dumps(features),
                "asha": hashlib.sha256(gif.read_bytes()).hexdigest(),
                "source": f"holdout-src-{index % 2}",
            },
        )
        disk.execute(
            """INSERT OR REPLACE INTO candidate_vectors
               (candidate_id, vector_type, embedding_model, embedding_dim,
                vector_blob, normalized)
               VALUES (:cid, 'clip', 'nomic-embed-text:latest', 768, :blob, 1)""",
            {"cid": candidate_id, "blob": _unit_vector(30 + index).tobytes()},
        )
        holdout_rows.append(
            {
                "candidate_id": candidate_id,
                "rating": rating,
                "features": features,
                "baseline": None,
                "artifact_sha256": hashlib.sha256(
                    gif.read_bytes()
                ).hexdigest(),
                "event_id": f"ev-q-{index:02d}",
                "event_created_at": _ts(2000 + index * 60),
                "source": f"holdout-src-{index % 2}",
            }
        )

    train_rows = [
        {
            "candidate_id": f"cand-train-like-{i}",
            "rating": "like",
            "features": {"extension_mean": 0.5},
            "baseline": None,
            "artifact_sha256": f"{i:064x}",
            "event_id": f"ev-train-like-{i}",
            "event_created_at": _ts(i * 60),
            "source": TRAIN_SOURCES[i % 3],
        }
        for i in range(24)
    ] + [
        {
            "candidate_id": f"cand-train-dislike-{i}",
            "rating": "dislike",
            "features": {"extension_mean": 0.2},
            "baseline": None,
            "artifact_sha256": f"{i + 100:064x}",
            "event_id": f"ev-train-dislike-{i}",
            "event_created_at": _ts(1000 + i * 60),
            "source": TRAIN_SOURCES[i % 3],
        }
        for i in range(12)
    ]
    train_sources = sorted(set(TRAIN_SOURCES))
    holdout_sources = sorted({row["source"] for row in holdout_rows})
    train_manifest, train_sha = build_manifest(
        "curve-training-manifest-v1", train_rows, set(train_sources),
        ["extension_mean"], "ex",
    )
    holdout_manifest, holdout_sha = build_manifest(
        "curve-holdout-manifest-v1", holdout_rows, set(holdout_sources),
        ["extension_mean"], "ex",
    )
    disk.execute(
        """
        INSERT INTO curve_preference_models (
            model_version, feature_version, extractor_sha256, status,
            coefficients_json, intercept, feature_mean_json, feature_std_json,
            feature_names_json, l2_lambda, training_event_watermark,
            training_source_set_sha256, holdout_source_set_sha256,
            training_manifest_sha256, holdout_manifest_sha256,
            training_manifest_json, holdout_manifest_json, holdout_row_count,
            metrics_json
        ) VALUES (
            'm-qual', 'curve-v1', 'ex', 'DRAFT',
            :coefficients, 0.0, :mean, :std, '["extension_mean"]', 1.0,
            'wm', :train_sources, :holdout_sources,
            :train_sha, :holdout_sha, :train_manifest, :holdout_manifest,
            :holdout_count, '{}'
        )
        """,
        {
            "coefficients": json.dumps({"extension_mean": 1.0}),
            "mean": json.dumps({"extension_mean": 0.5}),
            "std": json.dumps({"extension_mean": 0.2}),
            "train_sources": hashlib.sha256(
                json.dumps(train_sources, separators=(",", ":")).encode()
            ).hexdigest(),
            "holdout_sources": hashlib.sha256(
                json.dumps(holdout_sources, separators=(",", ":")).encode()
            ).hexdigest(),
            "train_sha": train_sha,
            "holdout_sha": holdout_sha,
            "train_manifest": json.dumps(train_manifest, sort_keys=True),
            "holdout_manifest": json.dumps(holdout_manifest, sort_keys=True),
            "holdout_count": len(holdout_rows),
        },
    )
    disk.commit()
    disk.close()
    return disk_path


def _good_enrich(path: str) -> str:
    return json.dumps(
        {"caption": "enriched clip", "gif_worthiness": 65, "sex_act": 35}
    )


def test_dry_run_writes_no_rows_and_no_run_dir(tmp_path):
    db_path = _file_source_db(tmp_path)

    summary = execute_qualification_run(
        db_path=db_path,
        model_version="m-qual",
        work_dir=tmp_path / "runs",
        dry_run=True,
    )

    assert summary["dry_run"] is True
    assert summary["model_ok"] is True
    assert summary["holdout_rows"] == 32
    assert summary["required_enrichments"] == 0
    assert not (tmp_path / "runs").exists()
    conn = sqlite3.connect(str(db_path))
    count = conn.execute(
        "SELECT COUNT(*) FROM curve_preference_qualification_runs"
    ).fetchone()[0]
    conn.close()
    assert count == 0


def test_full_run_writes_bundle_and_source_stays_unmutated(tmp_path):
    db_path = _file_source_db(tmp_path)

    before_identity = logical_source_identity(
        _open_read_only(db_path), "m-qual"
    )
    source_conn = sqlite3.connect(str(db_path))
    source_conn.row_factory = sqlite3.Row
    before_total_changes = source_conn.total_changes
    before_counts = {
        table: source_conn.execute(
            f"SELECT COUNT(*) FROM {table}"
        ).fetchone()[0]
        for table in ("candidate_gifs", "preference_events", "curve_features")
    }

    result = execute_qualification_run(
        db_path=db_path,
        model_version="m-qual",
        work_dir=tmp_path / "runs",
        qualification_id="qual-test-1",
    )

    after_identity = logical_source_identity(
        _open_read_only(db_path), "m-qual"
    )
    after_counts = {
        table: source_conn.execute(
            f"SELECT COUNT(*) FROM {table}"
        ).fetchone()[0]
        for table in ("candidate_gifs", "preference_events", "curve_features")
    }
    assert source_conn.total_changes == before_total_changes
    source_conn.close()

    assert before_identity == after_identity
    assert before_counts == after_counts
    assert result["status"] == "QUALIFIED"
    run_dir = Path(result["run_dir"])
    for name in (
        "run_state.json", "input_manifest.json", "profile_snapshot.json",
        "evidence.jsonl", "baseline_scores.jsonl", "nominal_scores.jsonl",
        "effective_scores.jsonl", "metrics.json", "report.md", "work.db",
    ):
        assert (run_dir / name).is_file(), name
    state = json.loads((run_dir / "run_state.json").read_text(encoding="utf-8"))
    assert state["status"] == "COMPLETED"
    metrics = json.loads((run_dir / "metrics.json").read_text(encoding="utf-8"))
    assert metrics["status"] == "QUALIFIED"
    assert metrics["enrichment_count"] == 0
    assert metrics["cinematic_fallback_count"] == 32


def test_interrupted_run_resumes_without_re_enrichment(tmp_path, monkeypatch):
    db_path = _file_source_db(tmp_path)
    conn = sqlite3.connect(str(db_path))
    conn.execute("UPDATE candidate_gifs SET vlm_summary_json = ''")
    conn.commit()
    conn.close()

    calls: list[str] = []

    def counting_enrich(path: str) -> str:
        calls.append(path)
        return _good_enrich(path)

    run_dir = tmp_path / "runs" / "qual-resume"

    import app.services.curve_preference_qualification as qual_module

    real_two_pass = qual_module.run_two_pass_qualification_scores
    calls_to_two_pass = {"n": 0}

    def exploding_two_pass(**kwargs):
        calls_to_two_pass["n"] += 1
        raise RuntimeError("simulated crash after enrichment")

    monkeypatch.setattr(
        qual_module, "run_two_pass_qualification_scores", exploding_two_pass
    )
    with pytest.raises(RuntimeError, match="simulated crash"):
        execute_qualification_run(
            db_path=db_path,
            model_version="m-qual",
            work_dir=tmp_path / "runs",
            enrich_fn=counting_enrich,
            vector_fn=lambda _conn, _cid: True,
            qualification_id="qual-resume",
        )
    state = json.loads(
        (run_dir / "run_state.json").read_text(encoding="utf-8")
    )
    assert state["status"] == "INCOMPLETE"
    assert len(calls) == 32  # every candidate enriched exactly once

    monkeypatch.setattr(
        qual_module, "run_two_pass_qualification_scores", real_two_pass
    )
    result = execute_qualification_run(
        db_path=db_path,
        model_version="m-qual",
        work_dir=tmp_path / "runs",
        enrich_fn=counting_enrich,
        vector_fn=lambda _conn, _cid: True,
        qualification_id="qual-resume",
    )

    # Resume must not re-call the VLM: all 32 summaries persist in work.db.
    assert result["status"] == "QUALIFIED"
    assert len(calls) == 32
    metrics = json.loads(
        (Path(result["run_dir"]) / "metrics.json").read_text(encoding="utf-8")
    )
    assert metrics["enrichment_count"] == 0


def test_resume_refused_when_source_identity_changed(tmp_path):
    db_path = _file_source_db(tmp_path)
    run_dir = tmp_path / "runs" / "qual-changed"
    run_dir.mkdir(parents=True)
    logical_source_identity(_open_read_only(db_path), "m-qual")
    (run_dir / "run_state.json").write_text(
        json.dumps(
            {
                "qualification_id": "qual-changed",
                "status": "INCOMPLETE",
                "model_version": "m-qual",
                "source_logical_identity": "stale-identity",
                "source_db_snapshot_sha256": "x",
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="resume refused"):
        execute_qualification_run(
            db_path=db_path,
            model_version="m-qual",
            work_dir=tmp_path / "runs",
            qualification_id="qual-changed",
        )


def test_two_complete_runs_reproduce_stable_content_hashes(tmp_path):
    db_path = _file_source_db(tmp_path)

    first = execute_qualification_run(
        db_path=db_path, model_version="m-qual",
        work_dir=tmp_path / "runs-a", qualification_id="q-a",
    )
    second = execute_qualification_run(
        db_path=db_path, model_version="m-qual",
        work_dir=tmp_path / "runs-b", qualification_id="q-b",
    )

    first_metrics = json.loads(
        (Path(first["run_dir"]) / "metrics.json").read_text(encoding="utf-8")
    )
    second_metrics = json.loads(
        (Path(second["run_dir"]) / "metrics.json").read_text(encoding="utf-8")
    )
    for key in ("nominal", "effective", "gate_reasons", "status",
                "enrichment_count", "cinematic_fallback_count"):
        assert first_metrics[key] == second_metrics[key]
    first_input = json.loads(
        (Path(first["run_dir"]) / "input_manifest.json").read_text(encoding="utf-8")
    )
    second_input = json.loads(
        (Path(second["run_dir"]) / "input_manifest.json").read_text(encoding="utf-8")
    )
    first_input.pop("qualification_id")
    second_input.pop("qualification_id")
    assert first_input == second_input


def test_not_qualified_run_preserves_full_bundle(tmp_path):
    db_path = _file_source_db(tmp_path)
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "UPDATE candidate_gifs SET artifact_path = :path "
        "WHERE candidate_id = 'cand-q-00'",
        {"path": str(tmp_path / "gone.gif")},
    )
    conn.commit()
    conn.close()

    result = execute_qualification_run(
        db_path=db_path, model_version="m-qual",
        work_dir=tmp_path / "runs", qualification_id="q-nq",
    )

    assert result["status"] == "NOT_QUALIFIED"
    run_dir = Path(result["run_dir"])
    for name in (
        "run_state.json", "input_manifest.json", "profile_snapshot.json",
        "evidence.jsonl", "metrics.json", "report.md", "work.db",
    ):
        assert (run_dir / name).is_file(), name
    metrics = json.loads((run_dir / "metrics.json").read_text(encoding="utf-8"))
    assert any(
        b["code"] == "artifact_missing" for b in metrics["blocking"]
    )


def test_ndcg_at_20_with_more_than_20_candidates_is_bounded_by_one():
    from app.services.curve_preference_qualification import (
        GroupedScoreRow,
        _ndcg_at_20_for_field,
    )

    # 40 candidates in a single source video
    rows = [
        GroupedScoreRow(
            candidate_id=f"c-{i}",
            source="src-large",
            rating="like" if i % 2 == 0 else "dislike",
            baseline=float(i),
            proposed=float(i * 2),
        )
        for i in range(40)
    ]
    ndcg_val = _ndcg_at_20_for_field(rows, "proposed")
    assert math.isfinite(ndcg_val)
    assert 0.0 <= ndcg_val <= 1.0


def test_qualification_binds_target_ranking_config(tmp_path):
    config_file = tmp_path / "models.yaml"
    config_file.write_text(
        """preference_memory:
  base_score_weight: 0.70
  preference_score_weight: 0.30
""",
        encoding="utf-8",
    )

    db_path = _file_source_db(tmp_path)
    result = execute_qualification_run(
        db_path=db_path,
        model_version="m-qual",
        work_dir=tmp_path / "runs",
        config_path=config_file,
        qualification_id="q-cfg-bind",
    )
    assert result["status"] == "QUALIFIED"
    metrics = result["metrics"]
    target_cfg = metrics.get("target_ranking_config") or {}
    assert target_cfg.get("base_score_weight") == 0.70
    assert target_cfg.get("preference_score_weight") == 0.30
    assert target_cfg.get("config_sha256") is not None


# ---------------------------------------------------------------------------
# Task 4 (2026-09-11 remediation): online vs qualification replay parity
# ---------------------------------------------------------------------------


def test_two_pass_replay_matches_online_curve_scoring(tmp_path):
    """Task 4 Step 5: for the same 100 frozen records the online
    baseline/nominal/effective numbers equal the qualification replay
    within 1e-12 -- one shared scorer, one scale."""
    import numpy as np
    from types import SimpleNamespace

    from app.services.curve_online import score_curve_candidate
    from app.services.curve_preference import (
        CurvePreferenceModel,
        curve_probability,
    )
    from app.services.reranker import (
        PreferenceReranker,
        clip_scenario_keys,
        preference_embedding_text,
    )
    from app.services.curve_preference_qualification import (
        BASE_SCORE_WEIGHT,
        PREFERENCE_SCORE_WEIGHT,
        run_two_pass_qualification_scores,
        score_evidence_from_summary,
    )

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_preference_schema(conn)

    # Profile signal: likes and dislikes across a few sources.
    for i in range(30):
        cid = f"profile-c-{i:02d}"
        _insert_candidate(
            conn, candidate_id=cid, source=f"profile-src-{i % 3}",
            tags=["tag-a"], scenario_keys=[],
        )
        _insert_event(
            conn, event_id=f"profile-ev-{i:02d}", target_id=cid,
            rating="like" if i % 2 == 0 else "dislike",
            source=f"profile-src-{i % 3}", scenario_keys=[],
            created_at=_ts(i),
        )
        _insert_vector(conn, candidate_id=cid, seed=400 + i)

    snapshot = _snapshot(conn)
    profile_version = snapshot.profile_version

    # 100 replay candidates with stored summaries, tags, and vectors.
    records = []
    for i in range(100):
        cid = f"replay-c-{i:03d}"
        summary = {
            "caption": f"replay clip {i}",
            "emotional_core": "joy" if i % 2 else "calm",
            "gif_worthiness": 50 + (i % 30),
            "sex_act": 20 + (i % 20),
        }
        tags = [f"tag-{i % 5}"]
        conn.execute(
            """INSERT OR REPLACE INTO candidate_gifs
               (candidate_id, source_run_id, source_run_candidate_id,
                source_video_sha256, source_video_path, start_sec, end_sec,
                artifact_path, status, tags_json, scenario_keys_json,
                vlm_summary_json)
               VALUES (:cid, 'run-replay', :rcid, :source, :svp, 0.0, 5.0,
                       NULL, 'candidate', :tags, '[]', :summary)""",
            {
                "cid": cid,
                "rcid": f"rc-{cid}",
                "source": f"replay-src-{i % 4}",
                "svp": f"/v/replay-{i % 4}.mp4",
                "tags": json.dumps(tags),
                "summary": json.dumps(summary),
            },
        )
        blob = _unit_vector(700 + i).tobytes()
        conn.execute(
            """INSERT OR REPLACE INTO candidate_vectors
               (candidate_id, vector_type, embedding_model, embedding_dim,
                vector_blob, normalized)
               VALUES (:cid, 'clip', 'nomic-embed-text:latest', 768, :blob, 1)""",
            {"cid": cid, "blob": blob},
        )
        records.append(
            {
                "candidate_id": cid,
                "rating": "like" if i % 2 == 0 else "dislike",
                "source_video_sha256": f"replay-src-{i % 4}",
                "base_export_score": 0.40 + (i % 40) / 100.0,
                "curve_features": {"extension_mean": 0.9 if i % 2 else 0.1},
            }
        )
    conn.commit()

    feature_names = ("extension_mean",)
    model = CurvePreferenceModel(
        model_version="replay-model",
        feature_version="curve-v1",
        extractor_sha256="ex",
        feature_names=feature_names,
        mean=np.array([0.5]),
        std=np.array([0.2]),
        coefficients=np.array([1.0]),
        intercept=0.0,
        training_manifest_sha256="t" * 64,
        holdout_manifest_sha256="h" * 64,
        training_source_set_sha256="ts" * 32,
        holdout_source_set_sha256="hs" * 32,
        training_manifest={"rows": [{} for _ in range(100)], "sources": []},
        holdout_manifest={"rows": [], "sources": []},
    )

    def deterministic_embed(text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        vec = np.frombuffer(digest * 24, dtype=np.uint8)[:768].astype(np.float32)
        return (vec / 255.0).tolist()

    two_pass = run_two_pass_qualification_scores(
        work_conn=conn,
        model=model,
        records=records,
        profile_version=profile_version,
        reranker=PreferenceReranker(conn),
        base_score_weight=BASE_SCORE_WEIGHT,
        preference_score_weight=PREFERENCE_SCORE_WEIGHT,
        embed_fn=deterministic_embed,
    )

    online_snapshot = SimpleNamespace(
        mode="shadow",
        qualified=False,
        confidence=two_pass.effective_confidence,
        profile_version=profile_version,
        model_version=model.model_version,
        base_score_weight=BASE_SCORE_WEIGHT,
        preference_score_weight=PREFERENCE_SCORE_WEIGHT,
        deployment_available=True,
        compatible=True,
        quality_pass=True,
        reranker=PreferenceReranker(conn),
        inactive_reasons=(),
    )

    max_diff = 0.0
    for record in records:
        cid = str(record["candidate_id"])
        cand_row = conn.execute(
            "SELECT vlm_summary_json, tags_json FROM candidate_gifs "
            "WHERE candidate_id = :cid",
            {"cid": cid},
        ).fetchone()
        summary = json.loads(cand_row["vlm_summary_json"])
        tags = json.loads(cand_row["tags_json"] or "[]")
        emb_text = preference_embedding_text(
            caption=str(summary.get("caption") or ""),
            emotion=str(summary.get("emotional_core") or ""),
            scene_type=str(summary.get("scene_type") or ""),
        )
        vector = np.array(deterministic_embed(emb_text), dtype=np.float32)
        worthiness = float(score_evidence_from_summary(summary)["gif_worthiness"])
        scenario_keys = clip_scenario_keys(
            {
                "emotional_core": summary.get("emotional_core"),
                "scene_type": summary.get("scene_type"),
                "tags": tags,
            }
        )
        probability = curve_probability(model, dict(record["curve_features"]))

        online = score_curve_candidate(
            conn,
            online_snapshot,
            {
                "base_score": float(record["base_export_score"]),
                "gif_worthiness": worthiness,
                "candidate_vector": vector,
                "scenario_keys": scenario_keys,
                "curve_probability": probability,
            },
        )
        max_diff = max(
            max_diff,
            abs(online["baseline_score"] - two_pass.baseline[cid]),
            abs(online["nominal_score"] - two_pass.nominal[cid]),
            abs(online["effective_score"] - two_pass.effective[cid]),
        )
    assert max_diff <= 1e-12
