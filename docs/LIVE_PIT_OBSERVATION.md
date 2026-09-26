# Native historical pit-visit observation

`live-pit-observation-v1` adds a local question and a reviewed setup draft to
the native app. It is advisor-only and explicitly **not calibrated future pit
loss**, surveyed geometry, service-content identification or live acceptance.

## Driver workflow

Keep AEIS connected before entering the pit road and throughout the visit.
After exiting, ask **这次进站用了多久 / 本次进站耗时 / 进站耗时**, or use
**问耗时**. This exact question bypasses DeepSeek. Speech gives the observed
pit-road elapsed interval, not a future stop recommendation. Detailed text
separately shows an available historical baseline/net-loss estimate and the
observed tank-level change. Tank change is not dispenser-delivered fuel.

For an optional draft, first drive enough continuously observed laps to obtain
three finish-line crossings and two complete own-car phase profiles, then make
one complete visit. In **模型与本地设置**, **从进站观测填写草稿** becomes available
only with fresh owned telemetry and speed at most 0.1 m/s, a usable historical
baseline, a positive bounded loss interval, and observed active service/stall
flags that are both inactive on exit. Missing service signals or a drive-through
can still give elapsed time, but not this draft. Those flags do not prove what
service was performed.

The button fills only the four editable rejoin fields. It does **not** apply
parameters, overwrite capacity/rate/transit-only fields, persist preferences,
restart capture or control the simulator. Review and edit it before clicking
**确认本次策略参数**:

- SDK pit-road transitions need not coincide with the actual entry/merge points.
  Draft positions are edge-bracket midpoints, not surveyed geometry.
- The estimate covers only the observed SDK boundaries. Deceleration before
  entry and acceleration after exit are outside it. Correct both the geometry
  and the corresponding loss range if using actual merge positions.
- Prior laps do not prove matching fuel load, tires, weather or traffic.
  Next service contents, fuel dose, queues and overhead can differ. The draft
  interval is not a calibrated confidence bound or guaranteed future cost.

Confirmation remains an explicit `USER_RULE` assumption; it does not become
an accepted offline calibration. Confirmation checks the draft's connection,
source, player/session and observation revision again under the state lock.
An obsolete draft or moving vehicle is rejected without replacing active
parameters. Clear the form or obtain a new draft after source/visit changes.

## Calculation and validity

The independent tracker uses direct own-car SDK position and `OnPitRoad` edges.
An entry is bracketed by the last outside/first inside points; exit uses the
last inside/first outside points. Road elapsed bounds subtract the outer and
inner time brackets. A visit must cover positive progress below one lap, last
at most 30 minutes, and have continuous monotone publication/session clocks with
gaps at most 0.25 seconds. The counters can have independent origins. Joining
inside cannot supply a missing entry.

SDK `approaching_pits` surface samples can precede and follow the `OnPitRoad`
edges. They support the edge brackets but never train clean phase profiles.
The preceding clean baseline is held for entry; returning to the main track
without entering starts a new baseline. An approach crossing the finish line
without a clean profile withholds the baseline, not the observed road elapsed time.

Progress normally cannot reverse. Only two consecutive directly observed
pit-stall samples at speed at most 0.5 m/s may settle within 0.1 m of projected
track progress behind the active visit's high-water mark. Conversion requires
exact-update-bound track length; absent/invalid length denies this exception,
and a length change discards the visit. This bounds total backward displacement,
not just each step. Raw positions are not clamped or rewritten. Moving/non-stall
reversal, excessive drift, invalid/missing speed for this exception, jumps and
clock gaps still discard the visit. Entry/exit edge validation remains strict.

The optional baseline uses two complete preceding 64-bin phase-time profiles.
The shared profile validator requires three bounded crossing windows, lap
durations 5–1200 seconds, at most 25% duration spread and a sufficiently recent
last crossing. It does not require the entry approach to match current racing
pace. Both historical profiles and all edge-position endpoints are enumerated;
sampling-error bounds expand track travel before subtraction from road elapsed.
Negative estimated losses stay negative and cannot produce a positive draft.
Displayed time ranges round outward. Neither baseline is called a matched
reference or evidence of physical tire condition.

Relevant read errors, source/session/player changes, replay/spectator context,
incidents, restrictive flags, invalid track surface, discontinuity or excessive
gaps clear the retained visit/result. Missing optional fuel does not disable
timing; missing service/stall evidence disables the service draft. Fuel-model
learning and opponent-position readiness are not prerequisites.

## Execution, privacy and withdrawal

One existing analysis owner holds a previous point, at most one active visit,
one completed record and two completed own-car profiles plus a partial profile.
There are no new SDK calls, threads, disk histories or cloud requests. A fault
is latched to a fixed visible/local spoken notice and does not stop independent
Spotter, fuel, coaching or rejoin analysis. Reconnect recreates the failed owner.

Consumers recompute the bounded numeric projection and verify typed values and
source/sequence/time/player/session bindings. Profiles and raw points never
enter provider facts: only fixed Chinese templates and numbers are available.
The optional model cannot omit the mandatory historical/non-calibration notice.
Current-source loss, observation revision changes, analysis invalidation and a
ten-second TTL withdraw old answers, including during synthesis. Transient
loss/recovery is latched. Ordinary fuel-learning or rival changes do not
continually cancel a standalone historical pit answer.

The observation is intentionally ephemeral. Existing opt-in private SDK capture
can retain the underlying signals for later review; this feature neither
creates an authentic capture nor silently saves calibration/service labels.

## Verification boundary

Synthetic tests cover finish-line wrap, exact edge intervals, missing baseline,
independent publication-clock origins, approach-edge ordering, bounded stationary
stall settling and rejection of cumulative drift or missing speed,
drive-through/missing flags, fuel uncertainty, negative loss, malformed data,
session/source discontinuity, bounded retention, fault isolation, local-query
TTL, fake PTT withdrawal, native draft/confirmation and no-save/no-restart guards.
The frozen self-test also runs an invented 1,921-frame visit through the real
analysis owner, local answer and explicit draft-confirmation path. It uses no
SDK transport, provider or physical audio device. This is not a real pit,
calibration, headset/VR or race-acceptance result.
