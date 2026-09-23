# Public project status

Snapshot: 2026-09-23 EDT

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
| Tire reasoning | Implemented as a performance belief | The project does not claim direct physical tire wear without a supported source. |
| Corner diagnosis | Implemented for repeated comparable evidence | Curb/risk claims remain blocked without trusted labels. |
| Deterministic reports | Implemented | JSON and script-free HTML outputs preserve provenance and limitations. |
| Privacy-safe live state bridge | Implemented; spectator-only field check | Tick-level normalization feeds bounded JSONL snapshots; spectator guard stayed WAIT_CAR. In-car validation remains pending. |
| Local fuel dashboard | Experimental; offline/synthetic checks | Whole-lap fuel learning, freshness/driver guards and optional bounded private recording are implemented. This is not a multi-stop, traffic, tire or driving-guidance release. |
| Local practice speech | Opt-in browser prototype | Only local English voices in confirmed Practice; Race speech is disabled. A hidden tab auto-mutes, so game-background playback is not guaranteed. |
| DeepSeek engineer framework | Implemented; constrained evidence selection | Opt-in asynchronous questions, local grounding/rendering, bounded attempts and safe fallback; validated historical receipt context is separate from live fuel. Real provider/account and in-car acceptance remain unverified. |
| Native Windows EXE | Experimental native Tk/ttk app | Standalone windowed binary, direct Python service calls, masked/optional DPAPI key storage and private recording. No HTML/WebView/server. |
| Native VR voice | Implemented; hardware/race acceptance pending | Opt-in background PTT, input/output selectors or refreshed Windows defaults, local Whisper STT and Windows TTS, interruption and optional guarded low-fuel facts. No continuous listening or raw-audio upload. |
| Tick-level proximity | Detector plus opt-in native audio in source | Independent fixed-phrase cache, priority cancellation, fast snapshots and bounded playback diagnostics; synthetic/local-synthesis checks only, no hardware or in-car acceptance. |
| Reader / analysis / recording isolation | Bounded worker lanes in source | Recorder and analysis failures no longer synchronously block the SDK reader; explicit incomplete prefixes, generation guards and streaming event digests. Not GIL isolation or a hardware latency guarantee. |
| Routine current-fuel questions | Local source implementation; synthetic checks | Current observations, learned range and conditional race fuel budgets bypass cloud waits; not live pit tactics or an updated EXE. |
| Current physical traffic / pit-state questions | Local source implementation; synthetic checks | Bound ahead/behind distance, player permission and flag facts in native display/PTT; independent of fuel readiness, not time gaps or optimal pit/rejoin advice. |
| Advisor-only safety | Required and implemented | No vehicle, simulator-launch or pit-box control path is accepted. |
| Authentic local `SDK_LIVE` acquisition | Proven before acceptance | The running simulator's real shared-memory transport has produced a complete, sealed canary capture. |
| Authentic local `SDK_LIVE` acceptance | Pending on-track evidence | Out-of-car, stationary pit-stall and spectator captures do not support strategy or driving acceptance. |
| Final strategy plus driving report | Pending live evidence | Both advice gates must pass on an admitted real capture. |

## Local traffic and pit-state question milestone

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
