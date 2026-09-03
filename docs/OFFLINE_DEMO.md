# Offline engineer demo v1

`offline-engineer-demo-v1` is the single runnable Audi R8 LMS Evo II GT3 × Spa
MVP demonstration. It orchestrates existing models; it does not add a new
prediction model or convert the public recording into event truth.

## Run it

First fetch or verify the pinned local-only IBT:

```bash
uv run python scripts/fetch_public_ibt.py --verify-only
```

Then run every offline engineer component and save the unified receipt:

```bash
uv run python scripts/run_local_cli.py offline-demo \
  --preset public-audi-spa \
  --output /tmp/audi-spa-offline-demo.json
```

`--output` is optional. The full JSON receipt is always written to stdout; when
an output path is supplied, the same receipt is exclusively created there.
An existing output is refused and never overwritten.

Use an alternate exact mirror of the repository data tree with:

```bash
uv run python scripts/run_local_cli.py offline-demo \
  --preset public-audi-spa \
  --manifest /absolute/mirror/data/public_sources.json
```

The manifest option relocates the same preset; it cannot define a different
asset. The package freezes and checks these trust roots independently of the
replaceable manifest:

- raw IBT: 162,304,117 bytes, SHA-256
  `754d14e6e2870eb00d42368e36c7a15495cb68bfd5f829ab08665d0f95fc7f36`;
- pending label artifact SHA-256
  `7cbf488e74220df163b23c6e79544ac28654adf91158d035ee412faced14d8dc`;
- pending label candidate-payload SHA-256
  `f30ea24e0b52400b704e91c1ae385f8d903d9d2ff6ec67e6c2039baa1690cdfa`.

The raw file is hashed through a no-follow regular-file descriptor before the
models run and checked again afterward. The strict manifest and label JSON
readers reject duplicate keys and non-finite values. The preset also freezes
source/session identity, the 20 L / 10 lap smoke scenario, target/reference
lap 11, a 1 m distance grid, top three recommendations, and all component
receipt hashes.

## What one receipt proves

The command executes four fresh paths over the same immutable IBT:

1. the IBT-only shadow report;
2. the source-neutral shared fuel replay;
3. the source-neutral shared driving replay;
4. the condition cohort, followed by structural validation of the pending
   driving-label candidate.

The orchestrator refuses to issue a receipt unless all shared paths agree on
the source/session identity, raw SHA-256, normalized sample receipt, event
receipt, track context, and frozen scenario. It also compares the driving
reference lap, condition target lap, and label candidate basis. Every returned
recommendation must explicitly carry `"executable": false`.

The receipt contains:

- `execution_mode: SHADOW`, `execution_status: COMPLETE`, and
  `advisor_only: true`;
- component and end-to-end canonical SHA-256 receipts;
- non-executable fuel and driving candidates;
- explicit suppressions and readiness gates for unavailable evidence.

## Honest expected state

For the current public Audi/Spa sample, a normal completed demonstration is:

- `offline_demo`: `PASS`;
- shared and legacy shadow fuel/driving gates: `PASS`;
- `condition_trust`: `WAIT_CONDITION_DATA` because only seven clean laps and
  no authenticated track-state labels are available;
- `label_trust`: `WAIT_HUMAN_LABELS` because the stored candidate is still
  pending independent human review;
- `personalized_coaching`: `BLOCKED`;
- `race_recommendation`: `BLOCKED`.

These waits are expected evidence states, so the normal command exits 0. A
CI or deployment gate can demand all offline trust evidence:

```bash
uv run python scripts/run_local_cli.py offline-demo \
  --preset public-audi-spa \
  --require-trusted
```

That command currently exits 5 while still printing the complete receipt.
Exit 3 means a contract, integrity, or output-path error; exit 4 means required
local data is missing. No mode enables vehicle control, pit-box mutation, or
automatic promotion of pending labels.

## Frozen real-run receipt

On 2026-08-08, two fresh processes using `PYTHONHASHSEED=1` and
`PYTHONHASHSEED=987654` produced byte-identical 29,670-byte JSON artifacts:

- serialized artifact SHA-256:
  `8fb7aa2023a06009f27139e43706706063a35300174cd30bbb374a4879199e86`;
- canonical `demo_sha256`:
  `fedebe9bea03be2767ac5a29f3373255d68fb7fff352aeda627d31ff40a26323`;
- normalized input: 151,892 samples, SHA-256
  `f38d336a4d647886c0a4b8fce32fc4d53ba688b26b7c46d70e780e85c012b07e`;
- smoke scenario SHA-256:
  `187bc2c164cde2b48a51965d1c5954bb83bcbbdeab6d829b13c007ed0a4b93cb`.

The canonical digest was independently recomputed after removing only the
`demo_sha256` field, every returned recommendation was rechecked as
non-executable, and every unavailable capability was rechecked as an explicit
non-estimate `SKIP`/`BLOCKED` gate. The compact receipt is frozen in
`data/public_sources.json`;
the raw IBT and full generated output remain local-only.

## Top-3 descriptive loss cards

The package-external `scripts/build_offline_corner_cards.py` verifier consumes
the complete shared driving replay and surfaces recurrent loss windows that do
not happen to trigger an action diagnosis. On the frozen Audi/Spa recording it
ranks C08 (0.294 s median), C02 (0.230 s), then C01 (0.163 s). C08 and C02 stay
`action=null`; only C01 reuses the existing `LONG_COAST` practice action. All
three remain non-executable candidate windows. The command, exact artifact
hashes, and evidence boundary are in
[`AUDI_SPA_CORNER_CARDS.md`](AUDI_SPA_CORNER_CARDS.md).

## Scope

The fuel values are a deterministic smoke-test scenario, not verified event
rules or the car's actual race state. The recording lacks trusted opponent
fuel, current-stint tire wear, traffic-loss truth, authenticated track state,
and independent corner labels. The demo therefore proves replayability,
cross-model input identity, and fail-closed behavior—not production race or
personalized coaching readiness.
