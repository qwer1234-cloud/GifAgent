# Milestone 0B — Real-Media Feasibility Findings

Date: 2026-09-05
Status: measured; go/no-go state `INSUFFICIENT_REAL_DATA` (expected — human
video labels not yet provided and two historical library strata are empty)
Harness: 0A CLI + `real-yolov8pose-yunet-v1` experimental backend
Run: `data/continuity_spike_runs/run-74c8addb-9e43-4f7f-b39c-d5dcb7fba9ce`

## 1. What was run

- **Backend**: YOLOv8n-pose (ONNX, DirectML on RTX 4070 Ti SUPER) for person
  detection + COCO-17 keypoints; YuNet (OpenCV FaceDetectorYN) for face
  boxes; greedy subject tracking; background-affine camera compensation with
  torso-scale-normalized amplitude; ffmpeg-based frame extraction (8 FPS,
  960 px analysis width).
- **Source-video population**: 50 real candidates from 14 files in the
  caller-named video directory, covering all 15 required scenario buckets
  (single/co-primary subjects, partial body, headless, close contact,
  occlusion, fast/subtle/moderate/large motion, low light, media edge,
  face visible/occluded/not-visible). Scenario tags are provisional sampling
  labels derived from measured signals; **no human verdicts exist yet**, so
  every candidate is `human_label=UNLABELED`.
- **Historical population**: 200 real library GIFs selected from
  `dist/GifAgentUI/data/library.db` (17,019 labeled candidate rows scanned,
  13,749 files located), each carrying its **real** preference label:
  favorite 27 / like 108 / dislike 39 / neutral 26.
- **Not measured (recorded honestly)**: local VLM semantic review
  (`vlm_status=SKIPPED`; Ollama was not running — 406 `ollama_unavailable`
  telemetry records), body/face appearance embeddings (identity statuses
  `UNAVAILABLE`; tracking is geometric only), facial landmark/expression
  dynamics (YuNet provides 5 landmarks only; advisory statuses
  `UNAVAILABLE`, which never alter dispositions).

## 2. Go/no-go state

`INSUFFICIENT_REAL_DATA` with exactly three reasons:

1. `unlabeled_source_video_candidates` — the 40 %/60 % usability gates
   require human `ACCEPTABLE`/`INVALID` verdicts that only the user can
   supply.
2. `empty_historical_bucket:resolution_le_256`
3. `empty_historical_bucket:resolution_257_480`

The two empty strata are a **real library property**: every labeled export
in the library is >480 px. The le-256 and 257-480 resolution buckets cannot
be populated from existing data without new exports; this limits the
historical yield measurement to the high-resolution strata that actually
exist.

Everything else measured cleanly (0 errors across 250 candidates).

## 3. Performance (all gates passed)

| Metric | Measured | Gate | Pass |
| --- | --- | --- | --- |
| Candidate p50 | 3,443 ms | ≤ 10,000 ms | ✅ |
| Candidate p95 | 5,217 ms | ≤ 30,000 ms | ✅ |
| Source-video batch p95 | 19,306 ms | ≤ 1,200,000 ms | ✅ |
| Peak VRAM | 4,210 MiB | ≤ 14,336 MiB | ✅ |
| GIF batch p50 | ~1,124 ms | — | — |

The heavy end of the p95 is 4K decode plus pose inference; well inside
budget. One residency cycle per source (one `evaluate_source` call per
file) worked as designed.

## 4. Evidence availability findings (the real spike output)

Source-video dispositions: 2 `AUTO_PASS`, 48 `REVIEW_REQUIRED`.
Review reason distribution (48 candidates, multiple reasons possible):

| Reason | Count |
| --- | --- |
| `amplitude_phase_incomplete` | 19 |
| `pose_coverage_low` | 19 |
| `camera_compensation_low_confidence` | 18 |
| `tracking_gap` | 16 |
| `motion_amplitude_discontinuity` | 6 |
| `primary_subject_ambiguous` | 4 |
| `motion_amplitude_below_observable_floor` | 3 |

Reading:

- **Amplitude is the dominant blocker (94 % amplitude-review rate).**
  37/50 candidates were `unobservable` relative to the adaptive floor.
  The noise floor from static-context jitter is high in scenes where the
  subject fills most of the frame (background compensation itself went
  `LOW_CONFIDENCE` in 36 % of candidates — subject boxes cover nearly all
  trackable features). Phase completeness also fails often at 3 s windows
  (38 %): windows cut mid-motion. **This evidence set needs redesign before
  any enforce ambition**: longer context windows for phase, a noise floor
  that does not collapse when background is scarce, and burst sampling for
  fast scenes.
- **Pose coverage** is the second blocker (19/50 below 95 %). Failures
  concentrate in partial-body/headless/close-contact scenes — expected, and
  exactly the scenario buckets the production gate must survive.
- **Face evidence works**: 27/50 USABLE (54 %), 16 detected-but-unusable,
  7 not detected. At 960 px analysis width, usable faces average well above
  the 64 px gate; a high-resolution face branch is not needed for coverage,
  only marginal gains would justify it.
- **Tracking gaps** (16) correlate with pose loss and fast motion; with no
  ReID model the tracker is geometric, so these numbers are an upper bound
  on what a real ReID would resolve.
- **No `HARD_REJECT` was emitted** (and none is valid here): with no VLM
  and no ReID there is never a second independent confirming group — the
  conservative contract behaved as designed.

## 5. Historical curve-feature yield

- 146/200 GIFs (73 %) produced `AVAILABLE` curve features; all 54 failures
  are `pose_coverage_low` (GIF crops, partial bodies, palette compression).
- Effective labeled training rows: like 90 / dislike 22 / favorite 16.
- Measured cost: p50 705 ms, p95 1,334 ms, mean 761 ms per GIF.
- **Projected full-inventory cost: ≈ 3.6 GPU hours for 16,894 items** —
  the historical curve-feature backfill is cheap and feasible.
- Library data quality: 11 of the 200 selected artifacts are byte-identical
  duplicates (same file, two candidate IDs); production exports contain
  duplicates that dedup would need to handle.

## 6. What this means for the go/no-go decision

Performance and resource gates pass with large margins. The decisive open
items before milestone-1 production work:

1. **Human labels** — the user must label the 50 video candidates
   (ACCEPTABLE/INVALID) to evaluate the 40 % auto-pass / 60 % review-rate
   usability gates at all.
2. **Amplitude evidence redesign** — the current adaptive floor and 3 s
   phase windows would send ~94 % of candidates to review; recalibrate
   (context length, floor estimation under subject-dominant frames, burst
   sampling) before treating REVIEW rates as meaningful.
3. **Historical strata gap** — small-resolution buckets do not exist in the
   library; the curve backfill can proceed for the strata that exist
   (~3.6 GPU hours), but the yield measurement cannot claim full stratified
   coverage.
4. **Model upgrades for the next round** — body/face ReID embeddings and a
   local VLM are required before identity-continuity and semantic evidence
   can be measured at all.

Per the 0A/0B contract, no report produced here enables production
`enforce`; the decision on milestone 1 remains with the user.

## 7. Historical curve-feature backfill (executed 2026-09-05)

Script: `scripts/curve_feature_backfill.py` (dry-run → backup → checkpointed
batches, per spec section 12). Target: `curve_features` table (spec 9.6) in
`dist/GifAgentUI/data/library.db`; feature version `curve-v1`; features:
pose_valid_ratio, subject_area_ratio, extension_mean/peak/range,
torso_speed_p95, motion_smoothness, visibility_ratio, border_clip_ratio,
verticality_mean.

- Backup before first write:
  `data/backups/library.before-curve-backfill.20260905_062446.db`,
  SHA-256 `0ec5cab8413bdb580c4ea088992e51914c2609b0e4da11e4d45f6ef80a2e4d25`.
- Label semantics: the app logs a `like` event immediately after every
  `favorite`, so the effective label is *any favorite event → favorite*,
  otherwise the latest non-undone rating; a plain latest-event rule would
  have erased all but one favorite.
- Result: **13,919 rows, 12,445 available (89.4 %)** — effective labeled
  training/ranking rows: like 7,550 / favorite 1,762 / dislike 1,453 /
  neutral 1,680.
- Measured cost: p50 1,043 ms, mean 1,058 ms, p95 1,847 ms per GIF;
  **4.09 GPU hours total** at 0.95 GIF/s on the RTX 4070 Ti SUPER
  (DirectML).
- Failures: 1,463 `pose_coverage_low`, 11 `no_person_detected`; every
  failure is stored as an `UNAVAILABLE` row with its reason and is
  retryable by bumping the feature version.
- Populated strata: `gt_480 × {2–5 s, >5 s}` dominate (12,444 of 12,445
  available rows); small-resolution strata remain absent from the library
  (see section 2). GIF metadata duration proved unreliable versus decoded
  duration, so strata derive from actual decoded frame counts.
- Safety: `candidate_gifs`, `favorite_gifs`, and `preference_events` were
  not modified; GIF files were only read; batches of 25 with a checkpoint
  at `data/curve_backfill_checkpoint.json` make re-rums idempotent.

The `curve-v1` features are now in place for the milestone-8 curve
preference model (L2 logistic regression over favorite/like positive and
dislike negative rows with source-grouped holdout), which remains a
separate, explicitly-gated step.
