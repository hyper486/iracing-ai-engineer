# Offline Shadow Report v2

`shadow-report-v2` connects the frozen IBT reader, lap-quality gates,
fuel model, and distance-domain driving analysis without producing an
executable race command.

## Run it

The fuel scenario is explicit because an offline recording does not provide a
truthful current-race fuel state, remaining distance, tank limit, or event
refueling rule:

```bash
uv run python scripts/run_local_cli.py shadow \
  "data/raw/audir8lmsevo2gt3_spa up.ibt" \
  --analysis all \
  --current-fuel-l 20 \
  --tank-capacity-l 120 \
  --refuel-rate-lps 2 \
  --remaining-laps 10 \
  --reserve-l 1 \
  --require-capability fuel_model_smoke \
  --require-capability driving_analysis_smoke
```

These example numbers are a deterministic smoke scenario, not verified Audi
or event configuration. Every supplied field is labeled `USER_RULE`.

Use `--receipt-only` for the compact deterministic receipt. A requested
capability that does not pass returns exit code `5`; ordinary, expected
suppression still returns `0`.

## Shared model replay

The source-neutral paths are `fuel-replay` and `driving-replay`: both IBT and
completed collector JSONL are first
normalized to `TelemetrySample`, then traverse the same streaming event,
lap-feature, and model functions.

```bash
uv run python scripts/run_local_cli.py fuel-replay \
  "data/raw/audir8lmsevo2gt3_spa up.ibt" \
  --source-id public-audi-r8-evo2-spa \
  --session-id public-fixture-2023-12-race \
  --current-fuel-l 20 --tank-capacity-l 120 --refuel-rate-lps 2 \
  --remaining-laps 10 --require-ready
uv run python scripts/run_local_cli.py driving-replay \
  "data/raw/audir8lmsevo2gt3_spa up.ibt" \
  --source-id public-audi-r8-evo2-spa \
  --session-id public-fixture-2023-12-race \
  --require-ready
```

`model_semantic_sha256` excludes source identity and proves that equivalent
normalized inputs produce the same lap and model semantics.
`fuel_replay_sha256` separately binds the actual IBT/collector receipt,
identity, event receipt, scenario, quality gate, and shadow recommendation.
Collector recovery prefixes are never accepted by this command.

Driving adds `semantic_input_receipt`, which excludes source identity and
hashes exactly the accepted fields the model consumes. Its track length comes
only from adapter-validated SessionInfo on the same source snapshot.
`driving_replay_sha256` separately binds provenance, event receipt, track
context, capabilities, and non-executable recommendations. Missing track
length, incident counts, or enough clean laps returns `WAIT_DRIVING_DATA`;
integrity faults take precedence and return `FAIL`.

## Evidence separation

The report keeps four collections separate:

- `facts`: lap-level values deterministically derived from direct telemetry;
- `estimates`: model outputs with method version, confidence, and evidence IDs;
- `recommendations`: non-executable `SHADOW_ONLY` candidates;
- `suppressions`: unavailable capabilities and the claims they block.

Every lap evidence ID binds the source SHA-256, lap-algorithm version, and lap
ordinal. The report also parents itself to the existing deterministic replay
hash. Its receipt separately hashes configuration, facts, estimates, model
outputs, recommendations, suppressions, and the complete analysis payload.

## Audi/Spa acceptance result

For the frozen Audi/Spa sample and the example scenario:

- fuel model: `PASS`, 15 admitted laps, mean 3.918 L/lap, empirical P90 3.999 L/lap;
- historical burn stability: `HIGH` within this recording only;
- scenario projection: 40.986 L conservative fuel-to-end, at least one stop,
  next-stop window 0–4 laps from now, 20.986 L cumulative refuel-to-end;
- driving smoke: `PASS`, 7 clean laps, real reference lap ordinal 11;
- track model: 8 contiguous windows covering 0–6,930 m;
- time-loss closure: every analyzed lap has zero residual at the stored precision;
- driving output: one `LONG_COAST` candidate at `C01`, supported by laps 4 and
  10, with 0.279 s median descriptive loss;
- personalized coaching, traffic, current tire wear, and executable race
  recommendations: `SKIP` or `BLOCKED`.

The frozen candidate analysis receipt is
`5d521bdf7443fe5be9e8ab27cd29254bc0ea56ee0dedf2ddd16eda89224554b6`.
It is a deterministic regression target, not human-approved driving truth.

The same Audi fuel scenario through the shared model path has semantic SHA-256
`d68fe9387c7d83db9f6425503f06be98c116d2cab18b63216963a6fa9ec76fe5`
and provenance-bound replay SHA-256
`1f3b642c43dd6b7cd16e433dee3f26335f9aecd0c83950e02b706a2f79c3a65c`.
The recommendation evidence IDs are anchored to normalized-stream SHA-256
`f38d336a4d647886c0a4b8fce32fc4d53ba688b26b7c46d70e780e85c012b07e`,
not to a caller-supplied raw-file claim.

The shared driving path has semantic SHA-256
`74f6f52d5743260cbdcedaa59a0e0620afb1d8c8987195009e31f7cb86399df6`
and provenance-bound replay SHA-256
`c5a8f19f156c57c3951e112df24ad3e3f07956961b78c68fe972a534955ebb82`.
Equivalent synthetic IBT and collector streams must also share the semantic
input, lap receipt, model output, and semantic hash while retaining distinct
provenance-bound hashes.

Both fuel paths call the same source-neutral recommendation builder. For the
same scenario, action fields, claim level, confidence, confidence basis, and
scenario hash must match; only their provenance-bound evidence prefixes may
differ. Tire wear, opponent fuel, and traffic use the same explicit
`SKIP / UNKNOWN / estimate_available=false` capability contract.

## Current boundaries

- The Audi file has no `CarIdx*` opponent arrays, so traffic is unobservable.
- Tire-wear channels describe the removed set at a pit snapshot, not current
  live tire wear.
- `condition-cohort-v1` evaluated the 7 clean laps, but found zero matches:
  track-state labels and opponent arrays are missing, some cross-stint tire-set
  context is unobserved, and the default minimum is 8 matched laps.
- Braking-based corner detection does not yet provide line or curb advice.
- The simple timed-race branch is not a leader-crossing simulation and cannot
  issue a production “box this lap” call.
