# Human-reviewed driving labels

`driving-labels-v1` is the regression contract for corner-window and driver-
event detection. Model proposals always begin pending. Because every v1
checksum and attestation field is publicly reproducible, even a structurally
approved artifact remains `SELF_ATTESTED_NOT_AUTHENTICATED` and cannot unlock a
trusted regression result.

This contract validates detection only. It does not validate a `LONG_COAST`
diagnosis, prove a causal time gain, or unlock personalized coaching.

## Approval states

- `PENDING_HUMAN_REVIEW`: structurally valid model proposal; not a golden set.
- `APPROVED`: self-declared workflow state in which every proposal has an
  explicit disposition and the public checksums bind the exact label content.
  It does not authenticate the reviewer or establish golden truth.
- `REJECTED`: self-declared workflow rejection, likewise unauthenticated.

The CLI deliberately has no `approve` action. `propose` always writes an empty
`human_labels` array, null review evidence, and
`status=PENDING_HUMAN_REVIEW`. Neither validation nor regression changes that
state or creates a reviewer identity, timestamp, attestation, tolerance, or
evidence artifact.

## Generate a candidate

First save the complete JSON from `driving-replay`; receipt-only output is not
enough because label generation needs the corner model and per-lap metrics.

```bash
uv run python scripts/run_local_cli.py driving-replay \
  "data/raw/audir8lmsevo2gt3_spa up.ibt" \
  --source-id public-audi-r8-evo2-spa \
  --session-id public-fixture-2023-12-race \
  --require-ready > /tmp/audi-spa-driving-replay.json

uv run python scripts/run_local_cli.py driving-labels propose \
  /tmp/audi-spa-driving-replay.json \
  --output data/labels/candidates/audi-spa-v1.candidate.json \
  --label-set-id audir8lmsevo2gt3-spa-gp-v1 \
  --car-key audir8lmsevo2gt3 \
  --track-key spa \
  --layout-key grand-prix-pit
```

The second command exits 5 and prints `status=WAIT_HUMAN_LABELS`. That is a
successful candidate-generation result: the file exists and validates, but no
human has approved it. An existing destination is never overwritten.

## Human review boundary

Reviewers should mark a telemetry or replay view that does not show the model
coordinates on the first pass. A second independent or repeated pass sets the
accepted position and uncertainty. The evidence artifact itself is referenced
by SHA-256. That digest checks identity/integrity only; it is not a signature
and does not prove who performed the review.

An approved human corner records:

- a stable human `label_id` and track-order `ordinal`;
- `CONFIRMED`, `CORRECTED`, `REJECTED_PROPOSAL`, or `NEW_HUMAN_CORNER`;
- expected detection-window start/end positions and explicit tolerances;
- expected brake onset, one or more apexes, and throttle pickup;
- `PRESENT` or `ABSENT` for optional brake/throttle events;
- at least two review passes and the independent evidence hash.

All positions and tolerances are integer millimetres. V1 caps tolerances at
20 m for window boundaries, 10 m for brake onset, and 15 m for apex and
throttle pickup. These caps are not permission to use the maximum: the stored
tolerance must come from review disagreement.

The intended review workflow populates approval fields and public checksums,
but v1 cannot verify that the workflow was actually operated by a human.
`seal_driving_labels` accepts only still-pending candidates; it refuses both
`APPROVED` and `REJECTED` artifacts, so it cannot reuse an old reviewer identity
or timestamp to refresh modified labels. A future detached signature or other
allowlisted trust anchor is required before authentication can pass.

## Validate and regress

```bash
uv run python scripts/run_local_cli.py driving-labels validate \
  data/labels/candidates/audi-spa-v1.candidate.json

uv run python scripts/run_local_cli.py driving-labels regress \
  data/labels/approved/audi-spa-v1.approved.json \
  /tmp/audi-spa-driving-replay.json
```

`validate` performs strict key, type, state, tolerance, provenance, and
canonical-checksum validation. JSON duplicate keys and non-finite
`NaN`/`Infinity` values are rejected before contract validation. A valid but
unapproved artifact returns `WAIT_HUMAN_LABELS` and exit 5. A structurally valid
self-attested `APPROVED` artifact reports `structural_validation_status=PASS`
but its primary/trusted status is `WAIT_HUMAN_AUTHENTICATION`, also with exit 5.
At the Python API boundary, `require_approved=True` means workflow approval
only; `require_trusted=True` fails closed with `WAIT_HUMAN_AUTHENTICATION`.

`regress` will not evaluate an unapproved label set. For approved labels it:

1. verifies the producer's complete `driving_replay_sha256`, exact top-level
   replay schema, current normalized-telemetry contract, and track length;
2. uses the human-labeled lap ordinal instead of silently accepting a newly
   selected reference lap;
3. requires the evaluated source, session, input kind, raw-source hash,
   normalized samples, input provenance, and track context to match the
   candidate basis;
4. matches expected and predicted corners one-to-one in track order;
5. fails on missing, extra, or reordered corners instead of nearest-neighbour
   remapping;
6. accepts coordinates only in `0 <= mm < track_length_mm`, then compares them
   with circular track distance and inclusive tolerances;
7. distinguishes a missing event key from an explicit `null`/`ABSENT` event;
8. reports explicit `PRESENT`/`ABSENT` and apex-count failures;
9. hashes the exact label artifact, candidate, evaluated replay identity,
   provenance comparisons, model, pipeline, field errors, and result.

The output separates `comparator_status` from the primary trusted status. A
geometry comparison may return `comparator_status=PASS`, while v1 must still
return `status=trusted_regression_status=WAIT_HUMAN_AUTHENTICATION` and CLI exit
5. There is no trusted `PASS` path until reviewer authentication is implemented.

The comparator does not assume throttle pickup follows minimum-speed apex. It
also keeps the model's `accounting_start_m/carry_end_m` closure partition out
of human boundary truth. V1 window labels map only to
`CornerSegment.brake_start_m/exit_m`; event labels map to the fixed lap's
`brake_onset_m/apex_m/throttle_pickup_m`.

## Frozen Audi/Spa candidate

[`audi-spa-v1.candidate.json`](../data/labels/candidates/audi-spa-v1.candidate.json)
was generated from the current `normalized-telemetry-v3` /
`normalized-sdk-adapter-v3` Audi R8 LMS Evo II GT3 × Spa replay.

- source SHA-256:
  `754d14e6e2870eb00d42368e36c7a15495cb68bfd5f829ab08665d0f95fc7f36`
- labeled model reference lap: 11
- proposal count: 8
- model-output SHA-256:
  `f7a7165b19dfa08f1576b3f2e495cfbedb2011aa32317b91d3eb725967af3195`
- model-semantic SHA-256:
  `74f6f52d5743260cbdcedaa59a0e0620afb1d8c8987195009e31f7cb86399df6`
- candidate-payload SHA-256:
  `f30ea24e0b52400b704e91c1ae385f8d903d9d2ff6ec67e6c2039baa1690cdfa`
- artifact SHA-256:
  `7cbf488e74220df163b23c6e79544ac28654adf91158d035ee412faced14d8dc`
- review status: `PENDING_HUMAN_REVIEW`
- review authenticity status: null until a self-attested workflow decision
- labels-content and review hashes: null

The eight coordinates are reproducible model proposals only. They are useful
for freezing the review input and testing provenance, but they are explicitly
not golden labels and do not satisfy the human-corner-label capability gate.
