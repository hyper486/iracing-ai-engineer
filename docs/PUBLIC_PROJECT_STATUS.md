# Public project status

Snapshot: 2026-09-24 EDT

## Product objective

Deliver an advisor-only iRacing AI engineer for solo endurance racing that can
reason about fuel, tires, stints, nearby traffic, pit timing and rejoin cost,
and can produce evidence-backed corner coaching and a post-session report.

The [active execution goal](ACTIVE_GOAL.md) retains this full objective and
prioritizes three usable loops: local proximity calls, current fuel answers and
one supported repeated-corner practice point. Simulator Controller is not a
required running dependency. The final goal remains active and unaccepted.

## Current public milestone

| Boundary | Status | Meaning |
|---|---|---|
| Replayable telemetry foundation | Implemented | Defensive IBT/SDK adapters, normalization and deterministic replay exist. |
| Standalone Crew Chief-derived acquisition | Optional prototype; real spectator comparison | Metadata/encoding optimization preserves bytes; 120 same-tick pairs matched 335 fields. Durable spectator captures reached about 40-41 Hz versus about 57 Hz for pyirsdk, still below quality needs. |
| Fuel, stint and pit reasoning | Implemented with evidence gates | Missing event rules or calibration produce `WAIT`, not invented values. |
| Rejoin/traffic reasoning | Implemented with evidence gates; review corrections applied | Physical circular-track projection binds the selected stop lap; ambiguous future position is WAIT. |
| Tire reasoning | Offline v2 performance belief with reviewed origin | Fuel-only stops do not imply new tires; explicit source-bound service labels and installation-derived calibration ages are required. Not native live tire advice or physical wear. |
| Corner diagnosis | Implemented for repeated comparable evidence | Curb/risk claims remain blocked without trusted labels. |
| Deterministic reports | Implemented | JSON and script-free HTML outputs preserve provenance and limitations. |
| Privacy-safe live state bridge | Implemented; spectator-only field check | Tick-level normalization feeds bounded JSONL snapshots; spectator guard stayed WAIT_CAR. In-car validation remains pending. |
| Local fuel dashboard | Experimental; offline/synthetic checks | Whole-lap fuel learning, freshness/driver guards and optional bounded private recording are implemented. This is not a multi-stop, traffic, tire or driving-guidance release. |
| Local practice speech | Opt-in browser prototype | Only local English voices in confirmed Practice; Race speech is disabled. A hidden tab auto-mutes, so game-background playback is not guaranteed. |
| DeepSeek engineer framework | Implemented; constrained evidence selection | Opt-in asynchronous questions, local grounding/rendering, bounded attempts and safe fallback; validated historical receipt context is separate from live fuel. Real provider/account and in-car acceptance remain unverified. |
| Native Windows EXE | Experimental native Tk/ttk app | Standalone windowed binary, direct Python service calls, masked/optional DPAPI key storage and private recording. No HTML/WebView/server. |
| Native VR voice | Implemented; hardware/race acceptance pending | Opt-in background PTT, input/output selectors or refreshed Windows defaults, local Whisper STT and Windows TTS, interruption and optional guarded low-fuel facts. No continuous listening or raw-audio upload. |
| Tick-level proximity | Detector plus opt-in native audio in the local trial build | Independent fixed-phrase cache, priority cancellation, fast snapshots and bounded playback diagnostics; synthetic/local-synthesis checks only, no hardware or in-car acceptance. |
| Reader / analysis / recording isolation | Bounded worker lanes in source | Recorder and analysis failures no longer synchronously block the SDK reader; explicit incomplete prefixes, generation guards and streaming event digests. Not GIL isolation or a hardware latency guarantee. |
| Routine current-fuel questions | Packaged local trial; synthetic checks | Current observations, learned range and conditional race fuel budgets bypass cloud waits; not live pit tactics or in-car acceptance. |
| Current physical traffic / pit-state questions | Packaged local trial; synthetic checks | Bound ahead/behind distance, player permission and flag facts in native display/PTT; independent of fuel readiness, not time gaps or optimal pit/rejoin advice. |
| Incremental recent-lap coaching | Packaged local trial; synthetic checks | Complete laps feed the existing repeated-pattern model via a bounded worker; local PTT gives an observed loss and practice hypothesis, not a causal gain or live acceptance. |
| Private proximity / audio replay | Packaged local trial; synthetic checks | Bounded local journal, exact detector recomputation, software-playback correlation and optional capture byte links; not audio reproduction, source authentication or human hearing. |
| Conditional live fuel-stop comparison | Packaged local trial; synthetic checks | Session-scoped hand-entered assumptions, shared complete-lap stop arithmetic, next-fill/stint budgets and local PTT; not mapped pit-entry timing, optimal strategy or future rejoin. |
| Conditional mapped rejoin | Packaged local trial; synthetic checks | User-confirmed entry/exit/full net-loss assumptions, two completed per-car phase profiles, reachable fuel-window endpoints, physical neighbor ranges and local PTT; not measured calibration, optimal timing or live acceptance. |
| Observed stint / tire-counter interval / raw pace | Packaged local trial; synthetic checks | Separate stint and tire observations, six consecutive clean-lap median comparison and local questions; not physical tire age, fuel-corrected degradation or a tire-change decision. |
| Advisor-only safety | Required and implemented | No vehicle, simulator-launch or pit-box control path is accepted. |
| Authentic local `SDK_LIVE` acquisition | Proven before acceptance | The running simulator's real shared-memory transport has produced a complete, sealed canary capture. |
| Authentic local `SDK_LIVE` acceptance | Pending on-track evidence | Out-of-car, stationary pit-stall and spectator captures do not support strategy or driving acceptance. |
| Final strategy plus driving report | Pending live evidence | Both advice gates must pass on an admitted real capture. |

## Conditional native mapped rejoin

The native window now has optional session-scoped pit-entry/exit and complete
net-loss inputs plus **问出站 / 出站预测**. Current fuel-window endpoints map to
reachable entrances, with projection to the corresponding exit. The tracker
retains each on-track actor's two completed 64-bin lap profiles, not a uniform
average-speed or instantaneous-speed extrapolation. Physical distance selects
neighbors in multiclass/lapped traffic; uncertainty in overlap or order gives
`WAIT`. Missing geometry, full loss, permission or continuous motion stays
explicit. Excluded pit/off-track slots do not become a "clear track" claim.

Projection runs outside the AppState lock. Delayed old-config work is discarded;
continuity/neighbor/material-gap changes and the ten-second answer TTL withdraw
old speech. Ordinary opponent profile refreshes do not continually cancel it.
Exact questions bypass DeepSeek; optional free-form selection only sees fixed
summary facts. Independent Spotter/fuel/coaching remain available when this
module faults. See [the contract](LIVE_MAPPED_REJOIN.md).

The 56 new synthetic cases cover numerical, source, fault, configuration-race,
fake-PTT and presentation boundaries. A separate 207-case malformed-projection
check admitted no unsafe facts and raised no uncaught errors. Native settings/
shortcut and streaming numerical tests also exercise the new lane. The rebuilt
unsigned local EXE passed all 16 frozen numerical/Tk/memory-only voice checks
with a system-only child PATH and no saved credentials. Visible synthetic QA
confirmed the four setup fields, question shortcut, unavailable response and
expired-answer withdrawal. It was then closed; existing installations, shortcuts,
credentials and device preferences were not replaced. Full regression passed
**2,950 tests, with 44 skips**, in **556.62 seconds**. Skips retain their existing
data/platform/private-deployment and explicit opt-in boundaries. No real race,
microphone, headphone, calibrated service or VR acceptance is implied.

The current **500-lap / 892,192-frame** virtual 60 Hz run passed all eight
numerical checks in **208.094 wall seconds** for **14,869.85 virtual seconds**.
It reached 8,400 ready mapped-rejoin publications and the local question path.
Peak motion storage for the two active synthetic cars was 363 profile points;
the other bounded peaks remained 12 corner laps / 346,176 trace bytes, 1,894
rows, one job / 387,904 bytes, 50 fuel samples and 42 proximity audit rows.
Measured peak private-commit growth after 24 warmup laps was **4.64 MiB**. This
is a small invented fleet with accelerated time and worker barriers, not a
full-grid SDK/recording/audio/VR endurance soak or a hardware latency guarantee.

## Previous milestone: reviewed tire-origin correction

Current source fixes the old offline assumption that every pit exit or lap-zero
attachment proves a fresh tire set. V2 tire contexts bind independently pinned
reviewed service labels to captured exit ticks and SDK observations. Full-new-set
labels establish an origin, reviewed unchanged stops preserve age, and unknown
or partial service withdraws age even when the set counter remains unchanged.
Missing channels, continuity loss and contradictory evidence stay unavailable.

Calibration pairs now require reviewed new-set origins and origin-derived lap
ages. M2 and the public belief API require the complete current v2 context; a
bare age plus a digest cannot bypass its provenance checks. Finalization and
object-exact verification both accept the same pinned private service history.
Old tire subcontracts must be regenerated from evidence, not blindly rehashed.
The unchanged native live tracker still reports observations only.

Targeted checks include a positive synthetic capture-to-model-selected-change
and exact bundle replay, plus unknown/partial/fuel-only service and tampering
regressions. These labels/captures are invented tests, not real tire calibration.
At that milestone, the local trial EXE and installed copies had not been rebuilt
for the offline source correction. Real matched tire/service data, native
strategy integration and hardware/VR acceptance remain open. See
[the tire-origin contract](TIRE_PERFORMANCE_BELIEF.md).

Final regression: **2,894 passed, 44 skipped** in **535.73 seconds**, including
50 added cases. The retained skips are data/platform/private-deployment or
explicit opt-in boundaries, not newly accepted evidence. A separate 54-variant
malformed-label check rejected every rehashed invalid input. Ruff, public safety
including history, exact staged-file review and diff checks passed. No simulator,
microphone, speaker or provider session was opened for this milestone.

## Previous milestone: observed stint and raw pace

Current-source questions now connect a constant-space stint/tire-counter tracker
and the recent-lap worker's raw pace summary to the native UI and local PTT.
Attachment during a run is partial. Fuel-only pit exits reset the observed stint,
not the tire interval. Counter/read/source discontinuities retract old facts,
including brief read loss between display publications. Tracker faults remain
visible without stopping independent fuel, traffic, coaching or proximity.

Pace compares two non-overlapping groups of three consecutive clean comparable
laps. Rejected intervening laps cannot be skipped. Starting-fuel change is
disclosed; there is no fuel-weight correction, causal wear claim, tire-life
forecast or tire-change recommendation. Exact questions bypass the provider;
free-form evidence selection remains opt-in and summary-only. See
[the observed-stint/pace contract](LIVE_STINT_PACE.md).

New regression cases cover source and counter transitions, malformed projections,
fault isolation, raw medians, local answer binding/expiry, fake PTT and native
shortcut activation. Every required stint-contract field is checked before fact
rendering; missing fields withdraw the observation instead of raising an error.
Short voice examples measured **6.427 / 7.197 / 8.166
seconds** using memory-only local synthesis for stint / tire / pace. These are
example waveform lengths, not measured response latency, selected-output hearing
or guarantees for every number, installed voice or rate. Full explanations stay
visible; unknown-evidence speech is also kept short.

The repeated **500-lap / 892,192-frame** accelerated numerical run passed all
seven numerical checks, including the new local observations. Peak retained
state remained 12 corner laps / 346,176 trace bytes, 1,894 rows, one worker job /
387,904 bytes, 50 fuel samples and 42 proximity audit rows. Measured private
commit growth after 24 warmup laps was **4.33 MiB**. This run took **185.125 wall
seconds** for **14,869.85 virtual seconds**, with the existing worker barriers;
it is not an SDK/audio/recording/VR endurance soak. It preceded the final speech
and malformed-projection refinements; the numerical producer and observation
shapes were unchanged.

Final complete regression: **2,844 passed, 44 skipped**, including 84 new cases.
The three new fake-PTT questions also passed five consecutive runs. Existing
skips retain data/platform/private-deployment or explicit opt-in boundaries.
Ruff, public safety including history and exact staged-file/diff review passed.
The rebuilt unsigned local EXE passed all **15 frozen self-test checks**, with
seven numerical checks plus native lifecycle and memory-only voice checks under
system-only PATH. The native observation row, shortcuts and no-evidence response
were also checked in a visible synthetic window; no hardware acceptance is claimed.
Existing installations, shortcuts, credentials and device preferences were not
replaced. Hardware hearing, genuine in-car laps, physical tire/service calibration
and strategy/rejoin integration remain open. The final endurance goal is active.

## Earlier integrated native trial and numerical resource milestone

A new unsigned local EXE packages the supported source slices together. Default
build verification now requires five numerical checks in addition to the native
window/lifecycle and in-memory voice checks. Invented frames traverse the real
normalizer, proximity, fuel and corner owners, including local fuel, coaching
and conditional stop questions. No SDK transport, provider account, microphone
or speaker is opened. All 13 frozen self-test checks passed with system-only
PATH and no inherited Python/Tcl/provider environment. Existing installations,
shortcuts, keys and device settings were not replaced.

The accelerated resource run processed **500 synthetic laps / 892,192 frames**
at a virtual 60 Hz: **14,869.85 simulated seconds in 176.625 wall seconds**.
Corner-worker barriers deliberately pace virtual time; this is not an SDK
latency, overload or four-hour hardware soak. It produced 55 repeated-corner
local answers and exercised refuel epochs. Peak observed retained state stayed
at 12 corner laps / 346,176 trace bytes, 1,894 buffered rows, one worker job /
387,904 bytes, 50 fuel samples and 42 proximity audit rows. After 24 warmup laps,
Windows private committed memory grew at most **3.85 MiB** above its baseline.
This memory measurement excludes real voice/SDK/recording/VR work and is not a
general memory-budget acceptance gate.

The native presenter also no longer accumulates an unbounded set of expired
answer IDs. A constant-space epoch/serial high-water mark rejects older answers
without allowing revival after eviction, including reconfiguration and changed
scope/origin. A 20,000-withdrawal regression and malformed-ID cases cover this.
Focused desktop/diagnostic checks: **113 passed**. Complete regression:
**2,760 passed, 44 skipped**, including 31 new checks. Ruff, public safety
including history and exact staged-file/diff review passed. Existing skips
retain data/platform/private-deployment or explicit opt-in boundaries; they do
not imply accepted live evidence. The visible synthetic EXE exposed the new
status/settings/voice/question controls and exited on its diagnostic timer;
this is not a microphone, playback or real-source check.

See [the integrated native trial guide](NATIVE_TRIAL_BUILD.md) for reproduction
and the focused user-driven practice checklist. Local receipts and binaries
remain ignored/private; GitHub receives reusable source and documentation, not
raw captures or host-specific files. Real selected-device hearing, recognition
and proximity latency, in-car frame quality and VR performance remain open.
The complete tire/service/rule/action-bound rejoin goal remains active.

## Earlier conditional live fuel-stop comparison milestone

The native source now connects the learned current-fuel model to shared
whole-lap stop arithmetic, without making synthetic lap samples or offline
receipts. Current-connection user inputs supply effective capacity and optional
refueling rate/pit-transit-loss bounds. Local questions and the new comparison
shortcut expose feasible earlier/later fuel scenarios, next dose, next stint
and further stops. Cumulative finish deficit remains a separate question.

Configuration is memory-only and source/session/player bound; applying does not
restart SDK/model/provider workers or modify the simulator. Missing/inconsistent
inputs explain why a comparison is unavailable. Numeric projections are
independently recomputed with typed-structure checks, fixed provenance and no
free-text forwarding. A projection fault is visible and contained. Ten-second
answers have latched config/plan/source withdrawal, including changes between
consumer polls; normal small consumption alone does not cancel every utterance.

Validation covers synthetic SDK-to-model publication, arithmetic boundaries,
multi-stop next-dose versus total deficit, parameters/reconnection, malformed
projections, fault containment, fake PTT and hidden native settings controls.
Complete regression: **2,729 passed, 44 skipped**, including 73 new checks.
The two asynchronous PTT cases also passed five consecutive runs. Ruff, public
safety including history and exact staged-file/diff review passed. Existing
skips retain data/platform/private-deployment or explicit opt-in boundaries;
they are not accepted live evidence.
No real simulator, microphone, selected output or provider account is used for
acceptance. Existing EXEs are not updated by this source commit. The windows
start at the question position, not a mapped pit entrance; tire/service/rule
integration, action-bound rejoin, resource soak, packaging and real in-car/VR
validation remain open. See [the comparison contract](LIVE_FUEL_STOP_COMPARISON.md).

## Earlier fuel-answer and corner-collection continuity milestone

Two pre-packaging defects were reproduced using invented frames and the actual
state/model owners. Exact direct-fuel amount answers were withdrawn on every
unrelated model-invalid interval. Corner collection reset its entire epoch on
any missing tick, even when the unchanged whole-lap quality gate admitted the
sparse lap. These are now corrected in source:

- Amount-only replies use an observation revision that survives model churn,
  while latching real read loss, refueling, source/lap changes and analysis
  faults. Fuel-specific loss/refuel signals are retained even between the
  half-second display publications. Forecast/model-fallback guards and expiry
  remain unchanged. Fake PTT verifies playback after model churn and suppression
  after refueling during TTS.
- Live coaching retains actual sparse rows and delegates completed-lap coverage
  to the existing 99.9% / 0.1 s gates. It resets on large tick/time gaps and gives
  explicit low-coverage rejection text. One omitted tick per invented 60 Hz lap
  can pass; sustained roughly 96% coverage still cannot produce advice.

Complete regression: **2,656 passed, 44 skipped**, including 42 new checks.
The two asynchronous amount/PTT cases also passed five consecutive runs. Ruff,
public safety including history and exact staged-file/diff review passed.
Existing skips remain data/platform/private-deployment or explicit opt-in
boundaries, not live acceptance.
No simulator, microphone, selected output, provider account or installed EXE
was used for this validation. This is not a measured SDK acquisition-rate
improvement. In particular, the older spectator coverage is still inadequate;
current acquisition under in-car/VR load remains to be measured. Live strategy,
endurance-duration resources, packaging and real acceptance remain open.

## Earlier private proximity / audio replay milestone

The native recording switch now owns an independent private diagnostic journal
as well as the existing raw collector. The journal links fixed proximity inputs,
time-driven decisions, native audio outcomes and raw capture IDs/hashes. A native
background replay button and source CLI recompute decisions and distinguish no
recorded attempt, pre-start drops, cancellation, playback failures and software
completion. They do not play audio, call a model or authenticate live source.

The 1 GiB per-run journal budget and bounded queue are separate from raw capture.
Strict projection prevents raw dictionaries, transcripts, device/voice names,
credentials and arbitrary errors entering the journal. Overflow or I/O failure
is visible and does not stop proximity; lifecycle operations await actual file
owner exit. Missing seals remain incomplete prefixes, and malformed/mutating
files are rejected. Raw byte-link matches do not promote SDK_LIVE gates.

Complete regression: **2,614 passed, 44 skipped**, including 68 new checks.
Eight asynchronous lifecycle/correlation cases also passed five consecutive
runs. Ruff, public safety including history and exact staged-file/diff review
passed. Existing skips remain data/platform/private-deployment or explicit
opt-in boundaries, not live acceptance. Hidden native-window checks exercise
the replay action and fixed report text with synthetic state only.
No selected device, microphone, live SDK session, provider or installed EXE was
used for acceptance. See [the replay contract](PRIVATE_TRIAL_REPLAY.md).
Full live pit/rejoin integration, packaging and real selected-device/in-car/VR
checks remain open under the unchanged active goal.

## Earlier incremental recent-lap coaching milestone

The native **问驾驶** button and exact PTT questions now use the same completed-lap
reference and repeated-pattern algorithms as offline analysis. The source path
collects fixed numeric rows from the existing normalized stream, rejects unclean
or mismatched evidence, and models laps on a separate bounded worker. It reports
one recent supported point for long coasting, later-braking/slower-exit or a
second throttle lift; it does not invent curb, line, tire-wear or causal gains.

Results require repeated support in the latest completed lap, not just an old
mistake. A bounded recent cohort filters observed fuel, temperature, wind and
tire selection; it does not prove identical tire age/grip or promote stricter
offline labeled-condition gates. Questions use local short speech, with the
actual reference, evidence count and observed median loss in the window.
Model/startup faults, missing data and resource limits are explicit; fuel,
traffic and urgent proximity remain independent. Old-generation model results
and delayed cloud explanations cannot revive withdrawn coaching.

Complete regression: **2,546 passed, 44 skipped**; focused regression:
**513 passed**. The 95 new coaching tests include positive and exclusion paths;
ten asynchronous/fault cases also passed five consecutive runs. Ruff, public
safety including history, and exact staged diff review passed. Existing skips
remain data/platform/private-deployment or explicit opt-in boundaries, not live
acceptance. Synthetic frames and fake PTT/output are not authentic
driving or hearing acceptance. Memory-only local TTS clips were 14.6–16.6 seconds;
no input/output device was opened. No EXE, game or Simulator Controller
configuration was changed. See [the coaching contract](LIVE_DRIVING_COACHING.md).

At that milestone, full live pit/rejoin integration, private event/audio replay,
packaging and real selected-device/in-car/VR checks remained open.

## Earlier local traffic and pit-state question milestone

The native display and exact PTT questions now expose ahead/behind longitudinal
distance, current-player pit permission and available flags. These local answers
do not wait for fuel learning or DeepSeek. `该进站了吗` combines available facts
but still withholds an optimal stop lap or future rejoin claim.

Bound metric track geometry and normalized direct opponent arrays feed a bounded
observation-only projection. Cross-line wrap, pit/inactive exclusions and
five-metre ambiguity are explicit. Missing data never becomes a clear-track
claim. A traffic-only analytical fault is isolated and visibly reported without
disabling fuel or proximity; no raw metadata or car identities reach the LLM.

Traffic/pit answers bind question-time situation state and expire after ten
seconds or an earlier relevant change. Published loss/recovery cannot revive
an old answer between polls. Fuel-learning revisions alone do not invalidate
standalone traffic. Brief speech uses approximate kilometres for long distances;
the window retains the fuller evidence and limitations.
The same isolation applies to unavailable-data notices with no selected facts:
unrelated fuel failures cannot repeatedly cancel their spoken explanation.

Complete regression: **2,451 passed, 44 skipped**; final focused regression:
**218 passed**. The skips remain missing-data, platform, private-deployment or
explicit opt-in boundaries, not live acceptance. Ten asynchronous/fault-recovery
cases also passed five consecutive runs, and 180 synthetic rendering combinations
stayed within the answer limits. Ruff, public-safety scanning including history
and exact staged diff checks passed.

The tests use invented SDK frames, fake providers and fake PTT/output. Separate
memory-only synthesis checked clip lengths without opening the microphone or
speakers; it did not measure response latency or hearing.

No game, provider account, installed EXE or Simulator Controller configuration
was changed. Stage C's action-bound pit/rejoin integration and real fuel checks,
incremental corner coaching, capture/audio replay, packaging and hardware/VR
acceptance remain open. See [the traffic contract](LIVE_TRAFFIC_QUESTIONS.md).

## Earlier local current-fuel question milestone

Explicit routine current-fuel questions now use locally rendered evidence in
the native buttons and PTT path. Current amount, learned whole-lap range, burn,
race finish balance and conditional fuel-stop bounds need no provider call.
They remain usable while one older cloud request unwinds, consume no cloud
budget and use a separate one-second guard. An older provider result cannot
overwrite the newer local answer. Mixed or explanatory questions still use
the existing grounded planner; this is not an unrestricted chatbot.

A fresh, owned fuel observation can be answered before burn learning completes
or while a pit/refuel interval blocks forecasting. Read errors, stale sources,
replay and spectator context remain excluded. Reserve, observed burn range and
finish-horizon basis are explicit. Cumulative deficit is not a next-stop fill
setting; a fuel-only minimum stop count is not a tactical or mandatory-stop
decision. Native default tank capacity remains unknown, so positive stop bounds
are withheld rather than guessed. Loss of a selected fact withdraws the answer
even when some other observation remains usable.

Synthetic checks connect invented SDK frames through the real monitor, fuel
estimator and question service. A separate fake microphone/STT/TTS/output test
asks a cloud question, interrupts its pending voice wait, and successfully
speaks a local range answer before the fake provider is released. These tests
do not measure real recognition latency, output-device hearing or VR impact.

Complete regression: **2,395 passed, 44 skipped**; focused regression: **427 passed**.
The skips remain missing-data, platform, private-deployment or explicit opt-in
boundaries, not live acceptance. The two cloud/local concurrency cases also
passed five repeated runs. The existing five-check offline HTTP/planner
rehearsal passed with invented data and a fake provider. Ruff, public-safety
scanning including history and exact staged diff checks passed.

No game, microphone, speaker, provider account, installed EXE or Simulator Controller
configuration was changed. Stage C's supported traffic/pit integration, live
fuel accuracy comparison, incremental corner coaching and capture/audio audit
remain open. See [the question contract](LIVE_FUEL_QUESTIONS.md).

## Earlier bounded live-work isolation milestone

The source reader now sends slow analysis and optional private recording to
separate single-owner lanes, each bounded to 128 observations / 16 MiB of retained
payload accounting including active work. Sink construction, writes, finalization,
status access and close all remain on their owner. Overflow stops that lane with
an explicit incomplete-capture reason; it cannot silently drop old frames and
then mark the session complete. Original observation timestamps and connection
generations prevent stale or late analysis from becoming fresh fuel facts.

Synthetic fault injection keeps SDK reads and left/clear detection progressing
through blocked recording initialization, ingestion and close, or blocked analysis.
It also checks shutdown release acknowledgment, old-worker reconnect/recovery,
failure callbacks, private-safe errors and unavailable current-recording labels.
Only final application shutdown waits for slow lane owners after SDK closure;
reconnection cannot accumulate replacement threads while old ones are alive.
If a recorder is still alive when a new connection starts, recording is disabled
until a deliberate restart and the UI says this connection is not being captured.

Live event history is now streamed into the same canonical SHA-256 receipt and
aggregate counts; offline event receipts retain byte compatibility. A synthetic
50,100-observation rejection stress check exceeds the removed 50,000-event stop
without race-long event retention. This is not a real-time endurance soak or an
RSS measurement. Per-lane byte metrics are conservative queue estimates; recorded
bytes are the last owner-observed committed count, not partial failed writes.

Complete regression: **2,335 passed, 44 skipped**; focused regression: **154 passed**.
The skips remain explicit missing-data, platform, private-deployment or opt-in
boundaries. Seven blocking/reconnect cases also passed five repeated runs. A
separate 60 Hz-paced synthetic check processed all 360 observations into 12
snapshots with no rejected/discarded work and left/clear proximity candidates;
its peak analysis queue was one observation. This used the unmodified production
worker with an invented SDK, not a real SDK or speaker.

Ruff, public-safety scanning including history and exact staged diff checks passed.
No game, microphone, speaker, provider, installed EXE or Simulator Controller
configuration was changed. SDK calls, metadata binding, input inspection and
Python GIL contention remain outside this thread-level isolation.
Hardware audio/VR acceptance, current strategy/corner integration and durable
event-to-audio replay remain open. See [the contract](PROXIMITY_SPOTTER.md).

## Earlier native proximity delivery milestone

Stage B adds an independent, opt-in proximity audio worker and guard, fixed Chinese
PCM cache, device warm-up, urgent output ownership, late-start rejection and
mid-play cancellation. It does not wait for fuel learning, the slow display,
local recognition, synthesis of a long answer or DeepSeek. Held PTT interrupted
by a proximity event is discarded rather than transcribed/submitted as a prefix;
an informational cancellation notice is deferred until clear conditions.

Native preferences migrate from v1 to v2 with the new proximity switch off.
The source UI reports detection separately from preparation/output health, zero
volume, suspension and errors. A visible synthetic preview found and fixed
wheel scrolling over child controls so the apply/test row remains reachable.
Local memory-only synthesis verified all eleven cached phrases; proximity clips
were about 0.80-1.16 seconds after rate/silence adjustments. This is clip length,
not SDK-to-ear latency. No microphone or speaker was opened for that check.
An accelerated detector-only check also processed 1,296,000 invented frames
(six simulated hours) with the audit capped at 128 rows; it is not a hardware
soak or full-pipeline endurance acceptance pass. Regression coverage includes a
newer stop winning over an older in-flight settings save and a cancelled output
still reporting device-close failure.

Final complete regression: **2,288 passed, 44 skipped**. The skips remain explicit
missing-data, platform, private-deployment or opt-in boundaries. Ruff, public-safety
scanning including history and staged diff checks passed. Six concurrent
question/stop scenarios also passed five repeated runs using fake devices.
All new audio scenarios remain synthetic, not hearing or in-car evidence.

No deployed EXE, Simulator Controller configuration or user credentials were
changed. Actual selected-device hearing, real microphone/VR load and durable
end-to-end replay remain open. The later isolation milestone above separates
slow recording/analysis work but does not establish hard real-time guarantees.
See [the detector/audio contract](PROXIMITY_SPOTTER.md) and [the active plan](ACTIVE_GOAL.md).

### Earlier Stage A diagnostic milestone

The September 23 source change adds an independent, deterministic `CarLeftRight`
state machine without requiring fuel learning or opponent lap-time arithmetic.
It rejects missing/invalid/stale data and replay/out-of-car context, confirms
occupancy and clear transitions, withdraws obsolete candidates and records a
bounded nonsecret audit. Detector faults remain local to that detector.

The native source window distinguishes waiting, ready, stale and failed detection
while explicitly showing that proximity audio is not connected. No installed
EXE was rebuilt or replaced, and no Simulator Controller configuration was changed.
The no-game rehearsal validates left/both/right/clear transitions on 300 invented
frames. Integration tests capture a short pass wholly between two slow display
updates and keep fuel analysis running through a detector fault.

Full regression completed with **2,215 passed, 44 skipped**. The final lock-clock
follow-up was separately verified with **122 focused tests passed**. The skips
remain explicit missing-data, platform, private-deployment or opt-in boundaries.
Ruff, public-safety scanning including history and staged diff checks passed.
All new proximity scenarios are synthetic, not authentic in-car evidence.

At that earlier diagnostic milestone, priority audio, full reader/writer/analysis
fault isolation, end-to-end capture replay and hardware acceptance were open.
The current Stage B source delivery above adds audio ownership and playback
diagnostics; it does not promote hearing or live acceptance. See
[the current contract](PROXIMITY_SPOTTER.md) for remaining boundaries.

## Native Windows desktop milestone

The September 22 desktop milestone replaces the browser as the primary local
interface with an independently runnable `AEIS-Engineer.exe`. Native controls
show live fuel/quality, ask constrained engineering questions, import validated
historical session reports and configure DeepSeek locally. The legacy web UI
remains an optional separate entry, not the implementation of the new window.
Build and Chinese operating instructions are in [the desktop guide](WINDOWS_DESKTOP.md).

The unsigned one-file x64 build includes Python, Tcl/Tk and the read-only SDK
adapter. Its frozen self-test runs with a system-only PATH and without Python,
Tcl, Conda or provider-key overrides. It checks GUI creation, local fallback and
the actual close protocol without starting the SDK or calling a provider.
Earlier visible native-window checks covered three tabs, a local fuel question and
window closure. These are packaging/UI checks, not authentic driving evidence.

Cloud use remains off by default. Current-user Windows DPAPI persistence is
optional, keys are never prefilled or logged, and provider attempt counts survive
model/history reconfiguration. A failed key decryption does not reset recording
preferences. Unsafe capture paths disable recording without disabling read-only
monitoring. Shutdown waits for both reader and configuration work to really end;
it does not declare successful closure just because a timeout elapsed.

The fourth tab adds opt-in VR voice. Input and output can be chosen independently;
unset selections resolve the current Windows default before each operation.
Explicit unavailable/ambiguous devices never silently fall back. PTT uses F9
(F8-F12 selectable) or a bound joystick button; it neither grabs focus nor injects
game input. Recording is capped at 12 seconds and kept in memory. Local-only,
commit-pinned Whisper medium CPU int8 recognition runs in a bounded, killable child;
Windows renders Chinese speech in memory, then PortAudio routes it to the chosen
output. Audio never goes to DeepSeek. Only the accepted question text follows
the existing opt-in, bounded and grounded cloud path.

Windows dictation and smaller Whisper models were rejected as the default after
poor synthetic Chinese round-trip results. The selected medium model preserved
intent and key words in eight predefined TTS-generated questions (punctuation
may differ); this tiny, synthetic sample is not an accuracy claim for human speech.
A successful API invocation alone was not treated as speech acceptance.
Automated tests cover device/default changes, background input edge
handling, disconnect/cancellation, pending-disable and close races, stale-answer
withdrawal and memory-only diagnostics. Real microphone, headset output, wheel
button, recognition in racing noise and VR frame-time impact still require a
human-driven hardware check. Synthetic tests do not promote those gates.

Optional low-fuel speech is off by default, uses only guarded fresh in-car facts,
and does not call a model or issue a pit instruction. Real DeepSeek account/network
behavior, in-car fuel validation, multi-stop/traffic/tire tactics, repeated-corner
coaching and reliable race audio remain unaccepted. No game/control command is issued.

Native/VR regression: the full suite completed with **2,148 passed, 46 skipped**.
Two wheel-build checks skipped because `uv` was not on PATH were rerun with the
tool explicitly available: **2 passed**. The other 44 skips remain documented
data, platform, private-deployment or opt-in boundaries. Ruff, public-safety
scanning including history and staged diff checks passed. The frozen one-file
EXE passed all **8** synthetic self-tests, including exact model hashes and real
in-memory Chinese TTS-to-STT, with a system-only PATH. No microphone, speaker,
simulator or provider was accessed by those frozen checks.

## DeepSeek framework integration

The September 22 integration connects typed questions and quick-topic buttons
to a separately queued DeepSeek answer planner. Only allowlisted engineering
summaries plus the user's question leave the host when explicitly enabled and
configured. The model selects existing fact IDs; local code renders all claims,
numbers and mandatory limitations. No raw telemetry, driver identity, paths,
source hashes, keys or whole receipts are sent in the generated context.

The default is cloud-off. Missing keys, provider failure, invalid plans and
exhausted attempt budgets preserve local answers and do not block SDK reading.
Live answers are snapshot-bound and withdrawn on expiry or safety/session
changes. Validated historical engineer-session receipts can provide descriptive
strategy/driving/tire context, always marked historical/shadow-only and not
authenticated original telemetry. This does not close the live multi-stop,
traffic, tire-service, corner-coaching or background race-audio gaps.

See [the DeepSeek guide](DEEPSEEK_ENGINEER.md) for local-only key entry, limits,
privacy, historical input and the synthetic loopback rehearsal. No real provider
call has been validated by the synthetic framework checks; account configuration
is private and is not part of public acceptance evidence.
Synthetic/mock checks are not `SDK_LIVE` acceptance.

The end-to-end historical receipt check also found and fixed a producer-side
rounding inconsistency: a millisecond-rounded descriptive gain upper bound
could exceed its unrounded observed loss and fail the existing receipt
validator. The producer now clamps its own bounds; validator tolerance and
driving-promotion rules are unchanged. A complete synthetic multi-lap receipt is
built and independently revalidated in the regression test.

Earlier DeepSeek-only regression: **1,726 passed, 43 skipped**. Ruff, public-safety scanning
including history, and staged diff checks passed. The skips remain explicit
missing-data, private-deployment or platform-only cases, not live passes.
The standalone loopback rehearsal passed all five scenario groups without SDK
access, credentials or provider calls. Browser smoke checks exercised the
missing-key state, quick questions, local fallback and old-answer withdrawal.
The local worker was refreshed while waiting for the simulator with zero
recorded bytes; no authentic in-car, real-provider or audio acceptance is claimed.

## Experimental fuel dashboard

The September 22 local-app work adds a loopback browser UI and a separate,
experimental fuel-only estimator. The hidden PowerShell launcher defaults to
six hours at `http://127.0.0.1:8765/`; it does not launch the simulator or send
vehicle/pit-black-box commands. Setup, state meanings, stopping and limitations
are in [the local-app guide](LIVE_APP.md).

Learning needs the first observed crossing plus at least five valid complete
laps by default. Pit/out laps, refueling, unsuitable flags, incidents and invalid
intervals are excluded; old estimates are withdrawn when evidence becomes
unusable. Sparse tick loss is tolerated only for this low-rate experimental fuel
path, not by weakening high-rate driving-quality or M2/M3 admission gates.
Finish demand requires a confirmed current Race session and a usable horizon;
refill liters additionally require configured tank capacity and remaining demand
that fits into one tank. No complete multi-stop/traffic/tire strategy or driving
guidance is released by this UI.

The launcher defaults to private raw capture under
`%LOCALAPPDATA%\iRacingAIEngineer\captures`, with a 4 GiB per-run budget;
`-NoRecording` disables it for a newly started worker. Recorder failure does not
by itself kill the UI. These captures remain private and are not served by HTTP.
Practice speech is manually opt-in, uses only browser-reported local English
voices, and auto-mutes in a hidden tab. Official races and all other Race sessions
remain silent; reliable game-background audio is not claimed.

Full regression: **1,426 passed, 43 skipped**. Ruff, public-safety scanning
including history, and staged diff checks passed. The skips remain explicit
missing-data, private-deployment or platform-only cases, not live passes.
Windows hidden startup, correctly quoted private capture arguments, loopback
waiting state, browser rendering with explicitly synthetic learned-fuel values,
and the mute control were checked locally. Browser checks reported no console
errors; no audible playback or authentic in-car UI/audio acceptance is claimed.
This establishes no new `SDK_LIVE` acceptance. The prior regression totals and
spectator transport results below describe earlier milestones. The
[Chinese trial checklist](LIVE_TRIAL_ZH.md) explains the next human-driven test.

## Review corrections

The September 22 clock/retry follow-up unifies capture, freshness observations
and deadlines on a high-resolution monotonic clock. The pyirsdk reader retries
normal increasing SessionInfo update races with at most three whole-frame
attempts and a 100 ms retry budget; failed attempts are discarded and released.
Persistent churn, counter regression, schema changes and unstable buffers still
fail closed. A new complete spectator capture had no equal/decreasing capture
timestamps and no capture-clock regression rejections. Its one quality rejection
was a measured 751 ms gap, which remains rejected correctly. The default backend,
advisor-only restrictions and live product acceptance are unchanged.
Full regression: **1,196 passed, 43 skipped**. The skips remain explicit
missing-data, private-deployment or platform-only cases; they are not live passes.

The September 22 follow-up reduces repeated schema validation, unchanged
SessionInfo parsing and collector JSON encoding, while preserving per-frame
checks, privacy filtering and per-record durable writes. Offline tests check
cache invalidation and byte-exact receipts; synthetic timings do not establish
live throughput. Full regression: 1,154 passed, 43 skipped. A later real
spectator comparison confirmed sampled field parity but insufficient sustained
coverage, SessionInfo-race interruptions and a spectator player-class identity
rejection. Fifteen normalized false-stale rejections were also traced to the
Windows runtime's coarse monotonic clock; the later clock/retry follow-up above
addresses that cause. Neither the default backend nor deployment is changed.

The September 21 acquisition prototype reuses Crew Chief's low-level SDK source
without its UI, speech, MQTT or strategy. It adds an opt-in `collect-live`
backend, a pinned upstream MIT notice, bounded pipe validation and separate
synthetic native tests. It adds no simulator or pit controls and does not
change live acceptance. See [the reader contract](CREWCHIEF_READER.md).
The local connection probe for this milestone found no available simulator SDK
session, returned `SDK_UNAVAILABLE`, created no capture file and left no reader
process running. The historical five-second canary below belongs to the existing
backend, not to the new extraction.

The September 4 review fixes address frozen-buffer freshness, metadata-only
updates, event/snapshot quality consistency, source-reset privacy, physical
rejoin position across lap deficits, future pit timing, legacy traffic gate
bypass, strategy/diagnosis coupling, corner coast/accounting errors, public
account identifiers, and Windows wheel-path portability. The advisor bridge
also checks that a rejoin estimate belongs to the actual recommendation action.
See [the review-fix record](REVIEW_FIXES.md) for scope and regression coverage.

A valid M2 strategy candidate can now reach the shadow speech policy while
driving-diagnosis promotion remains WAIT. That shadow path does not enable
race audio or vehicle control, promote unsupported driving advice, or make the
product live-accepted. The separate experimental practice-fuel speech above
does not change those gates.

## Last recorded live boundary

The latest clock/retry retest completed one 60-second pyirsdk spectator capture:
3,460 frames, 335 fields, 57.66 Hz and 96.11% tick coverage. Strict replay admission
passed; 3,459 frames normalized as DEGRADED and one as REJECTED for a genuine
751 ms capture gap. There were no equal/decreasing capture timestamps, read
errors, schema changes or session resets. Four SessionInfo records were
captured without interruption; retry counts were not instrumented, so this does
not prove a live metadata race was exercised. The Crew Chief repeat stopped
after 834 frames when the SDK became unavailable; the simulator process was
then absent. That incomplete prefix was correctly NOT_ADMITTED. The subsequent
monitor check could not connect and produced no completion receipt. Nothing was
restarted and all raw evidence stays private.

The preceding comparison used a real online spectator session, with four complete
60-second collector files plus one interrupted prefix. Only three complete
files passed strict replay admission: the remaining file failed player-class
identity consistency despite having a completion receipt. Sampled cross-backend
value parity passed; sustained capture quality and robustness did not. A
five-second live-monitor retry correctly stayed `WAIT_CAR`, with no in-car or
executable output. See [the detailed comparison and remaining issues](CREWCHIEF_READER.md).

The preceding Crew Chief-derived pit-stall capture completed for 60 seconds with 335 fields,
1,321 frames (21.99 Hz), 36.64% tick coverage and 2,284 accounted dropped ticks.
The maximum gap was 29 ticks (0.483 seconds), with no read errors, conflicting
duplicates, stale events, schema changes or session resets. It was a real
`SDK_LIVE` connection with the car stationary in its pit stall, not driven-lap
or pit-sequence evidence. Structural replay passed but quality was **DEGRADED**.
It is distinct from the later spectator comparison and is not a matched
before/after performance experiment.

An earlier session using the existing backend established normal simulator
startup and a real shared-memory canary. Its privacy-safe historical summary is:

- `SourceKind=SDK_LIVE`, full simulator mode and a 60 Hz SDK tick rate;
- 294 persisted frames across a five-second default-cadence capture;
- six accounted dropped ticks, with no conflicting duplicates, stale events,
  schema changes or session resets;
- explicit `OUT_OF_CAR_OR_REPLAY_VIEW` context, so no race-strategy or driving
  readiness claim was admitted.

The collector cadence was previously corrected so `poll_seconds` is a minimum
read-start interval rather than extra sleep added after serialization. The
default 10 ms setting approached the native 60 Hz source in that earlier
full-field, durable-write canary without requiring a 1 ms busy-poll setting.
This is not a same-session comparison with the Crew Chief-derived reader.

A separate `monitor-live` command now normalizes every distinct tick while
emitting only a bounded, privacy-safe state snapshot at a default 2 Hz. It is a
state bridge rather than a recommendation engine: `READY` means the bridge is
usable, not that strategy or driving evidence has passed. Its deterministic,
privacy, stale, cadence and CLI behavior are covered offline. A prior spectator
field check stayed `WAIT_CAR`; the latest post-clock-change attempt found the
SDK unavailable. In-car and post-change live-monitor verification remain pending.

The next live-validation prerequisite is human-driven evidence: configure the
physical driving inputs, enter the car, then record a sufficiently long clean
run and pit sequence. Host-specific telemetry, logs and device details remain
private. Software work also remains: real-time tactical delivery, reliable
background race audio, broader multi-stop planning, and calibrated curb/trail
braking coaching are not made complete by the transport canary or these fixes.

This boundary does not change the product goal and does not justify an
out-of-car canary being relabeled as end-to-end acceptance. Final acceptance
still requires:

1. An authentic, human-driven on-track `SourceKind=SDK_LIVE` capture.
2. Object-exact local admission with advisor-only safety intact.
3. Supported stint/fuel/pit strategy rather than an unsupported guess.
4. Repeated corner evidence supporting at least one driving diagnosis.
5. A deterministic, independently replayable post-session report.

## Evidence boundary

The public repository is privacy-sanitized reusable source. The byte-exact
Aeis deployment packages, host-bound recovery scripts, receipts, telemetry,
EAC/WPR evidence and private remote endpoints remain in a separate private
archive. Public placeholders must never be used to claim identity with those
frozen artifacts.
