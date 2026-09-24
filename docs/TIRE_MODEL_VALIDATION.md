# Frozen tire-model holdout validation

This **offline source/CLI** slice evaluates the existing reviewed-origin v2
tire-performance model without refitting it on held-out stints. It is a
prerequisite for later native admission, not a live model loader or tire-service
recommendation. The existing native EXE and settings are unchanged.

The original event identity identifies a car **class**, not a car model or
setup. The new request therefore requires explicit reviewed car-model and
setup bindings for **every lap**, as well as different training/holdout source
receipts. An old model hash alone does not satisfy this contract.

## Private request

`tire-performance-validation-request-v1` has exactly these fields:

| Field | Required input |
|---|---|
| `contract_version` | The v1 request version above. |
| `request_sha256` | Canonical hash of the other request fields; separately retained and supplied to the command. |
| `training_model_sha256` | Previously retained v2 model hash; the rebuilt training model must match. |
| `training_dataset` | Complete, self-hashed `matched-tire-performance-dataset-v2`. |
| `holdout_dataset` | A second complete v2 dataset, not used to fit/widen the model. |
| `applicability` | Exact object: integer `car_model_id`, `setup_sha256`, `review_receipt_sha256`. |
| `lap_contexts` | One exact coordinate-bound context for every early/late lap in both datasets. |

Each dataset has 3–64 reviewed full-new-set stint pairs. Both must use the same
complete event identity, compound and frozen fuel-load-effect model. Within a
capture, installation/lap coordinates and service intervals cannot overlap.
Across splits, entire declared capture-source receipts, sample/stint/lap ids,
and review/condition/installation receipts must be disjoint. The fuel correction
cannot declare a holdout source as its own calibration source. These checks
detect reuse of the supplied lineage, not dishonest replacement of its hashes.

Each `lap_contexts` entry has exactly:

```text
split                    "training" or "holdout"
source_receipt_sha256     the corresponding pair's source receipt
session_tick             the exact corresponding lap tick
car_model_id             identical to applicability.car_model_id
setup_sha256             identical to applicability.setup_sha256
air_temp_c               finite numeric, -50..60
track_temp_c             finite numeric, -50..100
wind_speed_mps           finite numeric, 0..100
wind_direction_rad       finite numeric, 0..2*pi, one consistent reference convention
precipitation_pct        exactly 0 in this dry-only version
track_state              exactly "REVIEWED_DRY"
```

These broad numeric limits are input sanity bounds, not calibrated operating
limits. Missing/duplicate/unbound contexts, wet conditions, booleans used as
numbers, unknown keys, invalid self hashes and inconsistent bindings fail.
Inputs are at most 1 MiB; tire ages are bounded to 10,000 completed laps and
integer coordinates to 2^53 before numerical model work.

The caller must retain and review the actual setup and source evidence privately.
There is no automatic setup-hash extraction from SDK metadata in this slice.
The review receipt declares that both pair matching and car/setup bindings were
checked; hashes do **not** authenticate telemetry, service contents, reviewer
identity or when a model was frozen. Review remains responsible for traffic,
damage, rubber state, driving changes and other unmeasured confounders. A setup
file must use a consistent hashing convention; do not substitute a class id.

## Evaluation

1. Validate the pinned request and both unchanged v2 datasets, then rebuild
   **only** the training model and require its independently retained hash.
2. Derive separate observed training minima/maxima for early laps, late laps
   and their signed differences. Features are tire age, starting fuel, air/track
   temperature and wind components `speed*cos(direction)`, `speed*sin(direction)`.
   This avoids the false discontinuity at 0/2*pi. Holdout values outside any of
   these marginal ranges are ineligible; no extrapolation or domain widening.
3. For each holdout pair predict:
   `lap-time delta = tire-age slope * age delta + fuel effect * fuel delta`.
   The predicted interval enumerates both slope endpoints and both fuel-effect
   endpoints. A negative fuel delta must retain its sign. The fixed 1e-9
   comparison epsilon is floating-point tolerance, not a fitted error margin.
4. Retain **every** pair's observed/predicted delta, interval, signed residual,
   domain violations and inclusion result. Summary error metrics include all
   supplied holdout pairs; the coverage count includes only eligible pairs.
5. `PASS_REVIEWED_HOLDOUT` requires a positive training degradation envelope,
   every holdout pair in domain, and every observed delta inside the unchanged
   envelope. Otherwise write `WAIT_REVIEWED_HOLDOUT` with explicit reasons.

This is an empirical envelope check, **not statistical coverage**, causal tire
wear or proof of adequate accuracy for race strategy. Marginal ranges do not
establish joint-condition support. Three pairs are a minimum sanity check, not
a guarantee of sufficient calibration. The report always retains:

```text
advisor_only = true
live_model_admitted = false
live_acceptance = false
physical_wear_available = false
source_authenticity = UNVERIFIED_CALLER_REVIEWED_INPUTS
interval_meaning = EMPIRICAL_ENVELOPE_NOT_STATISTICAL_COVERAGE
```

`verify_tire_validation_report` reconstructs the report from the pinned request
and compares canonical objects exactly. Rehashing edited errors, coverage,
domains or acceptance flags cannot pass reconstruction. The v2 model and its
existing offline consumer are unchanged and do not gain stronger evidence
merely because this validation command exists.

## Command and persistence

From the source checkout, keep both paths outside AEIS source repositories:

```powershell
uv run iracing-aie validate-tire-performance `
  C:\Users\racer\Documents\AEIS-private\tire-validation-request.json `
  --expected-request-sha256 <separately-retained-request-digest> `
  --output C:\Users\racer\Documents\AEIS-private\tire-validation-report.json
```

The command reports only fixed status/reason codes, counts/errors and report
hash, never the request path, arbitrary sample ids or exception details. It
returns 0 for the limited offline pass and 2 for a numerical WAIT or invalid
input. A numerical WAIT **still writes the failed evaluation**, so bad results
cannot disappear merely because they were not a pass.

Reads are bounded descriptor reads with private-root, regular-file, no-link and
before/after identity checks. Duplicate JSON keys and nonfinite values fail.
CreateNew output cannot overwrite an existing file. Flushed descriptor readback
and named-file exact reconstruction precede success. Invalid requests produce
no output; I/O failures can leave an incomplete new file, never a successful
receipt. These checks are not a defense against a hostile same-user process.

## Remaining product work

The tests use explicitly invented stints and declarations. There is no genuine
calibration or holdout acceptance in this milestone. A separate
[native preflight](LIVE_TIRE_CALIBRATION.md) now performs parked private-request
verification, owned car/setup binding and current condition checks. Its best
status is a context-only match, not model admission. Actual reviewed calibration,
current tire/fuel belief and action-bound tire/service/rejoin integration remain
open. No simulator launch, game command, provider call, speech or live-owner
mutation is performed by this evaluator.
