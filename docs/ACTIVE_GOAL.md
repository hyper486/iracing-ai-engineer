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
UI checks are not a real hearing/VR acceptance pass. A new local trial EXE is
built and self-tested, without replacing existing installations. Stage C's
local current-fuel and physical-traffic question slices are
implemented in source, including player pit permission and flag observations.
Conditional whole-lap fuel-stop comparison is now connected to current fuel and
session-scoped user assumptions. Conditional mapped pit/rejoin projection now
connects hand-entered entry/exit/full-loss assumptions and observed phase tracks;
measured calibration, rule/tire integration and real comparisons remain open. Stage D's
bounded recent-lap model and local coaching questions are now implemented in source;
this is descriptive evidence, not causal coaching or offline gate promotion.
Stage E now has a bounded private proximity/capture/audio journal and silent
native replay in source. It reproduces detector decisions and correlates
recorded software playback; it does not certify hearing or live source origin.
The separate native capture-replay tab now recomputes sealed raw captures using
the real fuel/corner/stint/pit owners, with historical-only bounded cards and no
live-question/voice promotion. It cancels when the simulator connects and never
borrows current strategy assumptions. See [the workflow](NATIVE_CAPTURE_REPLAY.md).
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
- Implemented: the native reader's unused 10 ms polling budget now uses a short
  high-resolution sleep instead of an Event timeout. CPU/timer-only diagnostics
  and synthetic real-reader shutdown/pacing tests cover the change. SDK event
  behavior, decoder, capture format and 99.9% / 0.1 s quality gates are unchanged;
  stop during the pause waits for the requested sleep and scheduling. Actual
  in-car/VR collection rate remains unmeasured. See [the timing boundary](SDK_READER_PACING.md).
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
- Accelerated numerical resource checks and an integrated native trial build
  now cover the supported slices; full-duration hardware limits remain open.
  Full calibrated pit/rejoin integration remains
  open beyond the conditional mapped forecast. Journal replay currently covers
  proximity and software audio, not a simulated hardware/PTT/LLM rerun.
- Implemented: exact direct-fuel amount answers have a separate observation
  revision, so unrelated model-invalid intervals cannot repeatedly cancel them.
  Observation/refuel/source/lap failures remain latched, forecasts retain their
  model dependency and question-time expiry is unchanged. Fake PTT checks cover
  model churn and refueling during synthesis; hardware hearing is still open.
- Implemented: live corner collection no longer clears prior laps for every
  isolated missing tick. It retains sparse rows and uses the unchanged offline
  99.9% whole-lap coverage / 0.1 s gap gates, with explicit quality rejections.
  Synthetic 60 Hz sparse-frame checks are not a real reader-rate improvement.
  Measure current acquisition under in-car/VR load; the older roughly 96%
  spectator result would still fail, and thresholds must not be silently relaxed.
- Implemented: shared whole-lap fuel arithmetic now produces up to two current
  conditional next-stop/stint budgets, with optional transit-plus-pumping loss.
  Native memory-only parameters require fresh owned telemetry and are revoked
  on source/session boundaries. Local questions, exact projection validation,
  short speech and latched plan/config withdrawal are covered with synthetic
  SDK/model/PTT/native-window tests. This is not a pit-entry command, full stop
  service model or optimal rejoin plan. See
  [the fuel-stop contract](LIVE_FUEL_STOP_COMPARISON.md).
- Implemented: packaged numerical integration checks and constant-space native
  answer withdrawal. A 500-lap/60 Hz invented stream processed 892,192 frames,
  with 12 retained corner laps, at most 1,894 observed buffered rows and 3.85 MiB
  measured post-warmup private-commit growth. This is accelerated numerical
  evidence, not four hours of real SDK/audio/recording/VR operation. The rebuilt
  unsigned local EXE passed frozen numerical, Tk and memory-only voice checks;
  installed copies, shortcuts and device settings were not replaced. See
  [the native trial guide](NATIVE_TRIAL_BUILD.md).
- Implemented: constant-space observed stint/tire-counter intervals
  and six-consecutive-clean-lap raw pace comparison, native shortcuts and local
  PTT answers. Fuel-only pit exits do not reset tire observations. Mid-run
  attachment, missing signals, source gaps and unknown physical tire age remain
  explicit. Raw medians disclose starting-fuel change without claiming causal
  degradation or deciding tire replacement. See [the observation contract](LIVE_STINT_PACE.md).
  Physical tire/service calibration and its strategy integration remain open.
- Corrected in current source: the offline tire path no longer uses pit exits
  or zero completed laps as new-tire proof. Versioned reviewed service history
  is bound to the same capture; full-new-set labels establish age, reviewed
  unchanged stops retain it, and missing/partial service or gaps withdraw it.
  V2 calibration pairs require installation-derived ages, and the belief API
  consumes the complete context. M2 and exact bundle replay enforce the same
  origin boundary. Labels are an explicit review trust boundary, not SDK service
  contents or physical wear. See [the contract](TIRE_PERFORMANCE_BELIEF.md).
  This offline-only correction does not admit a tire model to native live
  recommendations; its packaging is tracked in the native trial guide.
- Implemented: conditional native mapped rejoin, exact local questions and short
  PTT answers. Explicit entry/exit/full net-loss assumptions combine with two
  completed per-car phase profiles. Physical neighbor order, sampling bounds,
  reachable fuel windows, short forecast horizons and current-phase checks
  gate every number. Faults and stale/config-changed results withdraw without
  blocking independent proximity. Synthetic source/model/PTT/native checks do
  not establish measured pit calibration or race acceptance. See
  [the mapped-rejoin contract](LIVE_MAPPED_REJOIN.md).
  That mapped-rejoin build passed 16 frozen checks; its 500-lap
  virtual run reached the mapped question path with bounded motion storage.
  This build also contains the offline tire-origin correction, without admitting
  physical tire/service advice to the native live lane. Installed copies and
  shortcuts were not replaced.
- Implemented: the native historical pit-visit lane observes SDK entry/exit
  brackets, elapsed time and a two-profile historical net-loss estimate.
  Local **问耗时** and a stopped-only,
  revision-bound setup draft reduce manual transcription, without promoting
  one visit to a calibrated future cost. See
  [the observation contract](LIVE_PIT_OBSERVATION.md).
  The new unsigned local EXE passed all 17 frozen checks, including the real
  owner/local-question/draft path using invented data. This is not actual pit
  calibration, real-device hearing or VR acceptance.
- Still open: obtain reviewed matched tire/service evidence and connect calibrated
  performance/service costs to endurance strategy. Check selected microphone
  and headphones, wheel/PTT, actual proximity latency and VR performance in a
  user-driven session. A local TTS duration is not output or end-to-end latency;
  a trial build does not close the full tire/rule/action-bound rejoin goal.

For each major advance: update status, run Ruff and all pytest tests, scan public
safety including history, review exact staged files/diff, commit with the configured
noreply identity and push public `origin/main`. Never publish private evidence.
