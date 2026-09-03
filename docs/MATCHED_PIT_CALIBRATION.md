# Matched pit/service calibration

## Outcome

The frozen analysis-v2 source now contains a deterministic builder for the
exact calibration-model shape consumed by the M2 strategy gate. It converts a
separately reviewed `matched-pit-calibration-dataset-v1` object into a
`matched-pit-service-median-v1` model with:

- counterfactual pit-lane loss and an empirical min/max envelope;
- measured refuel rate;
- measured tire-change time;
- explicit service-label availability;
- event-identity, source-dataset, and model SHA-256 bindings; and
- a minimum of three independent source and label receipts.

This closes a **builder and admission-contract gap**. The repository does not
yet contain an accepted real matched dataset, so it does not claim a calibrated
Audi/Spa value or a race-ready pit recommendation.

## Why M1 observations are not enough

The M1 pit/stint receipt can directly observe pit-road elapsed time, stationary
time, `PitstopActive`, and tank-level endpoints. Those observations do not by
themselves reveal:

- how much time the same car would have taken through the matched on-track
  segment;
- which service produced the stationary interval;
- how much fuel was actually delivered; or
- whether and how long a tire change occurred.

The calibration builder therefore never promotes an M1 elapsed interval or a
tank-level delta. Every sample must carry a separately retained source receipt
and a separately retained label receipt.

## Exact dataset contract

The top-level object has only:

```text
contract_version
dataset_id
dataset_sha256
dataset_version
event_identity
samples
```

`event_identity` uses the same ten-field identity as M2: series, season, race
week, track, track configuration, car class, event type, simulator build,
official status, and provenance. A real live calibration must match the
identity derived from the same held `SDK_LIVE` capture exactly.

Every sample has only:

```text
sample_id
source_receipt_sha256
label_receipt_sha256
pit_road_elapsed_s
matched_track_segment_elapsed_s
stationary_service_elapsed_s
fuel_delivered_l
fuel_service_elapsed_s
tire_change_elapsed_s
```

Sample IDs, source receipts, and label receipts must each be unique. At least
three samples are required. The builder rejects non-finite or non-positive
measurements, stationary time not shorter than the pit-road interval, fuel or
tire service longer than total stationary service, a refuel rate above the
contract's 20 L/s sanity ceiling, and any match producing negative pit-lane
loss.

## Derivation

For each accepted sample:

```text
non_service_pit_transit_s = pit_road_elapsed_s - stationary_service_elapsed_s
pit_lane_loss_s = non_service_pit_transit_s - matched_track_segment_elapsed_s
refuel_rate_l_per_s = fuel_delivered_l / fuel_service_elapsed_s
```

The model uses the median sample pit loss, median sample refuel rate, and median
tire-change time. Its pit-loss uncertainty is the observed sample minimum and
maximum. This is an empirical envelope, not a validated high-quantile coverage
guarantee. A later acceptance gate still needs enough real repeats across the
intended session types and conditions.

The canonical dataset SHA-256 excludes only `dataset_sha256`; the canonical
model SHA-256 excludes only `model_sha256`. Both use sorted, compact UTF-8 JSON
with non-finite values forbidden. The caller must retain and supply the
expected dataset digest independently; copying the digest from the untrusted
input defeats that admission boundary.

## Build command

The command is available from an installed wheel or this checkout:

```powershell
.venv\Scripts\iracing-aie.exe build-pit-calibration `
  .\matched-pit-calibration.json `
  --expected-dataset-sha256 <independently-retained-canonical-dataset-sha256> `
  --output .\matched-pit-calibration-model.json
```

The output is CreateNew-only and is never overwritten. Success prints the
model digest and sample count. Invalid or insufficient evidence returns a
machine-readable `WAIT_CALIBRATION` result and does not create a model.

## Retrieved-live integration

`finalize-live-analysis` and `verify-live-analysis` accept calibration only as
an all-or-none triple:

```text
--calibration-model
--expected-calibration-model-sha256
--expected-calibration-source-receipt-sha256
```

The analysis bridge validates the model, requires its identity digest to equal
the identity projected from the same sealed live capture, and requires its
refuel rate to equal the independently hashed analysis profile's refuel rate.
The verifier must receive the same model and pins to reproduce the engineer
session object-exactly.

Once admitted, M2 advances:

```text
pit_loss_calibration = PASS_CALIBRATED
service_labels       = PASS_SERVICE_LABELS
```

If no valid short-window motion receipt exists, traffic remains
`OBSERVED_ONLY_WAIT_REJOIN_MODEL / REJOIN_ESTIMATOR_REQUIRED`. With same-capture
motion evidence it advances to
`OBSERVED_ONLY_WAIT_ACTION_BOUND_REJOIN / ACTION_BOUND_REJOIN_ESTIMATE_REQUIRED`.
Calibration alone still cannot produce `PASS_TRAFFIC_DATA`: the estimate must
be bound to the exact M2 fuel quantity, tire choice, and service ordering.

The analysis-v2 candidate now performs that final binding through the separate
[`time-domain-rejoin-estimate-v1`](TIME_DOMAIN_REJOIN_ESTIMATE.md) contract. A
stable action-specific bracket may pass the traffic gate; an uncertainty
zero-crossing or unstable neighbor order remains WAIT.

## Verification

Focused coverage includes exact median/envelope derivation, external and self
hashes, three-sample and independent-lineage requirements, physical consistency
checks, duplicate JSON-key rejection, CreateNew/no-overwrite behavior, exact M2
consumption, same-capture live identity closure, object-exact bundle replay, and
rejection before output when the calibration identity is crossed.

```powershell
.venv\Scripts\python.exe -m pytest `
  tests\test_pit_calibration.py `
  tests\test_offline_m2_strategy_receipt.py `
  tests\test_retrieved_live_analysis.py -q
```

The current focused result is `72 passed, 1 skipped`; the skip is the already
documented absent public Audi/Spa IBT, not a calibration failure.

## Safety and release status

The builder and integration are advisor-only. They do not connect to Aeis,
start iRacing, send network traffic, emit audio, alter the pit black box, or
control the car. The frozen collector v7 and retrieved-live analysis v1 release
remain unchanged. This code is frozen in the analysis-v2 release and has only
synthetic calibration proof.
