# Tire-performance belief v1

The frozen analysis-v2 source now contains an advisor-only tire-
performance calibration and service-tradeoff slice. It deliberately keeps two
different questions separate:

1. **performance age effect** — how much condition-matched, fuel-adjusted lap
   time tends to change per completed lap on a tire set; and
2. **physical wear** — the physical state of the currently fitted set.

The first quantity can be inferred from repeated historical stints. The second
is not directly established by the current data path. A positive performance
slope is therefore never relabelled as current tread percentage, and the
physical-wear capability remains `SKIP_CURRENT_PHYSICAL_WEAR`.

This is a source-contract and synthetic-model proof. It is not an accepted
Audi/Spa tire model and does not authorize a live no-tire recommendation. The
M2 v2 path now consumes the model and the current-stint receipt, while
the frozen collector and historical M2 v1 artifacts remain unchanged.

## Matched input contract

`matched-tire-performance-dataset-v1` is a self-hashed, independently pinned
JSON object containing:

- the exact ten-field event identity used by the M2 and pit-calibration paths;
- one tire compound;
- a separately self-hashed fuel-load-effect model and source receipt; and
- at least three disjoint, condition-matched lap pairs from three distinct
  observed stints.

Each pair has an early and late lap with a unique lap id, completed-lap tire
age, lap time, and starting fuel. It also binds a source receipt, a human/event-
label receipt, and a condition-match receipt. The validator requires all lap,
pair, stint, label, and condition identities to be disjoint. The late lap must
be at least two completed laps older and cannot gain fuel inside the stint.

The condition receipt is an input trust boundary rather than a caller-owned
`clean=true` boolean. A real dataset still needs independently reviewed clean-
lap, dry-condition, traffic, damage, setup, and stint-boundary evidence before
its digest is approved.

## Fuel correction and robust envelope

For each disjoint pair, with later-minus-earlier values:

```text
adjusted_delta(beta) = lap_time_delta - beta * fuel_start_delta
age_slope(beta)      = adjusted_delta(beta) / completed_tire_age_delta
```

`beta` is the independently bound seconds-per-liter fuel-load effect. The
central slope uses the central `beta`. The low and high pair endpoints use both
ends of its uncertainty interval. The model then stores:

- the median central slope across independent stints;
- the minimum low endpoint and maximum high endpoint as a conservative
  empirical envelope; and
- the oldest completed tire age represented by the matched pairs.

An interval entirely above zero returns
`PASS_SHADOW_POSITIVE_DEGRADATION`. An interval crossing zero returns
`WAIT_DEGRADATION_SIGN_AMBIGUOUS`; an interval no higher than zero returns
`WAIT_POSITIVE_DEGRADATION_NOT_OBSERVED`. Only the first state has
`estimate_available=true`.

The fixed method id is
`fuel-adjusted-disjoint-pair-envelope-v1`. The model rejects non-finite values,
implausible per-lap slopes, duplicate evidence, fuel gains within a pair,
partial schemas, stale or crossed external pins, and any attempt to promote a
physical-wear claim.

## Action-bound performance tradeoff

`tire-performance-belief-v1` combines an identity-matched tire model, the
identity-matched pit/service calibration, an independently bound current-stint
context digest, and an exact future pit scenario.

For a proposed stop:

```text
age_at_pit = current_completed_tire_age + laps_until_pit
scale      = age_at_pit * laps_after_pit
keep_loss  = performance_age_slope_range * scale
```

The linear-age calculation compares the same future laps on the existing set
and a new set, so their shared future age cancels and the current set's age at
the stop remains. It is intentionally valid only inside the oldest calibrated
tire age; extrapolation returns `WAIT_TIRE_MODEL_EXTRAPOLATION`.

Incremental tire-service cost is derived from the same calibrated refuel and
tire times used by M2:

- sequential service: the full tire-change time;
- parallel service: `max(fuel_time, tire_time) - fuel_time`.

The complete uncertainty interval, rather than its midpoint, controls the
result:

- lower keep-loss above incremental service cost:
  `PASS_SHADOW_CHANGE_TIRES`;
- upper keep-loss below service cost: performance prefers keeping the set, but
  the result remains `WAIT_PHYSICAL_WEAR_FOR_NO_TIRE_SERVICE`;
- overlapping intervals: `WAIT_PERFORMANCE_SERVICE_TRADEOFF`.

The asymmetry is deliberate. Changing to a new set does not require an
assumption that an unknown old set is physically safe to retain. A no-tire
service does. Until a separate current physical-wear safety belief is designed,
calibrated, and accepted, this contract cannot promote `KEEP_TIRES` into a race
action.

## M2 v2 admission and decision gate

`offline-m2-strategy-context-v2` adds exactly two fields to the historical
context: `tire_performance_model` and `tire_stint_context`. The latter is built
from the same held collector descriptor and binds SDK-direct `SessionTick`,
`LapCompleted`, `PlayerTireCompound`, `TireSetsUsed`, and `OnPitRoad` evidence.
An age is available only from an observed pit exit or a captured zero-completed-
lap origin, with monotonic channel continuity. It never contains physical-wear
percentage.

The corresponding `offline-m2-strategy-receipt-v2` exposes a self-hashed,
audited `tire_strategy` surface:

- an exact official rule that requires tires yields
  `PASS_RULE_MANDATED_TIRE_CHANGE` without needing a performance model;
- an optional-tire rule requires the model, current stint context, matched
  pit/service calibration, and exact future action. Only
  `PASS_SHADOW_CHANGE_TIRES` becomes `PASS_MODEL_SELECTED_TIRE_CHANGE`;
- a performance preference to keep tires, an overlapping uncertainty interval,
  compound mismatch, extrapolation, missing model, or unavailable current stint
  remains a `WAIT` and produces no M2 recommendation.

Thus every newly issued v2 action has `change_tires=true`. There is deliberately
no v2 path that emits `change_tires=false` while current physical wear remains
unknown. The recommendation id and evidence list bind the tire-strategy semantic
hash and exact tire-strategy receipt. The advisor timeline independently
reconstructs model-based beliefs and rejects rehashed physical-wear promotion.

## Persistence and CLI

The model can be created with:

```powershell
.venv\Scripts\iracing-ai-engineer.exe build-tire-performance-model `
  matched-tire-performance-dataset.json `
  --expected-dataset-sha256 <independently-retained-sha256> `
  --output tire-performance-model.json
```

The input reader rejects duplicate JSON keys, non-finite numbers, oversized
inputs, and non-object roots. Output uses CreateNew semantics, validates the
readback, and never overwrites an existing artifact. Invalid evidence returns
`WAIT_TIRE_PERFORMANCE_MODEL` and creates no output.

The model is admitted to retrieved-live finalization only as an all-or-none
three-part input:

```text
--tire-performance-model <path>
--expected-tire-performance-model-sha256 <independent model digest>
--expected-tire-performance-source-receipt-sha256 <independent dataset digest>
```

The same triplet is required for object-exact bundle verification. Partial or
crossed pins fail before any output is created.

## Verification

Focused tests cover exact deterministic derivation, fuel-load uncertainty,
disjoint-pair and lineage rules, crossed pins, self-hash tampering, ambiguous
signs, model extrapolation, compound mismatch, sequential and parallel service,
full-interval decision boundaries, the physical-wear non-promotion invariant,
duplicate JSON keys, and CreateNew persistence.

The dedicated tire-model result is `14 passed`. Additional M2 v2,
advisor-timeline, retrieved-live, and CLI tests cover the integrated positive,
WAIT, lifecycle, replay, and tamper paths; current aggregate counts are recorded
in [the goal acceptance status](GOAL_ACCEPTANCE_STATUS.md).

## Evidence still required

Before this can affect an official-race strategy recommendation, the project
still needs:

- a real target car/track/series dataset with at least three independently
  labelled, condition-matched stints;
- a validated fuel mass-to-lap-time calibration with retained error;
- holdout and predicted-versus-actual coverage across temperature, setup,
  traffic, damage, and driver-pace changes;
- a separately validated physical-wear safety belief before any no-tire action;
- AI/Hosted shadow trials proving zero unsafe tire-service promotion.

No network connection, target-host operation, simulator start, pit-box change,
audio output, or vehicle control is performed by this slice.
