# Live strategy-context evidence

## Outcome

The current checkout now has the first source-bound bridge between a frozen R8
collector capture and the M2 race-strategy context.  It removes three artificial
`None` values from the retrieved-live analysis path without weakening any
recommendation gate:

1. a privacy-safe event identity is projected from the same validated capture;
2. the latest `SessionFlags` value is mapped to an explicit `CLEAR` or `ACTIVE`
   penalty state;
3. the latest validated `CarIdx*` arrays and the same-capture track length are
   reduced to a privacy-safe physical nearest-ahead/nearest-behind map; and
4. the final ten seconds of SDK-direct lap progress are reduced to a
   source-bound time-domain motion envelope for each eligible car index; and
5. SDK-direct tire compound, set count, completed laps, pit-road state, and
   session tick are reduced to a source-bound current-stint-age receipt.

This is analysis-only work.  It does not connect to Aeis, install a runtime,
change a Scheduled Task, start iRacing, emit audio, alter the pit black box, or
control the car.

## Fixed event-identity projection

`event-identity-context-v1` exports exactly nine values:

| Strategy field | Same-capture source |
|---|---|
| `series_id` | `WeekendInfo.SeriesID` |
| `season_id` | `WeekendInfo.SeasonID` |
| `race_week` | `WeekendInfo.RaceWeek` |
| `track_id` | `WeekendInfo.TrackID` |
| `car_class_id` | live telemetry `PlayerCarClass` |
| `event_type` | `WeekendInfo.EventType` |
| `track_config` | `WeekendInfo.TrackConfigName` |
| `sim_build` | `WeekendInfo.BuildVersion` |
| `official` | `WeekendInfo.Official` |

Each field carries `PRESENT`, `MISSING`, `INVALID`, or `UNAVAILABLE`.  The
projection is `AVAILABLE / VERIFIED` only when every field is present and
valid.  A partial or unavailable projection remains usable as evidence but
cannot make the official-rules selector pass.

Within one collector session epoch, two different valid values for the same
identity field are rejected.  A session reset clears the identity epoch before
the new `session_info` transaction is admitted.  The evidence object is
self-hashed and bound to the complete validated collector-input evidence hash.
The second parse must reproduce it exactly before normalized samples are
accepted.

## Privacy boundary

The adapter still never returns raw `SessionInfo` and never returns any
`DriverInfo` subtree.  The fixed projection contains no driver name, customer
ID, team, setup, car number, voice/radio, or free-form metadata.  Player class
does not require relaxing redaction: R8 already records the direct
`PlayerCarClass` telemetry channel when iRacing exposes it.

The retrieved-live bridge performs another path-free admission of the same
held capture descriptor.  It requires the capture SHA-256 and byte size to
match the producer proof, requires the collector evidence identity to match
the producer's `SDK_LIVE` source binding, and requires the last decision tick
to be object-equal to the producer proof.  Only then may M2 use provenance
`SDK_DIRECT_SAME_SOURCE_CAPTURE`.

## Penalty-state boundary

The bridge reads only the latest normalized `SessionFlags` field.  When that
field is `PRESENT` with `SDK_DIRECT` provenance, black flag, disqualification,
mandatory-repair/meatball, or invalidated-scoring bits produce `ACTIVE`; their
absence produces `CLEAR`.  Missing or invalid flags produce `None`, so the M2
gate remains `WAIT_PIT_OPEN_AND_PENALTY_STATE`.

The derivation is conservative.  It does not infer penalty service, remaining
penalty time, drive-through versus stop-and-go, or whether a penalty has been
served.

## Traffic-observation boundary

`traffic-observation-context-v1` is built by the collector adapter during both
validation passes.  It is bound to the complete collector-input evidence hash,
the separately validated `track-context-v1` hash, and the latest direct
`SessionTick`.  It consumes only:

- player `LapCompleted`, `LapDistPct`, and `PlayerCarIdx`;
- opponent `CarIdxLapCompleted`, `CarIdxLapDistPct`, `CarIdxOnPitRoad`, and
  `CarIdxTrackSurface`; and
- `WeekendInfo.TrackLength` from the already privacy-filtered track context.

Only cars with `CarIdxTrackSurface == 3`, `CarIdxOnPitRoad == false`, and a lap
fraction in `[0, 1]` enter the physical map.  Inactive/off-track/pit-road slots
are counted with explicit exclusion reasons.  The adapter converts lap
fractions to deterministic billionths of a lap and distances to integer
millimetres, then records the nearest car index ahead and behind on the circular
track.  It also retains an optional race-lap delta when both direct completed-
lap counters are present.

This evidence contains no driver name, customer ID, team, car number, or raw
`DriverInfo`.  More importantly, it is **not** a post-pit prediction.  Current
spatial distance cannot be relabelled as time gap, and a pit-road elapsed
duration cannot be relabelled as counterfactual pit loss. Therefore the
retrieved-live M2 context retains an observation-only `traffic_rejoin` receipt.
Without a matched calibration it is:

```text
estimate_available=false
status=OBSERVED_ONLY_WAIT_PIT_LOSS
rejoin_gap_range_s=null
```

M2 reports `WAIT_REJOIN_ESTIMATE` with
`PIT_LOSS_CALIBRATION_REQUIRED_FOR_REJOIN_ESTIMATE`. Only a separately
identity-bound, matched pit-loss/service model may close that first blocker.

The checkout now includes the separately documented
[`matched-pit-calibration-dataset-v1`](MATCHED_PIT_CALIBRATION.md) builder and an
optional retrieved-live admission path. When a model and both independent pins
match the same-capture identity, M2 advances the pit-loss and service-label
capabilities to `PASS`, while the traffic input becomes:

```text
estimate_available=false
status=OBSERVED_ONLY_WAIT_REJOIN_MODEL
rejoin_gap_range_s=null
```

When the same capture also provides a valid motion window, the input advances
one more honest step:

```text
estimate_available=false
status=OBSERVED_ONLY_WAIT_ACTION_BOUND_REJOIN
motion_context_sha256=<same-capture motion receipt>
rejoin_gap_range_s=null
```

The remaining reason is `ACTION_BOUND_REJOIN_ESTIMATE_REQUIRED`. Calibration
and motion alone cannot promote the input because the result must still be
bound to the exact M2 fuel amount, tire choice, and sequential/parallel service
rule.

M2 now closes that final synthetic-contract step. Once all other gates produce
an exact action, it invokes the separately documented
[`time-domain-rejoin-estimate-v1`](TIME_DOMAIN_REJOIN_ESTIMATE.md) projection.
A stable bracket advances `traffic_data` to `PASS_TRAFFIC_DATA`; a zero-crossing
or overlapping neighbor order remains `WAIT_AMBIGUOUS_REJOIN_ORDER` and emits
no position claim.

## Current tire-stint boundary

`tire-stint-context-v1` is derived during the same held-descriptor pass as the
event, penalty, and traffic evidence. It uses only direct `SessionTick`,
`LapCompleted`, `PlayerTireCompound`, `TireSetsUsed`, and `OnPitRoad` fields. A
usable origin is either a captured zero-completed-lap start or an observed
pit-road exit. Missing channels, partial current values, non-monotonic ticks or
laps, an unexplained set/compound change, or a current on-pit-road state remains
WAIT/INVALID rather than guessing an age.

The receipt is tied to the same identity, input-evidence hash, and decision tick
as the M2 context. It deliberately fixes current physical wear to
`SKIP_CURRENT_PHYSICAL_WEAR`; stint age and historical lap-time degradation are
not tread percentage. `offline-m2-strategy-context-v2` embeds this receipt and
an optional independently pinned `tire-performance-model-v1`. See
[`TIRE_PERFORMANCE_BELIEF.md`](TIRE_PERFORMANCE_BELIEF.md) for the model and
action-bound service decision.

## What this unlocks

For a complete synthetic `SDK_LIVE` capture, the retrieved-live bundle now
persists all nine event-identity values in its M2 strategy context, retains the
same-capture provenance, and changes the dynamic safety gate to
`PASS_PIT_OPEN_AND_PENALTY_STATE` when `PitsOpen=true` and the penalty state is
`CLEAR`.  The same fixture also persists the self-hashed physical traffic-map
revision and advances the traffic blocker from missing data to
`WAIT_REJOIN_ESTIMATE` when calibration or official action evidence is absent.
A second complete synthetic fixture supplies identity-matched calibration and
contract-only rules that require tires, produces an action-bound stable rejoin
bracket, advances `traffic_data` and `tire_strategy` to PASS, persists one
shadow recommendation, and replays the engineer session, report, HTML, and
bundle object-exactly. A third fixture uses an optional-tire rule and a pinned
synthetic performance model to reach `PASS_MODEL_SELECTED_TIRE_CHANGE`, then
replays the same four artifacts exactly. These fixtures are contract proof and
are not accepted real-race evidence.

This deliberately does **not** create a pit recommendation by itself.  The
following independent gates still remain:

- exact official event-rules profile and source digest;
- a real accepted matched pit-lane loss, refuel-rate, tire-time, and
  service-label dataset (the builder exists, but only synthetic fixtures pass);
- a real accepted condition-matched tire-performance dataset and holdout
  coverage; plus a separate current physical-wear safety belief before any
  no-tire recommendation;
- real predicted-versus-actual rejoin labels and calibrated interval coverage
  for the implemented source-bound estimator;
- enough clean laps for the fuel model and distance horizon;
- repeated condition-matched driving evidence and accepted labels; and
- real `SDK_LIVE` acquisition on Aeis.

## Verification

Focused checks:

```powershell
.venv\Scripts\python.exe -m pytest `
  tests\test_adapters.py `
  tests\test_pit_calibration.py `
  tests\test_rejoin_estimator.py `
  tests\test_tire_performance_model.py `
  tests\test_retrieved_live_analysis.py `
  tests\test_offline_m2_strategy_receipt.py `
  tests\test_advisor_timeline.py -q

.venv\Scripts\python.exe -m ruff check `
  src\iracing_ai_engineer\adapters.py `
  src\iracing_ai_engineer\retrieved_live_analysis.py `
  src\iracing_ai_engineer\m2_strategy.py `
  src\iracing_ai_engineer\advisor_timeline.py `
  tests\test_adapters.py `
  tests\test_retrieved_live_analysis.py `
  tests\test_tire_performance_model.py
```

The regression set covers complete projection, explicit missing fields,
same-epoch class conflict rejection, `DriverInfo` redaction, clear penalty,
active black flag, nearest-ahead/nearest-behind distance, required-array
failure, observation-versus-estimate crossing attacks, held-descriptor source
closure, current tire-stint origin/continuity, model versus wear separation,
action-bound tire service, rehashed wear-promotion rejection, and the unchanged
advisor-only/no-control boundary.

## Release status

The historical analysis-v1 release remains immutable with its documented v1
`WAIT` behavior. The current implementation is frozen separately at
`C:\Code\iracingEngineer\deliverable\retrieved-live-analysis-release-20260902-v2`;
its external identity file SHA-256 is
`4368dc3d3938f7b539aded361406e0a609e76fa54f97d473420347195a631c47` and
its package-external verifier returned `PASS_OBJECT_EXACT_RELEASE_CLOSURE`.
The corresponding local embedded-runtime proof identity SHA-256 is
`a60c00570239a0ee0026ab8a964f6c128aea6b7b260411c5f74586eb867eb923`.
No v1 file or frozen collector-v7 file was modified, and neither v2 PASS is a
target-host or real-live claim.
