#!/usr/bin/env python3
"""Resume LLM synthesis from VLM frame annotations (checkpoint recovery)."""
import sys, json, re, uuid, time
from datetime import datetime, timezone
import httpx

sys.path.insert(0, '.')
from app.db import init_db, get_connection

init_db()
conn = get_connection()

LLM_MODEL = 'fredrezones55/Qwen3.5-Uncensored-HauhauCS-Aggressive:9b'
OLLAMA_BASE = 'http://localhost:11434'

# Find media with frame_annotations but no media-level annotation
rows = conn.execute('''
    SELECT DISTINCT m.media_id, m.file_path
    FROM media m
    INNER JOIN frame_annotations fa ON m.media_id = fa.media_id
    WHERE m.media_id NOT IN (SELECT media_id FROM annotations)
      AND m.is_representative = 1
''').fetchall()

print(f'GIFs with VLM frames but no annotation: {len(rows)}')


def parse_json(text):
    text = text.strip()
    if text.startswith('```'):
        text = re.sub(r'^```(?:json)?\s*', '', text)
        text = re.sub(r'\s*```$', '', text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r'\{[^{}]*\{[^{}]*\}[^{}]*\}|\{[^{}]*\}', text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return {'_parse_error': True, '_raw': text[:500]}


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
        'Synthesize frame analyses into one annotation. Output ONLY JSON:\n'
        '{\n  "summary": "one sentence describing visual style",\n'
        '  "emotional_core": "one dominant emotion",\n'
        '  "aesthetic_notes": ["2-4 cinematographic qualities"],\n'
        '  "why_i_like_it": "one cinephile reason",\n'
        '  "tags": ["3-5 keywords"]\n}\n\n'
        'Frame analyses:\n' + analyses
    )

    name = fpath.split('\\')[-1][:40] if fpath else '?'
    print(f'  [{idx+1}/{len(rows)}] Synthesizing {mid[:14]} ({name}...), {len(fas)} frames')

    for attempt in range(3):
        try:
            resp = httpx.post(
                f'{OLLAMA_BASE}/api/generate',
                json={'model': LLM_MODEL, 'prompt': prompt, 'stream': False, 'options': {'temperature': 0.3, 'num_think': 0}},
                timeout=120,
            )
            resp.raise_for_status()
            raw = resp.json().get('response', '')
            parsed = parse_json(raw)
            if parsed.get('_parse_error'):
                if attempt < 2:
                    prompt += '\n\nYour last response was not valid JSON. Output ONLY the JSON object.'
                    continue

            ann_id = f'ann_{uuid.uuid4().hex[:12]}'
            now = datetime.now(timezone.utc).isoformat()
            conn.execute('''
                INSERT INTO annotations (annotation_id, media_id, model_name, summary,
                    emotional_core, aesthetic_notes_json, why_i_like_it, tags_json, raw_json, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
            ''', (ann_id, mid, LLM_MODEL,
                parsed.get('summary', ''), parsed.get('emotional_core', ''),
                json.dumps(parsed.get('aesthetic_notes', [])),
                parsed.get('why_i_like_it', ''),
                json.dumps(parsed.get('tags', [])),
                json.dumps(parsed, ensure_ascii=False), now))
            conn.commit()
            print(f'    -> emotion={parsed.get("emotional_core", "?")}, tags={parsed.get("tags", [])}')
            break
        except Exception as e:
            if attempt == 2:
                print(f'    FAILED: {e}')
            time.sleep(3)

conn.commit()
annotated = conn.execute('SELECT COUNT(*) FROM annotations').fetchone()[0]
print(f'\nAnnotations complete: {annotated} total')
