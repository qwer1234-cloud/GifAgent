# Quality-First GUI EXE Release Design

## Status

- Date: 2026-08-11
- Decision: approved for autonomous implementation by the user's request to build a new EXE
- Scope: default adaptive extraction profile, packaged writable configuration, safe EXE rebuild, and release verification

## Goal

Make the GUI release prefer strong, coherent GIF candidates without turning low output count into a goal of its own. A video may still produce many GIFs when many candidates meet the quality gates.

## Chosen approach

Use a balanced quality-first profile:

- keep dense discovery (`sample_interval: 7`) so quality is not achieved by missing scenes;
- require stronger merge, refinement, and worthiness evidence (`0.58`, `0.65`, and `0.55`);
- require a strong peak (`merge_peak_threshold: 0.65`) and limit merged spans to 18 seconds;
- lower VLM temperature to `0.25` for more repeatable scoring;
- export up to 75% of post-gate, post-dedup candidates with a cap of 50, so a video with many genuinely strong moments can still yield many GIFs;
- keep 24 fps, 720 px width, low-light tolerance, action completeness, transition protection, and duplicate removal;
- keep Quality MoE in `report_only` until human calibration is complete.

This is preferred over an extreme high-threshold/low-cap profile, which would confuse low yield with high quality, and over an output-all profile, which would let marginal candidates dilute the review set.

## GUI and packaging behavior

The GUI continues to start the existing eight-stage Task Engine pipeline. No new GUI control is required for this release. Fresh installs receive the quality-first source defaults. The preserved writable packaged config is updated only for the quality-profile fields and retains endpoints, model choices, preference settings, databases, and export paths.

The existing EXE is not running, so the rebuild does not interrupt active work. `scripts/rebuild_exe.sh` remains the only rebuild path and must preserve `dist/GifAgentUI/data` and `dist/GifAgentUI/configs`.

## Verification

- a source-config regression test locks the quality-first values and their intent;
- adaptive/config and MoE tests must pass;
- the full repository test gate must pass before packaging;
- the rebuilt EXE must contain the current `app/quality_moe` package and quality-first bundled config;
- the writable packaged config must expose the same profile;
- the real EXE must start, answer API/UI health checks, close cleanly, and release ports 8000/7861;
- production and packaged runtime database hashes must match their pre-build values.

