# Task 6 Report: Direct/Staged Pipeline and Immutable Manifest Integration

## Outcome

- Direct and staged rank paths now call the same `evaluate_candidates` boundary after transition/action guards and dedup, before preference ranking or export.
- The default report-only path preserves the candidate count and order, including hard-rejected candidates. Active mode fans out only effective `KEEP_AS_IS` and `KEEP_FOR_REPAIR` candidates while retaining `REVIEW`/`ABSTAIN` assessments in the quality summary.
- Rank manifests contain a zero-safe quality summary, top assessment projection, full assessments, and immutable per-clip assessment copies.
- GIF export applies only a context-bound, validated `KEEP_FOR_REPAIR` recipe. Palette generation and GIF rendering share one `build_ffmpeg_filter` result, the source file is not rewritten, and GIF/materialized manifests retain decision, score, recipe, evidence, config, and parent-source lineage.
- Direct mode and staged jobs resolve quality configuration from their single frozen job snapshot. PyInstaller coverage explicitly includes the complete `app.quality_moe` package.

## Validation hardening

- Quality decision/evidence enums, finite confidence and score ranges, lowercase SHA-256 values, evidence-content hashes, summary counts/order, top-summary projection, active/report-only routing, per-clip immutability, repair bounds, and repair validation context are checked at artifact ingestion.
- Repair application and manifest ingestion resolve the validation evidence ID back to an `AVAILABLE`, positive `repair_delta` item whose candidate, evaluation, config, source, and proxy hashes match the selected recipe context.
- Existing schema-v2 artifacts without quality data remain compatible; when quality data is present, it is validated strictly.

## TDD evidence

- Config/manifest/integration baseline: observed 3 expected failures before the first implementation; focused loop then passed 63 tests.
- Pipeline routing and shared-filter tests: observed 4 expected failures before implementation, then passed.
- Packaging/config tests: observed 2 expected failures before implementation, then passed.
- Strict manifest, GIF lineage, legacy caller, report-only hard-reject, repair mismatch, and direct snapshot cases were each introduced as failing tests before their fixes.
- Self-review added failing tamper tests for evidence-content hashes, immutable top summaries, and forged non-delta repair evidence; all passed after validator and export-gate hardening.

## Verification

- `uv run pytest -q tests/quality_moe tests/test_adaptive_config.py tests/task_engine/test_manifest_validation.py tests/task_engine/test_production_artifact_contract.py tests/task_engine/test_packaged_stage_imports.py`
  - `207 passed in 8.69s`
- `uv run pytest -q tests/task_engine/test_full_production_stage_chain.py tests/task_engine/test_stage_pipeline.py tests/task_engine/test_stage_inputs.py tests/task_engine/test_materialize_production.py tests/task_engine/test_materialize_resolver.py tests/test_adaptive_direct_transition.py tests/test_adaptive_direct_action.py tests/test_action_pipeline.py`
  - `67 passed, 8 warnings in 34.17s`
- The warnings are existing Pillow `Image.getdata` deprecations. No production data or `dist` artifacts were modified.

## Remaining concerns

- The test suite covers FFmpeg command construction and the production stage chain, but this task did not perform a live Ollama quality-judge run against the configured model endpoint.
