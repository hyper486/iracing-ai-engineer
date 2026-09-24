# Conditional live fuel-stop comparisons

Source implementation with synthetic verification, not an updated EXE or a
real in-car/VR acceptance result. This is a fuel-budget comparison, not a
validated pit-entry action, optimal strategy, tire-service plan or rejoin forecast.
It does not promote offline strategy evidence gates or fabricate sealed receipts.

## Driver-facing setup

In the native **模型与本地设置** page, while parked in your own car with fresh
telemetry, confirm the effective fuel capacity allowed for this car/event.
Optionally enter a refueling rate and a low/high pit-transit time-loss range.
The latter is loss relative to remaining on track, excluding stationary service;
it is not the total elapsed travel time through pit lane. Both bounds are required
together. These are explicit `USER_RULE` assumptions, not SDK measurements,
official event rules or calibrated pit evidence.

Configuration stays in memory for this source/player/session connection. It is
not persisted or automatically restored after disconnection, source/context
loss, session/player change or an analysis fault. Existing text in an input box
does not mean it has been reconfirmed. Applying or clearing does not restart the
SDK reader, reset fuel learning, change provider settings, write a credential
file or change the game's fuel/pit settings. There is no inferred tank capacity
from the maximum observed fuel level.

Use **比较补油**, or hold the configured PTT button and ask **比较进站方案**,
**进站窗口**, or **这次进站加多少油**. **该进站了吗** includes this comparison when
available; **还要几停** uses its explicitly discrete whole-lap count. These
questions are local and do not spend provider requests. Ordinary **还要加多少油**
retains its cumulative-to-finish meaning, not the next-stop dose.

The window displays both feasible boundary scenarios: laps from the question
position, fuel at that point, amount to add, target fuel, next-stint length and
further stops. If both rate and transit bounds are supplied, it also displays
transit loss plus pumping time. No tires, repairs, driver change, queue or other
stationary overhead is silently folded into this partial estimate. Speech is a
short fuel-only hypothetical budget, not an instruction to enter pit lane.
Missing pit permission and observed restrictive/penalty/caution states remain
explicit. The detailed text keeps assumptions and all unavailable capabilities.

## Calculation and admission

The actual current fuel observation must agree with the ready learned fuel
model. The publication must be fresh, full/in-car, source-bound and free of a
fuel-invalid interval. Only a bound `Race` session with a consistent finish
horizon can produce a comparison. Practice may configure assumptions, but its
timer does not become a race horizon; session changes require reconfirmation.
Default learning requirements are unchanged. Reserve comes from the existing
fuel model. Capacity contradictions, below-reserve fuel and unusable tank range
produce a fixed reason instead of scenarios.

The live and offline fuel estimators share the same integral-lap arithmetic.
With remaining budget `H`, fuel `F`, capacity `C`, reserve `R` and burn `B`:

- Current complete-lap range: `c = floor(max(0, F - R) / B)`.
- Full-tank complete-lap range: `t = floor((C - R) / B)`.
- If `H > c`, the discrete stop count is `n = ceil((H - c) / t)`; otherwise zero.
- First-stop interval: `max(0, H - n*t)` through `c` complete laps from now.
- At each interval endpoint `k`, choose a next stint of
  `max(1, H - k - (n - 1)*t)` laps and add enough to cover it plus reserve.
  Subsequent budgeted fills may use the full-tank range.

These complete laps start at the **question position**, not an identified pit
entry or lap line. Zero means the arithmetic boundary is now, not that a pit
entrance is reachable immediately. The discrete count can be more conservative
than a continuous fuel-volume lower bound. No time-optimal selection is claimed.
The horizon retains its SDK-laps or timer/fastest-lap-plus-margin provenance;
weather, pace and the leader can still change the real finish requirement.

Synthetic example: `F=20 L, C=50 L, R=2 L, B=2 L/lap, H=15` yields one stop,
between zero and nine complete laps from the question position. At zero laps,
12 L added targets 32 L for a 15-lap stint; at nine laps, the same 12 L targets
14 L for six laps. With `H=60`, the budget instead has three stops; the earlier
first fill is only 6 L for 12 laps, with two later fills, even though the total
finish deficit is 102 L. These are invented examples, not this user's race data.

## Publication, withdrawal and isolation

Only two endpoints are evaluated (one if identical), not a loop over a long
endurance horizon. The fixed-size calculation runs with the half-second state
publication, with no extra SDK read, network, file access or history buffer.
Configuration and publication share the mailbox lock, so an old configuration's
result cannot publish after applying a new one. This bounded arithmetic does
not claim hard real-time or GIL isolation.

Consumers recompute the numeric projection and check exact typed structure,
configuration, source/player/session, sequence and time. Extra text, altered
numbers and bool/int aliases are rejected. Provider contexts contain only fixed
fact templates; source hashes, player slots and configuration objects are not
forwarded. A projection exception is latched with a fixed visible/local-answer
fault; it does not stop fuel, driving or proximity. Clear/reapply or reconnect
is required to retry the faulty projection.

Comparison answers have the existing ten-second situation TTL. Config changes,
refueling, lap/source/pit-state changes, changed model/horizon/window or missing
selected evidence revoke old answers. A plan revision latches changes even if
the value returns before a consumer polls. Ordinary small fuel consumption
within an unchanged window does not continually cancel speech. Slow TTS may
still expire before completion; expired replies are not resumed. Spotter
interruption remains independent and higher priority.

Tests use invented numbers, a fake SDK with the actual normalizer/fuel model,
fake recognition/audio, and a hidden native Tk window. They cover multi-stop
arithmetic, zero/edge ranges, optional loss inputs, source/config invalidation,
tampering, fault containment, PTT cancellation and no-restart/no-write settings.
They do not prove real acquisition quality, hardware hearing, end-to-end latency,
full-race resource use or safe/optimal race timing. Live action-bound rejoin,
pit-entry geometry, event-rule/service integration, tire evidence, packaging and
real acceptance remain open under the unchanged [active goal](ACTIVE_GOAL.md).
