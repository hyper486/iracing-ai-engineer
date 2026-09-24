# Active goal: usable, advisor-only endurance engineer

Started 2026-09-23. Status: **active, not accepted for racing**.

Build on AEIS, not a running Simulator Controller dependency. Retain the native
Windows app, selectable audio devices/PTT, default read-only pyirsdk transport,
optional standalone Crew Chief-derived reader, replayable telemetry and existing
fuel/strategy/driving models. Reuse third-party source only after file-level
license/provenance review. Do not copy host configuration or private captures.

## First product acceptance slice

1. **Nearby cars are announced promptly.** Local deterministic detection and
   cached short audio; no model/network on the critical path. Urgent events can
   interrupt long answers, expired events never play, and module/data/audio
   failures are visible and reported without repeated distracting chatter.
2. **Fuel questions have current, accurate answers.** Whole-lap learning feeds
   current facts, explicit uncertainty and supported pit/traffic comparisons.
   Missing tire/service/track evidence is not fabricated.
3. **An admitted session yields at least one supported corner practice point.**
   Compare repeated, comparable laps; distinguish observed loss from a causal
   hypothesis. No curb or line prescription without trusted geometry/evidence.

The final goal remains solo endurance support across fuel, tire performance,
stints, multi-stop strategy, nearby traffic and rejoin costs, plus evidence-based
braking/throttle/trail-braking coaching. The first slice does not replace it.

## Execution order and evidence

| Stage | Work | Required check |
|---|---|---|
| A | Independent tick-level proximity events, readiness and bounded audit | Deterministic synthetic transitions, missing/invalid data, stale/replay/spectator, disconnect and session-reset tests; no audio acceptance claim |
| B | Native priority audio, pre-rendered phrases, interruption/expiry, opt-in settings | Fake-output integration plus real selected-device/microphone checks; no LLM blocking |
| C | Current fuel answers and supported strategy integration | Known-outcome replay calculations plus real complete-lap comparison |
| D | Incremental corner comparison and brief coaching | Comparable-lap evidence, contamination exclusions and uncertainty checks |
| E | Record/replay the full event-to-audio chain and conduct one focused practice | Actual in-car SDK source, observed proximity events, fuel truth check, corner evidence, headset audio and VR performance |

Keep raw captures private. Synthetic and replay results remain explicitly labeled;
replaying a live-origin file is not a fresh live acceptance run. Record reasons
for suppressed/expired events and separate detection, playback-start, completion
and user-confirmed hearing. A greeting or running process is not acceptance.

Stage A is implemented as a source-level diagnostic slice; validation and current
limits are tracked in [the public status](PUBLIC_PROJECT_STATUS.md) and
[the detector contract](PROXIMITY_SPOTTER.md). Stage B's native opt-in audio lane,
cache, priority ownership, cancellation and settings migration are now implemented
in source. Synthetic backend, local memory-only synthesis and visible synthetic
UI checks are not a real hearing/VR acceptance pass. No new EXE deployment is
claimed. Stage C's local current-fuel and physical-traffic question slices are
implemented in source, including player pit permission and flag observations.
Action-bound strategy integration and real lap comparisons remain open. Stage D's
bounded recent-lap model and local coaching questions are now implemented in source;
this is descriptive evidence, not causal coaching or offline gate promotion.
Stage E now has a bounded private proximity/capture/audio journal and silent
native replay in source. It reproduces detector decisions and correlates
recorded software playback; it does not certify hearing or live source origin.
Stage B hardware acceptance, D's real-lap acceptance, E's actual in-car trial
and the final endurance goal remain open.
Data collection is user-started; no vehicle, simulator-launch or pit-box commands.
LLM use is opt-in, summary-only, for question interpretation and explanation.

## Delivery progress and next implementation slice

- Implemented: independent proximity worker/guard, fast detector snapshots with
  no EngineerService/fuel dependency, warmed device enumeration and fixed PCM cache.
- Implemented: priority ownership with cancellation acknowledgment, partial-PTT
  discard and deferred cancellation notice, generation/epoch/sequence binding,
  start-by deadlines, mid-play state withdrawal and bounded playback diagnostics.
- Implemented: synthetic scenarios for held PTT, blocked recognition/model/TTS,
  long output, stale data, selected-output loss, reconfiguration and shutdown.
  Device-default refresh behavior remains covered by the audio backend tests.
- Implemented: separate single-owner analysis and private-recording lanes, with
  bounded queues including active work, fixed failure reasons and incomplete
  capture accounting. Slow startup/write/close/status access does not run on the
  SDK owner. Overloaded analysis withdraws fuel without stopping proximity;
  old-generation results cannot republish after reconnection.
- Implemented: streaming event counts/digests with unchanged receipt bytes,
  removing the live app's 50,000-event shutdown and race-long event list. Synthetic
  checks exceeding that old threshold are not a full-duration hardware soak.
- Remaining isolation boundary: SDK access, metadata binding and bounded input
  inspection still run on the reader; Python threads do not isolate the GIL or
  forcibly recover a hung native call. Final shutdown waits for actual release.
- Implemented: exact routine fuel questions use local facts without provider
  waits or budget use, including PTT while an older cloud answer is pending.
  Fresh observations are separate from learned range; finish deficits and
  fuel-only stop bounds retain horizon, reserve and capacity limitations.
  Selected-fact withdrawal prevents a usable observation from preserving a
  now-invalid forecast. See [the question contract](LIVE_FUEL_QUESTIONS.md).
- Implemented: bound circular-track traffic observations and player pit-state
  facts in native display/PTT, independent of fuel learning. Physical distance
  is never relabeled as a time gap, race order or future rejoin position.
  Situation answers use a ten-second question-time limit and separate revision;
  changed neighbours, source loss, permission or flags withdraw old answers.
  Short speech and full explanatory text are separate. See
  [the traffic contract](LIVE_TRAFFIC_QUESTIONS.md).
- Implemented: incremental complete-lap collection and a separate bounded corner
  model, reusing the existing reference/loss/repeated-pattern algorithms. Recent
  observed-condition filters, latest-lap support, fixed health explanations and
  source/epoch-bound local driving answers do not depend on fuel or DeepSeek.
  Synthetic SDK-shaped and fake PTT tests are not real coaching acceptance. See
  [the coaching contract](LIVE_DRIVING_COACHING.md).
- Implemented: private bounded native trial journaling, fixed numeric/enum
  projection, ordered detector/audio correlation, optional capture byte links,
  strict silent replay and visible recording faults. No microphone recording,
  transcript or provider call; no synthetic result becomes hearing acceptance.
  See [the replay contract](PRIVATE_TRIAL_REPLAY.md).
- Verify endurance-duration resource limits and integrate supported live strategy
  before claiming an integrated trial build. Journal replay currently covers
  proximity and software audio, not a simulated hardware/PTT/LLM rerun.
- Before packaging, check observation-only fuel-answer liveness when unrelated
  fuel-model inputs are absent, and corner-collection continuity at the observed
  reader coverage. Treat these as open verification tasks, not proven defects
  or permission to silently relax evidence gates.
- Rebuild/package after those integrations, then check selected microphone and
  headphones, wheel/PTT, actual proximity latency and VR performance in a
  user-driven session. A local TTS duration is not output or end-to-end latency.

For each major advance: update status, run Ruff and all pytest tests, scan public
safety including history, review exact staged files/diff, commit with the configured
noreply identity and push public `origin/main`. Never publish private evidence.
