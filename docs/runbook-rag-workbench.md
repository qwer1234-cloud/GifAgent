# RAG Workbench Runbook

## Preference Memory Migration Safety

Before running Preference Memory migrations on the production database:

1. Stop VLM loop, stage2 synthesis, FAISS rebuild, and adaptive test workers.
2. Confirm `data/library.db-wal` is no longer growing.
3. Back up `data/library.db`, `data/faiss/`, and current model configuration outside Git.
4. Run `python scripts/preference_memory.py status --json`.
5. Proceed only when the operator has confirmed the active ingest run is complete.
