#!/usr/bin/env python3
"""Recover LLM synthesis for RAG test after VLM completion."""
import json, re, time, httpx

OLLAMA_BASE = 'http://localhost:11434'
LLM_MODEL = 'fredrezones55/Qwen3.5-Uncensored-HauhauCS-Aggressive:9b'

with open('data/test_jur639_rag_result.json') as f:
    data = json.load(f)

scenes = data.get('top_scenes', [])
if not scenes:
    print('No scenes to synthesize')
    exit(1)

analyses = '\n\n'.join(
    f"Frame {s['rank']}: caption={s.get('caption','')}, emotion={s.get('emotional_core','')}, "
    f"aesthetic={s.get('aesthetic_notes',[])}, why={s.get('why_i_like_it','')}"
    for s in scenes
)

prompt = (
    'Synthesize these film frame analyses into ONE cohesive annotation. Output ONLY JSON:\n'
    '{\n  "summary": "one sentence describing the visual style",\n'
    '  "emotional_core": "one dominant emotion",\n'
    '  "aesthetic_notes": ["2-4 cinematographic qualities"],\n'
    '  "why_i_like_it": "one cinephile reason",\n'
    '  "tags": ["3-5 keywords"],\n'
    '  "scene_type": "close-up | dialogue | action | transition | reaction | establishing | montage | other"\n'
    '}\n\n'
    'Frame analyses:\n' + analyses
)

LLM_MODEL = 'hf.co/unsloth/Qwen3-14B-GGUF:Q4_K_M'

def parse_json(text):
    text = text.strip()
    # Strip think tags (Qwen-style reasoning)
    if '</think>' in text:
        text = text.split('</think>')[-1].strip()
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
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

print('Sending prompt to LLM...')
resp = httpx.post(
    f'{OLLAMA_BASE}/api/generate',
    json={'model': LLM_MODEL, 'prompt': prompt, 'stream': False, 'options': {'temperature': 0.3}},
    timeout=120,
)
resp.raise_for_status()
resp_data = resp.json()
raw = resp_data.get('response', '') or resp_data.get('thinking', '')

synthesis = parse_json(raw)
print('Synthesis result:')
print(json.dumps(synthesis, ensure_ascii=False, indent=2))

# Save updated
data['synthesis'] = synthesis
with open('data/test_jur639_rag_result.json', 'w', encoding='utf-8') as f:
    json.dump(data, f, ensure_ascii=False, indent=2)
print('\nUpdated test_jur639_rag_result.json')
