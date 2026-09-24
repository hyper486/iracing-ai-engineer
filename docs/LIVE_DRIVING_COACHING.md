# Incremental recent-lap coaching

Source implementation, not an updated EXE or an in-car acceptance result.
The native engineer now feeds complete laps into the existing distance-domain
driving model and answers an explicit driving question without a cloud round trip.
It does not automatically narrate every corner or prescribe a racing maneuver.

## Use and meaning

The native **问驾驶** button, or a PTT question such as **哪里可以改进？**,
**哪里丢时间？** or **我该练什么？**, uses local evidence. Whisper still has to
recognize the spoken question; DeepSeek is not required for these exact questions.
Mixed or explanatory questions retain the constrained, fact-selecting planner.

With enough comparable evidence, the answer identifies one automatically
numbered braking/corner segment, its approximate distance from start/finish,
observed median loss against an actual reference lap, and a practice hypothesis:

| Repeated observation | Practice hypothesis, not a guaranteed gain |
|---|---|
| Longer coast between lifting and braking | Explore a shorter coast, without automatically delaying braking |
| Later braking together with slower exit | Try slightly earlier braking and observe exit speed |
| Throttle pickup followed by a second lift | Explore more progressive throttle and observe whether the second lift reduces |

`C01`, `C02`, etc. are model-generated segments, **not official track corner names**.
Location comes from the reference braking zone, not a commanded braking marker.
Loss is observed in the admitted evidence; it is not the time this practice will
necessarily recover. The model must see a repeated pattern in the **latest
completed lap**. A lone mistake or a newer lap without that pattern does not
keep an old recommendation alive. No supported point means an explicit wait,
rejection or no-repeat explanation, not an invented recommendation.

The desktop shows coaching health independently of fuel/traffic. Missing
required channels, source continuity, geometry, rejected laps, buffer limits
and a latched model fault have fixed local explanations. On a model fault,
reconnect to retry; the fuel, traffic and urgent Spotter lanes continue.

## Collection and comparable evidence

The existing single SDK reader and normalizer remain authoritative. The analysis
owner collects only a fixed numeric row schema; it does not read the SDK a second
time. Geometry must be metric track length bound to the same frame's metadata.
Required direct channels include lap/time/tick counters, position/speed, pedals,
steering, pit/surface/incident state, fuel level and fraction, tire compound/set
count, air/track temperature, wind, precipitation, lateral occupancy and usable
opponent arrays. Missing/read-error channels are unavailable, never zero-filled.

Only full, player-in-car, non-stale `SDK_LIVE` context is admitted. Pit/off-track
intervals, incidents, large sampling gaps, refueling, source/counter resets, player or
tire-context changes invalidate the collector epoch. Complete laps require both
start/finish crossings and the existing lap-segmentation quality gates. A 0.3 s
tail admits the existing delayed counter-alignment window; partial capture ends
are not treated as complete laps.

An isolated missing tick no longer clears all accumulated laps. Actual sparse
rows retain their original ticks/times; no replacement frame is fabricated.
The completed-lap model still requires **at least 99.9% tick coverage** and
**at most 0.1 s between samples**, plus all existing cleanliness rules. The live
collector and offline model share the same unchanged thresholds. A larger tick
or simulation-time gap resets the epoch immediately; persistent small gaps
that fail whole-lap coverage yield an explicit rejection/health explanation.
This does not make the older roughly 96% spectator acquisition suitable for
coaching. Current in-car sampling under VR load still needs measurement.

Before coaching, each lap excludes unsuitable flags, stops, observable pit
service, alongside cars, longitudinal opponents within 100 m, changing tire
selection/set count, refueling, precipitation above the declared dry threshold,
and material within-lap temperature/wind changes. Inactive or pit opponent slots
are excluded; no locatable opponents is not a lateral-clearance claim.

The recent cohort retains only laps within six completed-lap increments of the
latest one, with the same observed tire compound/set count. All retained pairs
must have starting fuel fractions within five percentage points, combined air
and track temperature ranges within 2 C, and mean wind vectors within 2 m/s.
These are **observed-condition filters**, not proof of identical tire age, grip,
track wetness or a causal relationship. They do not replace or promote the
stricter labeled/sealed offline condition-cohort acceptance gates. Physical
tire wear, curb use, line geometry and trail-braking prescriptions remain out
of this live projection.

At least three admitted comparable laps are needed to seek a reproducible actual
reference, and at least two supporting laps for a diagnosis. Three laps do not
guarantee a recommendation. The existing fastest-group spread, real reference
selection, disjoint loss accounting and repeated-pattern thresholds are reused.
Each admitted lap is resampled once at a 2 m grid; grid spacing is not a claim
of 2 m physical accuracy. No new third-party source was copied.

## Bounds, isolation and withdrawal

- Fixed 26-column float64 buffers, at most 120,000 rows per pending/current lap;
  reaching the limit discards the partial lap with an explicit reason.
- One independent model worker, at most two queued/active lap jobs and a bounded
  byte budget; overload/stale queued work fails that lane instead of silently
  dropping evidence. It is not an unbounded task/thread-per-lap design.
- At most twelve cached resampled laps and 128 detected corners; one bounded
  point is published. The public mailbox and provider context contain no raw
  rows, opponent identities, trace arrays or private error text.
- The local projection binds the monitor identity, player/session, collector
  epoch/revision and completed-lap counter. Source/condition loss invalidates
  old work; a delayed old model result cannot revive it after reset.
- Routine answers expire after thirty seconds from the **question**, or earlier
  if their source, lap, selected evidence or coaching revision changes. They do
  not restart on unrelated fuel-learning or traffic revisions. Mixed answers
  retain the relevant dependencies; traffic/pit-containing answers still have
  the stricter ten-second limit. Cloud completion never rebases old evidence.
- Playback uses the existing validity checks and urgent Spotter interruption.
  There is no automatic coaching chatter or new simulator-control path.

These are algorithmic buffer/work bounds, not total process-RSS or real-time
latency guarantees. Python workers share the GIL. Shutdown waits for the actual
model owner to exit; it does not pretend a hung native operation has stopped.

## Verification boundary

Invented uniform-cadence SDK frames reach the production normalizer, collector,
asynchronous model, evidence projection and native question service. Separate
fake PTT/output tests exercise the voice route. Tests include all three positive
patterns, one-off rejection, latest-lap improvement, weather/fuel/tire mismatch,
traffic/pit/incident exclusions, malformed/stale projections, bounded history and
rows, blocked model work, queue overload, startup/feed/model faults, delayed fake
cloud replies and native health labels. SDK_LIVE tags in these fixtures are not
authentic SDK evidence or a live acceptance result.
Invented 60 Hz laps with one omitted tick per lap now also reach the unchanged
model when its coverage gate passes; a sustained roughly 96% stream cannot
produce a coaching point. Large time-only and tick-only gaps reset collection.

Memory-only local synthesis produced 14.6–16.6 s clips for the three example
recommendations. No microphone or speaker was opened. Clip duration is not
response latency, user-confirmed hearing or selected-device/VR acceptance.
Private proximity/event/audio replay is now implemented separately; it does not
re-run this coaching model. Packaging, actual complete-lap comparisons and
hardware acceptance remain work under the [active goal](ACTIVE_GOAL.md).
