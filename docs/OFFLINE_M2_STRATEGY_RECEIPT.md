# Offline M2-A strategy receipt

`offline-m2-strategy-receipt-v1` remains the historical, replay-compatible
post-admission contract. The frozen analysis-v2 source now emits
`offline-m2-strategy-receipt-v2`, which adds an action-bound tire-service gate
without changing any frozen v1 artifact. Both implementations live in the
installable `iracing_ai_engineer.m2_strategy` module, alongside the package
`pit_plan` validator; the documented scripts are compatibility wrappers. This
work does **not** complete M2, attest the live/R8 path, or provide a race-ready
recommendation.

The compatibility entry point is [`scripts/build_offline_m2_strategy_receipt.py`](../scripts/build_offline_m2_strategy_receipt.py).
It consumes an already admitted `fuel-model-replay-v2` receipt, the frozen
`offline-m1-pit-stint-v1` receipt, and an independently hashed decision
context. An optional official-rules profile and an optional previous M2-A
receipt exercise the positive and lifecycle paths only when every gate closes.

## Safety boundary

Every output is:

- `advisor_only=true`;
- `execution_mode=SHADOW_ONLY`;
- `attestation_status=NOT_R7_ATTESTED`;
- `derivation_status=POST_ADMISSION_PACKAGE_EXTERNAL`;
- self-bound by `m2_strategy_receipt_sha256`;
- limited to zero or one recommendation, whose `executable` field is always
  `false` and whose status is always `SHADOW_ONLY`.

The implementation does not import into or modify the frozen r7 verifier,
live collector, Windows bundle, deployment receipt, or existing
`offline-pit-plan-v1` contract. A positive synthetic result is only
`PASS_SHADOW_CONTRACT`; it is not M2 acceptance or permission to speak during
an official race.

## Bound inputs

The receipt closes the following lineage before strategy logic runs:

| Input | Independent trust root | Required closure |
|---|---|---|
| Fuel replay | expected `fuel_replay_sha256` | exact schema, self hash, ready model, PASS quality, source evidence, scenario, model and event closures through the existing pit-plan validator |
| M1 pit/stint | expected `pit_stint_receipt_sha256` | exact top-level schema, self hash, advisor/shadow boundary, empty recommendations, and UNKNOWN service contents |
| Strategy context | expected `context_sha256` | exact versioned schema and self hash; v2 also validates the identity/tick/source-bound tire model and current-stint receipt |
| Official rules, when present | expected `profile_sha256` plus an independently supplied official source-document SHA-256 | exact profile schema, self hash, source authority and selector |
| Previous M2-A state, when present | expected previous self hash plus expected previous revision | exact schema, self hash, same source/session/epochs, and strictly increasing decision tick |

Fuel, M1, and context must have identical:

- raw `source_sha256`;
- `source_kind`, `source_id`, and `session_id`;
- normalized sample SHA-256 and sample count;
- shared telemetry-event receipt SHA-256.

The M2-A output repeats those identities, both upstream receipt hashes, the
fuel semantic-model hash, and the context hash under `input_binding`. A
separate `input_lineage_sha256` closes that set. It also embeds the complete,
exact admitted context under `strategy_context`, so the decision tick,
dynamic state, horizon inputs, and every layered model remain independently
inspectable instead of being represented only by a digest.

## Deliberately separate evidence layers

The context keeps these domains separate:

1. `event_identity`: same-source SessionInfo identity and its provenance;
2. `official_rules`: an optional exact-selector profile with a separately
   admitted official source digest;
3. `vehicle_context`: tank capacity and its provenance;
4. `calibration_model`: matched pit-loss and service calibration, when it
   exists;
5. `strategy_policy`: reserve, conservative quantile, and deterministic
   selection policy;
6. `traffic_rejoin`: a same-identity, same-decision-tick rejoin estimate;
7. `tire_performance_model`: an optional identity-bound, fuel-adjusted
   historical performance-age envelope; and
8. `tire_stint_context`: a required v2 same-capture current-set origin, age,
   compound, set count, and pit-road state, explicitly excluding physical wear.

The observed session `official` field is descriptive event identity. It is
not copied into `official_event_rules` and cannot promote a rules profile.
Official-rule status is derived only after all of these checks succeed:

- `source.authority == IRACING_OFFICIAL`;
- the profile's document SHA-256 equals the independently supplied source
  digest;
- the profile selector exactly equals the complete event selector;
- series, season, track, and car-class IDs in an official selector are
  positive; race week is non-negative;
- the profile is self-hashed and its independently supplied profile digest
  matches.

An extra caller-owned `official_event_rules: true` field is invalid schema.
Missing, incomplete, or mismatched identity remains
`WAIT_EVENT_RULES_IDENTITY`.

## Distance and one-more-lap branches

For a lap-limited event, a non-sentinel `laps_remaining` produces one
`LAPS_EXACT` branch. The observed iRacing `32767` sentinel produces
`WAIT_ONE_MORE_LAP_DATA`, not a 32,767-lap projection.

For a timed event, the builder needs all of the following:

- a verified exact-match official rule with
  `finish_rule=TIMED_LEADER_CROSSING`;
- `player_is_leader=true`;
- non-negative time remaining and leader ETA to the next crossing;
- a positive reference-lap time.

It then publishes two explicit synthetic branches:

```text
BASE = 1 + max(0, ceil((time_remaining - leader_eta) / reference_lap_time))
ONE_MORE = BASE + 1
```

Missing data, the observed `-1` time sentinel, a non-leader player, or an
unverified finish rule returns `WAIT_ONE_MORE_LAP_DATA`. The one-stop action,
when otherwise admissible, uses the latest common fuel-feasible pit lap and
the largest fuel addition across every admitted branch. No branch is silently
dropped.

## Calibration and traffic gates

The real M1 receipt observes elapsed pit-road, stall, and service-active time,
plus a tank-level endpoint difference. M2-A republishes those values only as
`OBSERVED_SAMPLE_ONLY`. It explicitly sets these M1-derived fields to null:

- counterfactual pit-lane loss;
- refuel rate;
- service-content model.

M1 elapsed pit time is not a matched pit-loss baseline. `PitstopActive` does
not label tires, fuel delivery, driver swaps, or repairs. A tank-level
endpoint difference is not delivered-fuel truth. Without a separate,
self-hashed model containing at least three matched samples, an uncertainty
interval, a refuel rate, tire time, and service-label availability, the gates
remain `WAIT_MATCHED_PIT_LOSS_BASELINE` and `WAIT_SERVICE_LABELS`. The checkout
now supplies a strict [matched calibration builder](MATCHED_PIT_CALIBRATION.md),
but no accepted real dataset has been provided.

Traffic must be self-hashed, identity-bound, and observed at the exact current
decision tick. Without any traffic evidence the result is `WAIT_TRAFFIC_DATA`.
An observation-only current physical map is retained with
`estimate_available=false`, but remains `WAIT_REJOIN_ESTIMATE`: current
ahead/behind distance is not a post-pit time-gap prediction. An admitted matched
pit-loss/service model without motion evidence advances the reason to
`REJOIN_ESTIMATOR_REQUIRED`. With a valid same-capture motion envelope the
remaining reason is `ACTION_BOUND_REJOIN_ESTIMATE_REQUIRED`. The analysis-v2
candidate now binds that motion, the calibrated loss range, and the exact M2
fuel/tire service action through
[`time-domain-rejoin-estimate-v2`](TIME_DOMAIN_REJOIN_ESTIMATE.md). Only a
stable neighbor bracket promotes the capability to `PASS_TRAFFIC_DATA`; a
zero-crossing or overlapping order remains `WAIT_REJOIN_ESTIMATE`.
Pit-open and penalty state are checked independently; stale/reset/schema-change
state, closed or unknown pits, and active or unknown penalties all block a
candidate.

## Tire-service gate in v2

The v2 context and receipt are documented in
[`TIRE_PERFORMANCE_BELIEF.md`](TIRE_PERFORMANCE_BELIEF.md). The gate keeps
historical performance loss separate from the physical condition of the fitted
set. A verified official rule can require a tire change directly. Otherwise,
the exact proposed pit lap, post-pit horizon, fuel addition, sequential/parallel
service timing, matched service calibration, current completed tire age, and
performance-slope interval are combined in `tire-performance-belief-v1`.

Only a lower-bound performance loss greater than incremental tire-service time
can produce `PASS_MODEL_SELECTED_TIRE_CHANGE`. If the model prefers keeping the
set, M2 returns `WAIT_PHYSICAL_WEAR_FOR_NO_TIRE_SERVICE`; it never emits a v2
`change_tires=false` action without a separate physical-wear safety contract.
Missing model, unavailable stint origin, compound mismatch, extrapolation, or
an overlapping interval likewise remains WAIT. The top-level `tire_strategy`,
capability status, recommendation semantic basis, and evidence id are all bound
into the receipt. Advisor-timeline admission reconstructs the model-based belief
and rejects rehashed wear promotion.

## Lifecycle

The first output has `state_revision=1`. Every accepted previous state must:

- match an independent previous receipt hash;
- match the caller's expected previous revision;
- preserve source, session, source epoch, and session epoch;
- precede the current `decision_tick` strictly.

The next revision is exactly previous revision plus one. The lifecycle emits:

- `ISSUE` for a new semantic recommendation ID;
- `NO_CHANGE` when the admitted semantic action is unchanged;
- `REVOKE`, followed by `ISSUE` when appropriate, when an active plan changes;
- `REVOKE` without replacement when any hard gate becomes WAIT or BLOCKED.

This is optimistic concurrency, not a hidden global state store. The caller
must supply the latest independently trusted previous hash and revision. A
wrong hash, wrong revision, epoch change, or non-monotonic tick fails closed.
Stale current data cannot restore or retain a recommendation.

## Current Audi/Spa receipt

The pinned 151,892-frame Audi R8 LMS Evo II GT3 × Spa input deterministically
produces **no recommendation**. Its context binds the same raw, normalized,
event, source, and session identities as the fuel replay and M1 receipt.

Observed context used for this finite receipt:

- source `public-audi-r8-evo2-spa`, session
  `public-fixture-2023-12-race`, kind `IBT_OFFLINE`, sample count `151892`;
- event: Race; track ID `163`; `Grand Prix Pit`;
- series ID `0`, season ID `0`, race week `0`, car-class ID unavailable;
- observed session official flag `false`;
- sim build `2023.12.12.04`;
- decision tick `332490`, 17 completed laps, pits open;
- source/session epochs `1/1`; stale, reset, and schema-change flags false;
- penalty state unavailable;
- timed-race value `154.71901666026497 s` and laps sentinel `32767`, but leader crossing ETA,
  reference-lap admission, and leader status are unavailable/not satisfied;
- vehicle tank capacity `120.0 L` with `USER_RULE` provenance;
- strategy policy: 0.9 conservative quantile, 1.0 L reserve, and
  `LATEST_COMMON_FUEL_FEASIBLE` selection;
- no official event-rules profile, matched calibration, service labels, or
  rejoin-traffic input.

The exact required blockers are present:

```text
WAIT_EVENT_RULES_IDENTITY
WAIT_ONE_MORE_LAP_DATA
WAIT_MATCHED_PIT_LOSS_BASELINE
WAIT_SERVICE_LABELS
WAIT_TRAFFIC_DATA
WAIT_PIT_OPEN_AND_PENALTY_STATE
```

Additional `WAIT_STRATEGY_DATA` and `race_recommendation=BLOCKED` make the
absence of an action explicit. The real receipt also preserves these M1 facts:

| Observed-only M1 value | Value |
|---|---:|
| Pit-road elapsed | 34.0 s |
| Stall elapsed | 10.1 s |
| Service-active elapsed | 9.333333 s |
| Tank endpoint difference | +20.995004 L |

None is promoted to pit loss, refuel rate, delivered fuel, or service content.

Frozen receipt evidence:

| Item | SHA-256 / result |
|---|---|
| Builder | `c06b44e83e58c6ec16bbf3ae56675d39996054129cf6b12488d0b2d1f22a0f88` |
| Focused test module | `cb7775a338d4793c79a3b2af409b0ad23e1a92a2dd85694812d14cfcf875ebc3` |
| Raw IBT | `754d14e6e2870eb00d42368e36c7a15495cb68bfd5f829ab08665d0f95fc7f36` |
| Normalized samples | `f38d336a4d647886c0a4b8fce32fc4d53ba688b26b7c46d70e780e85c012b07e` |
| Shared event receipt | `6abc981e717fe40311147979db6a5ae0b9d6a44658fabf5ec798af1ffc692800` |
| Fuel replay | `1f3b642c43dd6b7cd16e433dee3f26335f9aecd0c83950e02b706a2f79c3a65c` |
| M1 receipt | `76a7cec5cf255cd1d7f8fb9e46847b3cae515c8ad3c14acccfffdb0280b906d9` |
| Strategy context | `f337bc6ea9a43f1c01656a4a692d59fb3f84e62a4eff2d84388075262124f87a` |
| Serialized strategy context | 1,416 bytes; `ca4c456ac4421a97827acb5d4f2d15dc8c542be5efd59c026d8f80a95b2a78a2` |
| M2-A self-bound receipt | `72e5265ca6aea84c8d747640bf2cd0a99a2a6430817ccd0163121f4a8a973fb4` |
| Serialized M2-A artifact | 6,060 bytes; `1726ea834c6c75b794ba17494e529a26387db12b356bcd38d2533cf866ed8cee` |
| Determinism | byte-identical under `PYTHONHASHSEED=1` and `987654` |
| Focused tests | 29 passed, including the real Audi/Spa integration |
| Unchanged upstream regressions | 128 passed across pit-plan, M1 pit/stint, events, and adapters |
| Independent challenge review | `PASS / NO_BLOCKER`; 29 focused, 128 upstream, and 8 additional read-only attacks independently checked |

The generated local receipt is
`data/derived/audi-spa-offline-m2-strategy-v1.json`. Generated artifacts and
the raw IBT remain local/ignored evidence rather than a redistributable source.

The current v2 tests additionally cover rule-mandated tire changes,
model-selected changes, missing-model WAIT, physical-wear blocking of a
keep-tire preference, sequential/concurrent service, CAS continuation, crossed
identity, self-hash tampering, and independent advisor reconstruction. Current
aggregate counts are maintained in
[`GOAL_ACCEPTANCE_STATUS.md`](GOAL_ACCEPTANCE_STATUS.md).

## CLI behavior

```text
build_offline_m2_strategy_receipt.py FUEL_REPLAY M1_RECEIPT STRATEGY_CONTEXT
  --expected-fuel-replay-sha256 SHA256
  --expected-m1-receipt-sha256 SHA256
  --expected-strategy-context-sha256 SHA256
  [--rules-profile PROFILE]
  [--expected-rules-profile-sha256 SHA256]
  [--expected-rules-source-sha256 SHA256]
  [--previous-receipt RECEIPT]
  [--expected-previous-receipt-sha256 SHA256]
  [--expected-previous-revision N]
  [--output NEW_PATH]
```

Exit `0` means every contract gate closed for a synthetic/non-live shadow
candidate. Exit `5` means a structurally valid WAIT/BLOCKED receipt. Exit `3`
means invalid input, lineage, trust root, lifecycle, or output admission. The
output path is created exclusively and is never overwritten.

## What still must WAIT

This contract does not satisfy the M2 completion condition. The following are
still required before calling M2 complete or enabling official-race advice:

- one authoritative target-series rules document and exact event selector;
- same-event timed-race/leader-crossing evidence for the one-more-lap branch;
- matched no-stop versus pit-stop baselines for pit-lane loss;
- human- or independently verified service-content labels and service-time
  calibration;
- a real, independently labelled matched-stint tire-performance dataset with
  holdout interval coverage;
- a separately validated current physical-wear safety belief before any
  no-tire-service recommendation;
- same-identity opponent/traffic inputs and measured pit-exit-gap error;
- live pit-open, penalty, stale/reset, latency, and recommendation-expiry
  evidence;
- historical plus AI/Hosted sessions proving zero fuel-out and zero
  rules-violating recommendations at the configured high-quantile target;
- independent live admission and r7 attestation;
- M4 renderer/TTS/audio/session review before any official-race speech.

Until those gates are closed, the current Audi/Spa state remains a useful
negative receipt: it proves that missing evidence stays visible and produces
no recommendation.
