#!/usr/bin/env python3
"""
Stage 2 post-VLM pipeline: LLM synthesis → FAISS index rebuild.
Runs automatically after vlm_loop.py completes, or can be run standalone.

Usage:
  uv run python scripts/pipeline_stage2.py          # run both steps
  uv run python scripts/pipeline_stage2.py --llm    # LLM synthesis only
  uv run python scripts/pipeline_stage2.py --index  # FAISS rebuild only
"""
import sys, json, uuid, time, io
from datetime import datetime, timezone

# Fix Windows GBK encoding
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

sys.path.insert(0, '.')
from app.db import init_db, get_connection
from app.config import get
from app.services.json_guard import parse_json_response
from app.services.llm_client import generate_llm_text, llm_model_name, wait_for_llm
from app.services.quality import validate_media_annotation

init_db()

LLM_MODEL = llm_model_name()
LOG_FILE = 'data/pipeline_stage2.log'
BATCH_COMMIT = 10

RUN_LLM = '--index' not in sys.argv
RUN_INDEX = '--llm' not in sys.argv


def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def stop_model(name):
    import subprocess
    subprocess.run(["wsl", "ollama", "stop", name], capture_output=True, timeout=30)


# ── LLM Synthesis ────────────────────────────────────────────────────────
if RUN_LLM:
    log("=" * 50)
    log("Stage 2a: LLM Synthesis started")

    conn = get_connection()

    rows = conn.execute('''
        SELECT DISTINCT m.media_id, m.file_path
        FROM media m
        INNER JOIN frame_annotations fa ON m.media_id = fa.media_id
        WHERE m.media_id NOT IN (SELECT media_id FROM annotations)
        ORDER BY m.media_id
    ''').fetchall()

    log(f"Media needing LLM synthesis: {len(rows)}")

    if len(rows) == 0:
        log("No media need synthesis — skipping LLM step")
    else:
        # Stop VLM, start LLM
        stop_model("llava")
        stop_model("nomic-embed-text")
        time.sleep(5)
        if not wait_for_llm(timeout_s=180):
            log("ERROR: LLM not responding")
            sys.exit(1)

        processed = 0
        for idx, (mid, fpath) in enumerate(rows):
            fas = conn.execute('''
                SELECT caption, emotional_core, aesthetic_notes_json, why_i_like_it
                FROM frame_annotations WHERE media_id=? ORDER BY annotation_id
            ''', (mid,)).fetchall()

            if not fas:
                continue

            analyses = '\n'.join(
                f"Frame {i+1}: caption={fa['caption']}, emotion={fa['emotional_core']}, "
                f"aesthetic={fa['aesthetic_notes_json']}, why={fa['why_i_like_it']}"
                for i, fa in enumerate(fas[:8])
            )

            prompt = (
                'Synthesize frame analyses into one media annotation. Output ONLY valid JSON:\n'
                '{\n  "summary": "one sentence describing visual style",\n'
                '  "emotional_core": "one dominant emotion",\n'
                '  "aesthetic_notes": ["2-4 cinematographic qualities"],\n'
                '  "why_i_like_it": "one cinephile reason",\n'
                '  "tags": ["3-5 keywords"],\n'
                '  "scene_type": "close-up|dialogue|action|transition|reaction|establishing|montage|other"\n}\n\n'
                'Frame analyses:\n' + analyses
            )

            name = fpath.split('\\')[-1][:40] if fpath else '?'

            for attempt in range(3):
                try:
                    raw = generate_llm_text(prompt, temperature=0.3, timeout=120)

                    parse_result = parse_json_response(raw)
                    if not parse_result.ok:
                        if attempt < 2:
                            continue
                        break

                    parsed = parse_result.data
                    cleaned, quality_errors = validate_media_annotation(parsed)

                    ann_id = f'ann_{uuid.uuid4().hex[:12]}'
                    now = datetime.now(timezone.utc).isoformat()
                    conn.execute('''
                        INSERT INTO annotations (annotation_id, media_id, model_name, summary,
                            emotional_core, aesthetic_notes_json, why_i_like_it, tags_json, raw_json, created_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?)
                    ''', (ann_id, mid, LLM_MODEL,
                        cleaned.get('summary', ''),
                        cleaned.get('emotional_core', ''),
                        json.dumps(cleaned.get('aesthetic_notes', [])),
                        cleaned.get('why_i_like_it', ''),
                        json.dumps(cleaned.get('tags', [])),
                        json.dumps(parsed, ensure_ascii=False), now))
                    processed += 1
                    break
                except Exception as e:
                    if attempt == 2:
                        log(f"  [{idx+1}/{len(rows)}] FAILED {mid[:14]}: {e}")
                    time.sleep(3)

            if (idx + 1) % BATCH_COMMIT == 0:
                conn.commit()
                pct = (idx + 1) / len(rows) * 100
                log(f"  [{idx+1}/{len(rows)}] {pct:.1f}% — {processed} synthesized")

        conn.commit()
        annotated = conn.execute('SELECT COUNT(*) FROM annotations').fetchone()[0]
        log(f"LLM synthesis complete: {annotated} total annotations ({processed} new this run)")

# ── FAISS Index Rebuild ──────────────────────────────────────────────────
if RUN_INDEX:
    log("=" * 50)
    log("Stage 2b: FAISS index rebuild started")

    from app.services.embedding import compute_media_embedding, compute_text_summary_embedding
    from app.services.indexer import get_index, verify_index

    conn = get_connection()
    idx = get_index()

    # Re-index all annotated media that aren't already indexed
    rows = conn.execute("""
        SELECT DISTINCT m.media_id
        FROM media m
        INNER JOIN annotations a ON m.media_id = a.media_id
        WHERE m.media_id NOT IN (SELECT owner_id FROM vector_refs WHERE vector_type='media_global')
        ORDER BY m.media_id
    """).fetchall()

    log(f"Media to index: {len(rows)}")

    if len(rows) == 0:
        log("All media already indexed — skipping")
    else:
        stop_model("llava")
        indexed = 0
        failed = 0
        for i, (mid,) in enumerate(rows):
            try:
                emb = compute_media_embedding(mid)
                if emb is None:
                    emb = compute_text_summary_embedding(mid)
                if emb is not None:
                    idx.add(emb, mid, "media_global")
                    indexed += 1
                else:
                    failed += 1
            except Exception as e:
                failed += 1
                if failed <= 5:
                    log(f"  Index failed for {mid}: {e}")

            if (i + 1) % 200 == 0:
                log(f"  [{i+1}/{len(rows)}] indexed...")

        log(f"Indexed: {indexed}, failed: {failed}")

    # Verify
    result = verify_index()
    log(f"Verification: FAISS={result['faiss_ntotal']}, SQL={result['sql_vector_refs']}")
    if result['errors']:
        for e in result['errors']:
            log(f"  WARN: {e}")

    log("FAISS index rebuild complete")

# ── Optional: Preference Memory reranking ──────────────────────────────────
if get("preference_memory.enabled", False):
    log("=" * 50)
    log("Stage 2c: Preference reranking started")

    from app.services.reranker import PreferenceReranker
    from app.services.embedding import compute_text_embedding

    reranker = PreferenceReranker(get_connection())

    # Re-score candidates that have vectors and RAG similarities
    candidates = conn.execute(
        """SELECT c.candidate_id, c.scenario_keys_json, c.base_rag_similarity
           FROM candidate_gifs c
           INNER JOIN candidate_vectors cv
             ON c.candidate_id = cv.candidate_id
           WHERE c.status = 'candidate'
             AND c.base_rag_similarity IS NOT NULL
             AND cv.embedding_model = 'nomic-embed-text:latest'
             AND cv.embedding_dim = 768"""
    ).fetchall()

    reranked = 0
    for row in candidates:
        try:
            vec_row = conn.execute(
                """SELECT vector_blob FROM candidate_vectors
                   WHERE candidate_id = ? AND embedding_model = 'nomic-embed-text:latest'
                   AND embedding_dim = 768""",
                (row["candidate_id"],),
            ).fetchone()
            if vec_row is None:
                continue

            import numpy as np
            vec = np.frombuffer(vec_row["vector_blob"], dtype=np.float32)
            scenario_keys = json.loads(row["scenario_keys_json"] or "[]")

            breakdown = reranker.score(
                candidate_vector=vec,
                base_rag_similarity=row["base_rag_similarity"],
                scenario_keys=scenario_keys,
                profile_version=None,
                enabled=True,
            )

            conn.execute(
                """UPDATE candidate_gifs
                   SET final_score = ?, profile_score = ?, score_profile_version = ?
                   WHERE candidate_id = ?""",
                (
                    breakdown["final_score"],
                    breakdown.get("profile_score"),
                    breakdown.get("preference_profile_version"),
                    row["candidate_id"],
                ),
            )
            reranked += 1
        except Exception:
            pass  # reranking is best-effort

    conn.commit()
    log(f"Preference reranking complete: {reranked} candidates rescored")

log("=" * 50)
log("Pipeline Stage 2 finished")
