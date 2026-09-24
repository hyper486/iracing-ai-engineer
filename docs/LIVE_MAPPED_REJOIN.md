# Conditional native mapped rejoin

`live-mapped-rejoin-v1` connects the native app's fuel budget, source-bound user
assumptions and per-car observed lap shapes. It is an experimental conditional
forecast, not a calibrated confidence interval, optimal pit instruction, race
position, tire-service decision or authentic in-car acceptance result.

## Driver setup and questions

While parked with fresh owned in-car telemetry, open **模型与本地设置**. In
addition to effective tank capacity, the four right-hand fields are optional
as a group, but all are required for this forecast:

- Pit-entry and pit-exit fractions of the lap, in `[0, 1)`, distinct from each
  other. Use the correct track/layout and actual entry/exit merge positions.
- Lower/upper **complete net pit loss**, in seconds, relative to ordinary
  on-track travel between those positions. Include all expected stationary
  service, overhead and queues; the range must cover both compared fuel doses.
  Do not enter the left-hand transit-only loss or total pit-lane travel time.

These are explicitly `USER_RULE`, not SDK geometry, measured calibration or
verified event rules. No values are guessed. Settings are memory-only, bound to
this source/player/session, and must be reconfirmed after its existing reset
boundaries. Applying them neither restarts capture nor modifies game settings.

Ask **出站预测**, **进站后会落在哪**, **预测出站交通**, or use **问出站**. Exact
routine questions stay local and make zero provider requests. The full answer
shows each mapped entry distance, next-fill budget, subsequent fuel-stop count,
conditional front/rear time ranges and assumptions. Only supported rows contain
gaps; a far or ambiguous endpoint remains unavailable independently.

Short speech identifies the early/late hypothetical scheme and front/rear gap
ranges. Ranges round outward. When a lower bound is at least 30 seconds, speech
says **30秒外**, never a made-up point estimate. A range extending beyond 30
seconds from a nearer lower bound is described as broad. The exact intervals
remain in the text. This keeps common speech brief without weakening expiry.

## Evidence and calculation

The ready `Race` fuel model and its complete-lap finish horizon are prerequisites.
Player pit permission must be explicitly open; missing/read-error/restrictive
flags withdraw the forecast. No tire/service assumption is inferred from a pit
exit or a tire counter. This does not change the offline strategy gates.

The bounded motion tracker observes `LapCompleted + LapDistPct` for the player
and each currently on-track car. Each actor needs three observed finish-line
crossings yielding two complete 64-bin phase-time profiles. A tick gap over
0.25 seconds, rejected source, relevant read error, replay/spectator/pit state,
player incident, caution/penalty flag, missing on-track actor or impossible
progress withdraws affected evidence. The first partial lap is not a complete
profile. Pit/off-track/unlocated slots are counted as excluded, not declared
absent; future exits or rejoins by those cars are not predicted.

Completed lap brackets must be 5–1200 seconds with at most 25% duration spread.
The current lap's elapsed phase must fit the observed phase envelope within
the larger of one second or 1% of the longest observed lap. This is a model
validity guard, not a guarantee that future pace is unchanged. Stationary or
substantially changed pace cannot indefinitely reuse old complete laps.

The fuel budget still uses whole laps measured from the question position.
The new lane maps its earliest/latest feasible boundaries to actual reachable
user-specified **entry** positions. It recomputes fuel at those fractional
distances and rejects capacity/reserve contradictions. It does not optimize
the whole race or prove that this discrete budget is the minimum attainable
stop count for the actual pit geometry.

For each entry, projection is to its mapped **exit**, including wrap past the
finish line. Player counterfactual track travel uses interpolation in its
observed phase-time profile; complete net pit loss is then added. Opponent
future progress uses the inverse of its own phase-time profile. Both players'
two historical profiles, loss endpoints and bounded crossing/sampling errors
are enumerated. This is not instantaneous-speed extrapolation, nor a uniform
average-speed model that ignores slower corners.

Only entries within two laps are evaluated. Total forecast time including
service must not exceed three times the shorter observed player lap. At most
two scenarios are evaluated. Physical neighbors are selected by circular track
distance, including lapped/multiclass cars, not by classification or the
fastest arrival time. Their seconds are equivalent travel time along the track,
not an official timing-line gap. An envelope crossing physical overlap or
ambiguous nearest-neighbor order yields no numeric advice. Historical profiles
are an empirical scenario envelope, **not statistical forecast coverage**.

Future pit stops, weather, battles, yellow flags, fuel/tires and unmodeled pace
changes can invalidate this scenario. There is no "clear exit" guarantee.
Measured matched pit/service evidence, trusted geometry, complete event rules,
calibrated tire-performance integration and real race validation remain open.

## Bounded execution and withdrawal

One existing analysis owner holds at most 256 actors, each with two 65-point
profiles, three crossing brackets and a partial profile of at most 64 points.
No new SDK calls, disk history, network access or worker threads are added.
Projection runs outside the AppState lock; an old configuration's in-flight
result is discarded at publication. Motion faults and projection faults have
fixed visible/local spoken explanations without stopping the independent
Spotter. A projection exception is latched until parameter reapply/reconnect;
a motion-owner exception requires reconnect. Python/GIL isolation and hard
real-time latency are not claimed.

Consumers verify source/configuration/sequence/time, typed numeric shape and
an exact recalculation. Raw profiles, source hashes and car slots never enter
provider facts. Only fixed templates and their numbers may be selected by the
optional model; local questions do not wait for it.

Forecast answers retain the ten-second situation TTL and Spotter priority.
Configuration, fuel-window/horizon, source/lap/permission, continuity, selected
neighbor and material gap-band changes revoke old answers, including during
TTS. A publication revision latches transient changes even after recovery.
Ordinary opponent lap completion/profile refresh does not itself continually
cancel speech. Small movement inside an unchanged scenario/band can retain the
question-time answer until expiry; it is never represented as continuously
current. Cancellation does not resume stale speech.

## Verification boundary

Synthetic tests exercise actual normalization, profile completion, nonuniform
pace, wrap/lapped/multiclass order, ambiguity, service-time limits, fault
containment, delayed-job configuration races, question binding, fake PTT and
native settings/shortcuts. Frozen numerical tests require a locally answered
mapped forecast and retain the `SYNTHETIC`, `live_acceptance=false` labels.

An isolated 256-car synthetic projection averaged about 20.75 ms over ten
calls on the build machine. Three memory-only Chinese synthesis examples were
6.898, 8.117 and 7.937 seconds long. These are a small local compute diagnostic
and waveform lengths, not SDK/VR scheduling, response latency or hearing passes.
Other values, voices or rates can exceed the short answer deadline and will be
cancelled normally. No microphone or speaker was opened by these checks.
