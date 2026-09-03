# Offline post-session report

The project now produces a self-contained local HTML report for the pinned
Audi R8 LMS EVO II GT3 × Spa recording. It is an evidence review, not a live
race-engineering surface: every driving finding remains `SHADOW_ONLY`, overall
corner confidence is `LOW`, condition and human-label gates remain `WAIT`, and
the real race recommendation remains `BLOCKED`.

Open the current local report:

- [`audi-spa-offline-session-report-v1.html`](../data/derived/audi-spa-offline-session-report-v1.html)
- [`audi-spa-offline-session-report-v1.artifact.json`](../data/derived/audi-spa-offline-session-report-v1.artifact.json)

Both files are generated under the ignored `data/derived/` directory. They are
local evidence artifacts and are not redistributed with raw telemetry.

## Reader-facing result

The report binds four complete, exact-hash-validated receipts: the driving
replay, corner cards, fuel replay, and development-smoke pit plan. It shows:

- a real eligible reference lap 11, seven clean laps, six comparison laps per
  candidate window, and a 1 m spatial grid;
- C08 at 0.294 s with support on 5/6 comparison laps, C02 at 0.230 s with
  support on 5/6, and C01 at 0.163 s with support on 4/6;
- all 18 per-lap observations in the only chart, including negative
  counterexamples and a zero reference line;
- C08 and C02 with no supported action diagnosis;
- C01 as a practice-only `LONG_COAST` hypothesis whose loss evidence and
  action evidence remain separate;
- 15 admitted historical burn laps, mean 3.918 L/lap, and observed P90 3.999
  L/lap, explicitly limited to this recording; and
- current tire wear, opponent fuel, traffic, and rejoin state as unavailable
  and not estimated.

The report deliberately omits every development-smoke pit action and number.
In particular, no candidate pit lap, fuel quantity, tire choice, service time,
or pit-loss value is exposed in the artifact or HTML. The non-official rules
prove only deterministic calculation closure; they do not support race advice.

## Deterministic build

Generate the complete fuel replay and development-only pit receipt as described
in [`OFFLINE_PIT_PLAN.md`](OFFLINE_PIT_PLAN.md), then run:

```bash
uv run python scripts/build_offline_session_report.py \
  data/derived/audi-spa-driving-replay-v1.json \
  data/derived/audi-spa-corner-cards-v1.json \
  /path/to/exact-audi-spa-fuel-replay.json \
  /path/to/exact-development-smoke-pit-plan.json \
  --expected-driving-replay-file-sha256 \
  b1825535c80d316b5379ae646c9710383050637ff7c335e9fe056a1d29010adf \
  --expected-driving-replay-sha256 \
  c5a8f19f156c57c3951e112df24ad3e3f07956961b78c68fe972a534955ebb82 \
  --expected-corner-cards-file-sha256 \
  ea569e10989fc614577a072fef367817c301db233bd776e731e3f9054813ef22 \
  --expected-fuel-replay-file-sha256 \
  20976d7508604589ff9360604a7167a6ee482f08bc00bf1ee814513a079e81d7 \
  --expected-fuel-replay-sha256 \
  1f3b642c43dd6b7cd16e433dee3f26335f9aecd0c83950e02b706a2f79c3a65c \
  --expected-pit-plan-file-sha256 \
  a0773afef4dc2904507182698684b2e49f401fdb202ecc882192232ef9d64d71 \
  --output data/derived/audi-spa-offline-session-report-v1.artifact.json
```

The writer uses exclusive create and refuses overwrite. Input basenames do not
affect the result: provenance uses role-scoped content identities plus both
serialized and canonical SHA-256 values. Source metadata explicitly labels
those paths as non-resolvable logical identities. Its SQLite queries replay
already-audited bounded rows through an inline `VALUES` CTE; they do not claim
to read files at those logical paths.

Package the canonical artifact with the Data Analytics portable report builder:

```bash
npm --prefix "$DATA_ANALYTICS_PLUGIN_ROOT" run report:deliver -- \
  --input "$PWD/data/derived/audi-spa-offline-session-report-v1.artifact.json" \
  --output "$PWD/data/derived/audi-spa-offline-session-report-v1.html"
```

## Frozen local receipt

| Item | Current receipt |
|---|---|
| Builder SHA-256 | `c334382d51d3ecfc0be00fb6e028f091674a9bdd332900754859261c3ff45c7a` |
| Builder tests SHA-256 | `071eb8b52c896570c185b84d4aa0dfacda5f2e9f3e7698ca26d7da85bb889333` |
| Artifact | 69,964 bytes; serialized SHA-256 `6aa890488efb42251e8ceff93b32812a61a393f2276481a9f33052e42ee421f3` |
| Artifact self-bound SHA-256 | `21caeb62e817de0939014bbf976a8b4d7ea0e72f1cbae213125110e73b41e648` |
| Portable HTML | 573,783 bytes; SHA-256 `2cd8c74c578f1453cd3fae8296ca393c2bfbfb895fac6b660dadac122aa9086d` |
| Determinism | `PYTHONHASHSEED=1` and `987654` produced byte-identical artifact and stdout |
| Focused report tests | 13 passed |
| Report + pit-plan + corner-card tests | 59 passed in 47.69 s |
| Portable delivery | validation `passed`; package `passed`; verification `structural_only` |
| Interactive browser QA | 1280×720 and 390×844 rendered without horizontal overflow or console warnings; the 18-point chart, zero line, tables, and status boundaries were visibly present |

The portable report contains 19 rendered blocks, one native scatter chart, four
metric cards, and three tables. The portable packager still reports
`structural_only` because no installed Chromium headless-shell was available;
it did not download a browser, and source-dialog interaction remains untested.
A separate local in-app-browser pass rendered the report at 1280×720 and
390×844, found no horizontal overflow or console warnings, and visually
confirmed the 18-point scatter, negative counterexamples, zero reference line,
tables, and safety status text. Individual point labels were deliberately
removed after that pass exposed narrow-screen collisions; lap identity remains
available through tooltips and the complete per-lap table.

## Remaining product boundary

This report closes the MVP's local report-surface requirement for the pinned
offline evidence only. It does not close the live SDK gate, real event-rules,
rejoin traffic, tire/opponent inference, authenticated corner labels, three
supported driving diagnoses, or live speech/mute requirements.
