# Time-domain rejoin estimate

## Outcome

The frozen analysis-v2 source now has an action-bound rejoin estimator.
It converts the last ten seconds of same-capture SDK-direct player/opponent lap
progress into conservative rate envelopes, combines those envelopes with the
matched pit/service calibration and the exact M2 fuel/tire action, and returns
the nearest stable car-index bracket ahead and behind the predicted rejoin.

The implementation does not turn a current distance into a time gap by label
alone. It requires a separately self-hashed `traffic-motion-context-v1`, an
identity-matched `matched-pit-service-median-v1` calibration, and the exact
fuel amount, tire choice, and service-timing rule used by the M2 action. The
result is a self-hashed `time-domain-rejoin-estimate-v1` object and remains
advisor-only.

## Same-capture motion evidence

The retrieved-live bridge builds motion evidence while replaying the already
held, externally pinned collector descriptor. It consumes only fields already
present in the frozen R8 capture:

- SDK-direct `SessionTime` and `SessionTick`;
- player `PlayerCarIdx`, `LapCompleted`, `LapDistPct`, and `OnPitRoad`; and
- opponent `CarIdxLapCompleted`, `CarIdxLapDistPct`, `CarIdxOnPitRoad`, and
  `CarIdxTrackSurface`.

Driver names, customer IDs, teams, car numbers, and raw `DriverInfo` remain
outside the analytical plane. Cars in the pits or off the racing surface are
not used for motion fitting.

The estimator retains at most the latest ten seconds. A session-time
regression clears the window. Player and opponent rates require at least five
points and at least two seconds of positive, finite progress. Endpoint slopes
from every qualifying start point to the latest point form an empirical rate
range; the median is retained as the central rate. Rates above `0.2 laps/s`
fail the sanity gate.

The motion receipt binds:

```text
decision_tick
event identity SHA-256
source evidence SHA-256
latest physical traffic-map SHA-256
player rate and range
sorted opponent car indexes, rates, and signed lap deltas
motion SHA-256
```

An unavailable or too-short window remains `WAIT_TIME_DOMAIN_MOTION`; it is not
silently replaced by the latest spatial snapshot.

## Action-specific projection

For the action selected by M2, stationary service time is reproduced from the
calibrated refuel rate and tire-change time:

```text
fuel_service_s = fuel_add_l / refuel_rate_l_per_s

SEQUENTIAL:
stationary_service_s = fuel_service_s + tire_change_s

PARALLEL:
stationary_service_s = max(fuel_service_s, tire_change_s)
```

The full loss interval is:

```text
total_pit_loss_range_s =
  pit_lane_loss_uncertainty_s + stationary_service_s
```

A positive signed lap delta means that the opponent is currently ahead. Its
current time-gap interval uses the player's observed rate range. A negative
delta means that the opponent is behind and uses that opponent's rate range.
The action-specific pit-loss interval is then added to every signed current
gap. Positive projected values are ahead of the rejoining player; negative
values are behind.

The estimate becomes `AVAILABLE_STABLE_BRACKET` only when:

- no opponent's interval crosses zero;
- the closest-ahead interval cannot swap order with another ahead interval;
- the closest-behind interval cannot swap order with another behind interval;
  and
- at least one stable neighboring car exists.

If a car can fall on either side of the player, or candidate neighbor ranges
overlap, the estimator returns `WAIT_AMBIGUOUS_REJOIN_ORDER`, preserves the
reason codes, and emits no neighbor claim. It never resolves a tie by car
index.

## M2 integration

M2 now derives its action before promoting the traffic capability. The same
fuel addition, tire choice, calibrated stationary time, and official
fuel/tire service ordering are passed to the rejoin estimator. A passing
estimate is persisted under `traffic_rejoin.estimate`, and the recommendation
binds both its exact digest and a tick-independent semantic digest.

This preserves lifecycle behavior: a new observation tick with the same
rates, action, and bracket refreshes evidence without inventing a different
recommendation identity. A changed rate/bracket/action changes the semantic
basis and triggers the existing revoke/issue path.

The older frozen WAIT receipt shape remains readable. A motion-derived result
requires the extended output shape and cannot be relabelled as a legacy
traffic pass.

## Verification

The focused suite covers:

- exact motion/source/map/identity/tick binding;
- deterministic action-specific fuel and tire service timing;
- stable nearest-ahead and nearest-behind selection;
- zero-crossing and overlapping-order WAIT behavior;
- external-digest rejection after total rehashing;
- same-capture motion extraction from a sealed synthetic `SDK_LIVE` capture;
- M2 recommendation and evidence binding;
- lifecycle stability across observation-tick refreshes;
- advisor-timeline independent reproduction; and
- object-exact engineer-session, JSON report, HTML, and bundle replay.

The current focused rejoin/M2/retrieved-live/advisor-timeline result is
`89 passed, 2 skipped`. Both skips are pre-existing missing public Audi/Spa
artifacts, not rejoin-estimator failures.

## Acceptance boundary

This closes the implementation and synthetic end-to-end proof for the
time-domain rejoin slice. It does not yet establish real-race accuracy. The
current rate envelope assumes the short-window progress behavior remains
useful through the stop; it does not model a safety car, class-specific pace,
traffic battles, pit-entry/exit geometry, a driver swap, weather change, or a
future pace discontinuity. The empirical pit-loss min/max is not a validated
high-quantile coverage guarantee.

Real acceptance still requires repeated user-owned or explicitly authorized
sessions with retained predicted-versus-actual rejoin labels, condition
coverage, calibration of interval coverage, and zero unsafe promotions. Until
then, the synthetic passing path remains an analysis-v2 contract proof, not an
accepted live race recommendation.

No part of this implementation connects to Aeis, starts iRacing, emits audio,
changes a Scheduled Task, mutates the pit black box, or controls the vehicle.
The frozen collector v7 and analysis v1 releases remain unchanged.
