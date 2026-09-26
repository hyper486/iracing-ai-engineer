# Public project status

Snapshot: 2026-09-26 EDT

## Coasting explanation phase correction

The Chinese `LONG_COAST` explanation now matches the measured post-brake-release,
pre-throttle-pickup distance. It previously named the lift-to-brake approach
interval instead. The native short answer, bounded provider context and historical
pattern description now keep the correct phase and descriptive/practice-only
limits. Neither numerical thresholds nor lap admission were relaxed.
Invented pedal traces distinguish the two intervals; native production-owner
questions and a fake planner check the resulting text without hardware audio
or real provider access. The targeted group passed 420 tests. The refreshed
unsigned local EXE passed all 28 frozen checks under a system-only child PATH;
its actual bytes match its build manifest. The full repository suite passed:
3,673 passed, 44 skipped. Skipped/data-dependent checks remain unverified;
neither the new wording nor the frozen checks establish hardware/race acceptance.

## Live-trial ingestion and pit-timing corrections (local trial build)

Live-path assumptions have been corrected without promoting racing or audio
acceptance. The Spotter and the stint, pit-observation, motion-profile and
tire-counter owners now treat the frozen-buffer publication counter and
`SessionTick` as independently based clocks. Each must still progress, stay fresh
and reject conflicting duplicates; an absolute counter offset is not a torn read.
The private recording lane now has its own bounded 128 MiB accounting budget
(still at most 128 observations), while analysis keeps its 16 MiB / 0.5 s limits.
This accommodates short write bursts for full-schema/metadata frames; overload
still fails the lane without evicting rows or writing a successful receipt.

Exactly update-bound `Offline Testing` is now an explicit native session type,
not an alias for Race. Its persistent one-lap-to-green bit is excluded from
fuel/corner rejection only with directly observed integer `SessionState=4`.
Raw flags remain intact; unknown context, yellow/red, repair, start lights,
incidents, pits, partial laps and existing clean-lap thresholds remain guarded.
The native optional fuel-fact policy accepts this mode, but no race finish is
invented. A context change invalidates the offline learning cohort/partial lap.

Pit timing additionally accepts direct approaching-pits samples around the SDK
pit-road edges without training clean phase profiles on them. Low-speed stall
settling has a bounded progress high-water tolerance; accumulated reversal,
missing speed, jumps, incidents and source/clock gaps still discard the visit.
Raw positions and strict edge brackets remain unchanged. Current-code replay
of a retained complete pit clip now produces one elapsed-time interval and net
tank-change observation. Missing clean baseline evidence still withholds net
loss and the calibration draft; this is not live strategy acceptance.

Targeted synthetic detector, audio-consumer, reader, recorder, worker and
offline-context tests passed; one relevant regression group passed 516 tests,
and the independent-clock tracker regression group passed 318 tests. After the
pit-boundary fixes, 180 pit/clock/capture/reader tests passed. These are
targeted groups, not a full-suite result. Four orderly sealed private in-car
clips have matching detector replays and
exact-byte capture links. All four final-code numerical recomputations completed;
no learned fuel model or repeated-corner practice fact was admitted. The full
repository suite passed: 3,667 passed, 46 skipped. Skips remain explicit and
do not count as data-dependent, private-deployment or audio acceptance.
The two wheel-build checks skipped for tool discovery also passed separately
after adding the existing local `uv` directory to that test process's PATH.
The updated unsigned local EXE passed all 28
frozen checks under a system-only child PATH; size/hash matched its private
build manifest. Existing separate installations/settings are not changed.
In-car diagnostics were collected privately without a GUI or audio stream;
they do not verify packaged startup, microphone, hearing or end-to-end VR latency.
One sealed clip also retains a natural pit-entry/refuel/exit sequence:
live state withheld lap learning and proximity calls in the pits and resumed
after exit. This is not a calibrated pit-loss, tire-installation or strategy pass;
the complete numerical recomputation runs only after the simulator exits.
After the tracker fix, the private diagnostic also reported a source-bound partial
stint observation from then-current in-car data. It did not infer a tire installation
or reconstruct a pre-attachment stint origin. Replaying its earlier saved pit
sequence under the current owners, not collecting another live visit, exposed
and then checked the additional approach/stall corrections.
The last raw clip is an unsealed prefix after SDK disconnect; its complete
diagnostic journal does not repair that missing raw receipt. It remains private
and is not admitted for complete numerical replay. Previously sealed clips remain
available; no file has been relabeled or given a fabricated receipt.

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
| Privacy-safe live state bridge | Implemented; spectator and in-car source diagnostics | Tick-level normalization feeds bounded snapshots; spectator guard stayed WAIT_CAR and current in-car telemetry reaches the native controller. Packaged/audio/VR acceptance remains pending. |
| Local fuel dashboard | Experimental; offline/synthetic checks | Whole-lap fuel learning, freshness/driver guards and optional bounded private recording are implemented. This is not a multi-stop, traffic, tire or driving-guidance release. |
| Local practice speech | Opt-in browser prototype | Only local English voices in confirmed Practice; Race speech is disabled. A hidden tab auto-mutes, so game-background playback is not guaranteed. |
| DeepSeek engineer framework | Implemented; constrained evidence selection | Opt-in asynchronous questions, local grounding/rendering, bounded attempts and safe fallback; validated historical receipt context is separate from live fuel. Real provider/account and in-car acceptance remain unverified. |
| Native Windows EXE | Experimental native Tk/ttk app | Standalone windowed binary, direct Python service calls, masked/optional DPAPI key storage and private recording. No HTML/WebView/server. |
| First-trial setup overview | Read-only native startup page | Applied voice switches, device/PTT choices, independent module states and setup/replay navigation. No automatic configuration, device test or acceptance promotion. |
| Native VR voice | Implemented; hardware/race acceptance pending | Opt-in background PTT, input/output selectors or refreshed Windows defaults, local Whisper STT and Windows TTS, interruption and optional guarded low-fuel facts. No continuous listening or raw-audio upload. |
| Tick-level proximity | Detector plus opt-in native audio in the local trial build | Independent fixed-phrase cache, priority cancellation, fast snapshots and bounded playback diagnostics; synthetic/local-synthesis checks only, no hardware or in-car acceptance. |
| Reader / analysis / recording isolation | Bounded worker lanes in source | Recorder and analysis failures no longer synchronously block the SDK reader; explicit incomplete prefixes, generation guards and streaming event digests. Not GIL isolation or a hardware latency guarantee. |
| Native reader pacing | Packaged short high-resolution polling pause | Unused 10 ms budget no longer uses an Event timeout; SDK event waits and quality gates are unchanged. Idle timer and synthetic checks are not live sample-rate acceptance. |
| Full-schema SDK char recording | Schema-bound reversible conversion | Raw pyirsdk char bytes retain all octets/NULs in private capture; prior JSON values and v2 replay stay compatible. Generic bytes rejection and independent module failure remain enforced. |
| Routine current-fuel questions | Packaged local trial; synthetic checks | Current observations, learned range and conditional race fuel budgets bypass cloud waits; not live pit tactics or in-car acceptance. |
| Current physical traffic / pit-state questions | Packaged local trial; synthetic checks | Bound ahead/behind distance, player permission and flag facts in native display/PTT; independent of fuel readiness, not time gaps or optimal pit/rejoin advice. |
| Incremental recent-lap coaching | Packaged local trial; synthetic checks | Complete laps feed the existing repeated-pattern model via a bounded worker; local PTT gives an observed loss and practice hypothesis, not a causal gain or live acceptance. |
| Private proximity / audio replay | Packaged local trial; synthetic checks | Bounded local journal, exact detector recomputation, software-playback correlation and optional capture byte links; not audio reproduction, source authentication or human hearing. |
| Native raw capture recomputation | Packaged historical-only local trial; synthetic checks | Separate native tab reuses fuel/corner/stint/pit owners on sealed private captures, with bounded cards and cancellation. No current-state, provider, voice or live-acceptance promotion. |
| Conditional live fuel-stop comparison | Packaged local trial; synthetic checks | Session-scoped hand-entered assumptions, shared complete-lap stop arithmetic, next-fill/stint budgets and local PTT; not mapped pit-entry timing, optimal strategy or future rejoin. |
| Conditional mapped rejoin | Packaged local trial; synthetic checks | User-confirmed entry/exit/full net-loss assumptions, two completed per-car phase profiles, reachable fuel-window endpoints, physical neighbor ranges and local PTT; not measured calibration, optimal timing or live acceptance. |
| Conditional fuel / tire service comparison | Native settings and local questions; synthetic checks | Parallel/sequential four-tire costs, explicit other overhead and scenario-specific complete loss feed up to four mapped rejoin variants. Not tire condition, calibrated performance benefit or a keep/change recommendation. |
| Historical pit-visit observation | Native local question and reviewed draft | SDK-edge elapsed interval, preceding phase-profile net-loss estimate and tank net change; optional stopped-only, revision-bound form draft, never automatically applied or promoted to calibrated future service cost. |
| Observed stint / tire-counter interval / raw pace | Packaged local trial; synthetic checks | Separate stint and tire observations, six consecutive clean-lap median comparison and local questions; not physical tire age, fuel-corrected degradation or a tire-change decision. |
| Driver-confirmed tire installation | Native assertion and continuous counter origin | Explicit parked service assertion, observed exit, strict withdrawal, local question and private assertion audit. Not SDK service truth, independently reviewed history or a live performance-model decision. |
| Paired tire-counter replay | Exact capture-byte and confirmation-frame binding | Current-code historical counter/state recomputation from a sealed capture and v3 journal; legacy unanchored assertions are refused. Not service authentication or original scheduling. |
| Native tire-service review/export | Explicit self-attested workflow | Same-capture exit and identity/source pins, per-row human dispositions, independent local evidence and private CreateNew export. Not automatic approval, reviewer/service authentication or live model admission. |
| VR tire-record confirmation | Two-utterance local PTT workflow | Same parked-visit binding, explicit readback/confirmation and matched analysis-owner acknowledgement. No game control, automatic approval, model request or real hearing acceptance. |
| Tire-model holdout validation | Offline source/CLI; invented-data checks | Frozen-model prediction on source-disjoint stints, explicit car/setup/condition bindings and exact report reconstruction. Not source authentication, statistical coverage, native model admission or real tire calibration. |
| Native tire-calibration preflight | Packaged session-only context matching; synthetic checks | Parked pinned-request verification, owned car/setup/event binding, current compound/weather checks and source/setup withdrawal. Not current tire belief, live model admission or a tire recommendation. |
| Conditional next-stint tire benefit | Native local calculation; invented-data checks | Frozen model plus driver-confirmed origin, whole-stint age/fuel bounds and extra four-tire service yield a net time interval. Not wear, leave-tire safety, mapped rejoin, rule-aware race optimization or actual calibration. |
| Same-action pit briefing | Native local integration; invented-data checks | Actual mapped dose, separate service traffic states and complete-lap tire gain share one action. Partial-lap benefit and full-stint net gain remain unknown; no ranking, wear claim or live acceptance. |
| Advisor-only safety | Required and implemented | No vehicle, simulator-launch or pit-box control path is accepted. |
| Authentic local `SDK_LIVE` acquisition | Proven; private in-car diagnostics retained | The running simulator's read-only shared-memory path produced sealed driving clips, including a pit visit; source corrections were checked against them. |
| Authentic local `SDK_LIVE` acceptance | Product and hardware evidence still pending | In-car acquisition and historical pit recomputation do not establish learned fuel, repeated-corner advice, actual nearby-car speech or headset/VR acceptance. |
| Final strategy plus driving report | Pending live evidence | Both advice gates must pass on an admitted real capture. |

## Native first-trial setup and guidance

The native app now opens **上车检查**. It reports applied voice switches rather
than unchecked/edited widget state, keeps the Spotter lane independent of absent
fuel/analysis evidence, and links to voice setup, recording settings, local
questions, fuel progress and replay. Only fixed preference descriptions appear;
device enumeration is not interpreted as a successful microphone/headset test.
Zero/invalid volume, binding-in-progress, preparation and known voice faults
have explicit next steps. Missing snapshots clear the overview instead of
stopping the UI poll. There is no global green-light or live-acceptance state.

Unavailable Spotter messages now distinguish SDK-off, invalid lateral field,
read error and mismatched ticks with fixed text. Unknown errors remain generic;
source values/exception text do not become messages, and code zero never means
all-clear. The [Chinese quickstart](LIVE_TRIAL_ZH.md) now describes the native
EXE, separate voice switches, parked audio/PTT checks, three on-track loops and
orderly private-recording closure/replay. It no longer sends native users to
the legacy browser/Practice-only voice workflow.

This is setup usability and software evidence, not real hardware or in-car/VR
acceptance. The goal remains active; existing settings are not silently changed.

The unsigned EXE passed all **28 frozen checks** under a system-only child PATH
without saved keys, including the updated native-window startup assertion.
Size/hash matched the build receipt. Source and packaged synthetic windows were
inspected with Computer Use: first page, voice-settings navigation, scrolling
and acceptance limits. Both were closed normally. Tests add 31 pure setup cases,
five fixed-Spotter-reason cases and one isolated native scenario at minimum
width, with no SDK/provider calls or preference mutation. Existing installations,
shortcuts and audio settings were not replaced.

Full regression with `uv` on the child PATH passed **3,547 tests, 44 skipped** in
**742.84 s**, including both locked-wheel checks. All 37 new tests passed; skips
require absent data, private deployment, platform capabilities or explicit local
speech opt-in. Ruff, local document links and public-safety including history
passed. No admission, freshness or audio timing thresholds were relaxed.

## Same-action mapped pit briefing

**综合进站 / 综合进站方案** now joins a mapped action's fuel, service and
traffic with the supported complete-lap portion of its conditional tire gain.
It does not join by an unrelated whole-lap endpoint name or subtract post-exit
tire gain from the immediate rejoin loss. Each service keeps its own traffic
state; unavailable tire or traffic evidence does not erase independent facts.

The current historical model has no spatial partial-lap evidence. Head/tail
segments remain explicitly unmodeled; there is no whole-stint net gain, optimal
ranking or keep/change safety judgment. Local PTT, mandatory provider limits,
selected-evidence withdrawal and fixed fault isolation are included. The new
frozen path exercises real owners with invented fuel/calibration/confirmation
inputs. See [the calculation and limits](LIVE_PIT_BRIEFING.md).

The voice summary is split into a short fuel/service budget and three optional
local subquestions: **综合换胎收益**, **综合仅加油交通**, **综合换胎交通**.
Each takes fresh evidence, not a locked multi-question plan. Four source replies
synthesized to memory in about 8.14–8.23 seconds; selected-device playback,
different voices and end-to-end latency are not verified by that measurement.
The rebuilt unsigned EXE passed **28 frozen checks**, including the numerical
path and a 9.5-second per-reply memory-only speech bound. Artifact hash/size
matched its receipt; Computer Use checked the final native shortcut, scroll and
unavailable answer, then closed the synthetic window normally. No existing
installation or settings were replaced.

Validation adds **36 invented briefing tests, one isolated native scenario and
one overlong-speech rejection case**. Final regression with `uv` on the child
PATH passed **3,510 tests, 44 skipped** in **728.43 s**, including both locked-wheel
checks. Skips require absent data, private deployment, platform capabilities or
explicit local-speech opt-in. An earlier run identified the old 41-fact test
ceiling: the new seven-field allowlist raises it to 48 while retaining the
existing 18,000-byte summary bound and privacy checks. Both 20 Hz and 60 Hz
end-to-end cases now pass. Source/quality/admission thresholds were not relaxed.
Ruff and public-safety including history passed; this remains synthetic/software
evidence, not a real calibration or driving acceptance result.

Full calibration, rule-aware endurance decisions and hardware/in-car/VR
acceptance remain open. The full goal remains active, not completed.

## Conditional next-stint tire benefit

The native app now connects previously separate calibration, confirmed-origin,
fuel-budget and service-cost slices. **问换胎收益 / 比较换胎收益** gives the
modeled next-stint time range minus extra four-tire service without a provider
request. Complete projected ages and lap-start fuel must remain inside observed
training bounds; unsupported endpoints retain explicit reasons and no net
numbers. Normal tire questions can use the same evidence when supported.

The current tire basis remains a driver assertion, not independently reviewed
SDK service truth. The empirical envelope assumes unchanged clean/dry running
and linear-age behavior; marginal bounds do not establish joint support or a
future new/old-tire counterfactual. Negative net gain does not prove old tires
safe. There is no physical wear value, keep/change command, mapped traffic or
event-rule claim. See [the calculation and workflow](LIVE_TIRE_COMPARISON.md).

Model reconstruction runs outside the shared Spotter lock. Unavailable/fault
notices do not disable fuel or proximity. Local answers have selected-evidence
bindings and ten-second expiry; recovery cannot revive a withdrawn answer.
The new frozen-path check builds and verifies an invented request, learns fuel
from eight invented laps, confirms service through the real owner, answers
locally and then withdraws the answer. These are software checks, not actual
calibration, headset hearing or in-car/VR acceptance. The full goal stays active.

Validation added **40 invented-data tests and one isolated native scenario**.
Full regression with `uv` on the child PATH passed **3,472 tests, 44 skipped**
in **703.08 s**, including both locked-wheel checks. Skips require absent data,
private deployments, platform capabilities or explicit local-speech opt-in.
The subsequent minimum-width layout adjustment also passed its isolated native
scenario; supplemental shortcuts now use two rows. Source-mode Computer Use
confirmed the question, unavailable response, expiry and settings notice.
Ruff and public-safety including history passed; the synthetic window closed
normally without using a simulator, microphone or output endpoint.

The rebuilt unsigned EXE passed **26 frozen checks** with a system-only child
PATH and saved keys removed; its size/hash matched the build receipt. Computer
Use confirmed the packaged two-row shortcuts, local unavailable answer,
scrolling and settings notice. The synthetic window was closed normally;
installed copies, shortcuts, saved keys and device selections were not replaced.
The EXE remains a local trial artifact, not a GitHub binary release or in-car pass.

## Native tire-calibration context preflight

The native settings page now accepts a private pinned training/holdout request
and verifies it off the UI thread. Numeric owned-car identity and a versioned
filtered SDK setup fingerprint bind it to the current source. Driver names,
setup names and raw setup values are not copied into app snapshots. Explicit
historical tire measurements are excluded from the setup hash; unknown settings
remain included. Current dry-state, compound and marginal weather-range checks
can reach only `CONTEXT_MATCH_ONLY`, never a live tire decision.

Clear, stale data, source changes and setup/metadata loss withdraw the selection;
a late background result cannot restore an old selection. No persistence,
provider request, tire-service assertion or simulator command is added. Old
captures missing car metadata are not rewritten or relabeled. See
[the workflow and remaining model boundary](LIVE_TIRE_CALIBRATION.md).

Validation added **43 invented-data tests and one isolated native-control
scenario**. Full regression with `uv` on the child PATH passed **3,431 tests,
44 skipped**, in **692.59 s**, including both locked-wheel checks. Remaining
skips require absent data, private deployment, platform capabilities or explicit
local-speech opt-in. Ruff and public-safety including history passed. These are
software checks, not genuine calibration, hardware hearing or in-car evidence.

The unsigned rebuilt EXE passed **25 frozen checks**, including the new
synthetic context-matching/withdrawal check, under a system-only child PATH
without saved keys. Artifact size and SHA-256 matched the new build receipt.
Computer Use confirmed the native input, buttons and no-advice notices were
visible; the synthetic window closed normally. No genuine calibration, SDK,
provider, microphone or output endpoint was used. Existing installations,
shortcuts and saved settings were not replaced. See
[the local trial build](NATIVE_TRIAL_BUILD.md).

Real calibration, model-bound current tire/fuel belief and action-bound tire/service/rejoin
integration remain open. The full goal remains active and unaccepted.

## Frozen tire-model holdout validation

The source CLI now evaluates a pinned training model against separate reviewed
holdout stints, without using those stints to refit or widen it. Full source
receipt separation, per-lap car/setup bindings, unchanged fuel correction and
observed age/fuel/weather domains gate eligibility. Failed predictions remain
in the private CreateNew report with numerical residuals; exact reconstruction
rejects rehashed edits and acceptance promotion. See
[the contract and operator command](TIRE_MODEL_VALIDATION.md).

Validation added **64 invented-data tests**; the focused new/existing tire-model
suite passed **89 tests**. Full regression with `uv` on the child PATH passed
**3,387 tests with 44 skipped** in **637.85 s**, including both locked-wheel
checks. Remaining skips require absent data, private deployments, platform
capabilities or explicit local-speech opt-in. An additional in-memory check of
**2,156 constructed input mutations** produced no unhandled exceptions. Ruff,
public-safety including history, CLI help and exact staged-file/diff review
passed. These are software checks, not genuine tire/service or race evidence.

This milestone used invented data only and changed the source CLI, not the then
existing EXE. Native car/setup context preflight is now implemented above; real
calibration, live model admission and action-bound tire/service/rejoin integration
remain open.
The full goal remains active and unaccepted.

## VR tire-record confirmation

The native PTT path now allows a parked VR driver to record a tire-service
assertion without clicking a window. First an exact phrase creates a local
30-second draft and reads it back; a new explicit confirmation queues one
assertion only if the same fresh service-complete parked episode still holds.
The analysis owner's exact matching receipt gates success speech; queue status
alone does not. Transient movement/service restarts invalidate the original
binding even if recovered before the UI refresh. Proximity, cancellation and
answer withdrawal retain their independent paths. No approximate recognition
or model answer is treated as an action, and attempted unmatched record phrases
are rejected locally. See [the workflow and boundaries](VR_TIRE_CONFIRMATION.md).

The unsigned rebuilt EXE passed **24 frozen checks**, including the real
voice-handler/analysis-owner path with fake audio and a memory-only local
TTS/STT check of the five record/confirm/cancel phrases. Its size/hash matched
the build receipt. Computer Use confirmed the new native instructions were
visible, and the synthetic-only window closed normally. Existing installed
copies, settings, keys and audio selections were not replaced.

Full regression completed with **3,321 passed and 46 skipped** in **658.22 s**.
Two of those skips were locked-wheel checks caused by `uv` not being on the
child PATH; both passed in a subsequent explicit rerun with PATH corrected
(**2 passed**, 6.17 s). The other 44 data/private/platform/opt-in skips remain.
Ruff, public-safety including history and exact staged-file/diff review passed
before publication. None of these results promotes real racing acceptance.

Synthetic tests and explicit memory-only TTS/STT checks do not establish real
microphone accuracy, hearing, SDK/service truth, end-to-end latency, tire-model
calibration or VR acceptance. Real reviewed calibration and live tire/service/
rejoin integration remain open; the final goal is still active.

## Native tire-service review/export

The native capture tab now has a manual post-session workflow that produces
the existing offline tire-service-history format. It maps actual captured
off-pit samples, not the earlier confirmation tick, and derives the same adapter
input-evidence and event-identity hashes as the strategy consumer. Every row
requires an explicit disposition; reviewed rows require independent retained
local evidence. Choices and the human declaration start empty/unchecked.
Partial and unreviewed service retain the existing tire-age withdrawal behavior.

Export revalidates the pair and plan, hashes bounded local evidence, creates new
private review/history files and does not overwrite originals. The exported
history can be passed to the existing finalizer/verifier; it does not load a
live model. Receipts explicitly remain self-attested and unauthenticated.
See [the complete workflow and limits](TIRE_SERVICE_REVIEW.md).

Full regression passed **3,273 tests, with 44 skips**, in **739.30 seconds**.
The final layout correction also passed an isolated native review rerun.
The unsigned rebuilt EXE passed **22 frozen checks** (fourteen numerical,
five native/runtime, three memory-only voice) with a system-only child PATH
and saved keys removed; its size/hash matched the receipt. Computer Use found
and corrected low-contrast unselected table rows and a clipped export button.
Corrected source and final frozen inspection confirmed visible rows, blank
choices, unchecked declaration and the export control; the frozen UI refused
submission without review inputs. Both final QA windows were closed normally.
Ruff, public-safety including history and exact staged/diff review passed.
Real SDK/service, microphone/headphone and VR acceptance remain open. Installed
copies, shortcuts and saved settings are unchanged.

## Paired tire-counter replay

The native capture tab can now join one complete raw capture with its completed
v3 assertion journal. Exact full-file hash/size and one-to-one frame anchors are
required. It reuses the current tire owner, reports accepted/rejected assertions
and bounded counter/suspension/withdrawal history, without feeding historical
data to live speech, questions or strategy. Generation-locked assertion enqueue
also prevents a reconnect from reordering an older receipt after a reset.

Legacy v1/v2 journals remain readable; old unanchored tire assertions cannot be
used for paired recomputation. A mismatch or incomplete pair returns no partial
tire result. The manual export workflow is now implemented above; obtaining real
reviewed labels and live tire-performance integration remain open.
See [the complete workflow and boundary](TIRE_CAPTURE_REPLAY.md).

Full regression passed **3,249 tests, with 44 skips**, in **626.87 seconds**.
Ruff, public-safety including history and exact staged-file/diff review passed.
The unsigned rebuilt EXE passed **21 frozen checks** (thirteen numerical,
five native/runtime, three memory-only voice) under a system-only child PATH
with saved keys removed; its size/hash matched the build receipt. Computer Use
checked the paired button and instructions in both source and final frozen
synthetic windows, then closed both normally. Isolated native tests cover the
two selected paths, second-picker cancellation and report rendering.
No authentic source, service, microphone/headphone or VR acceptance has been
added; installed copies, shortcuts and saved settings remain unchanged.

## Driver-confirmed tire installation prerequisite

The native settings now accept a current-visit driver assertion after observed
entry and completed stopped service. A full-new assertion plus continuous exit
can establish a counter origin; unchanged service only preserves a known one.
Missing/partial service, discontinuities and tire-context changes withdraw it.
The local tire question identifies its human-confirmed basis explicitly.

That milestone's v2 assertion lane was fixed numeric/enum data. Its updated
replay accepted v1 logs and reported assertions without claiming tire-age
recomputation or verified service contents. This is not yet live calibrated
tire-benefit integration. Paired v3 replay is now implemented above; reviewed
label export is now available through the separate explicit manual workflow.
See [the workflow and limits](LIVE_TIRE_INSTALLATION.md).

At that installation milestone, full regression passed
**3,221 tests, with 44 skips**, in **623.84 seconds**.
Ruff, public-safety including history and exact staged-file/diff review passed.
The unsigned rebuilt EXE passed **20 frozen checks** under a system-only child
PATH with saved keys removed; its size/hash matched the build receipt.
Computer Use checked the source and final frozen synthetic settings windows;
both were closed normally. Isolated native tests cover confirmation availability,
queued/moving states and delegation. No authentic SDK, service,
microphone/headphone or VR acceptance has been added. Existing installations,
shortcuts and saved settings are unchanged.

## Conditional service costs connected to rejoin

The native comparison now connects next-stop fuel, entered four-tire service
time and mapped traffic. Parallel and sequential work produce different
incremental costs. Complete component mode requires explicit additional
overhead, even when zero; missing cost is never silently assumed away. Fixed
complete totals remain supported as a separate mode. Each mapped fuel/tire
variant receives its own loss range, neighbor envelope and withdrawal reason.

The local **换胎会多花多久** question and **问换胎耗时** shortcut give a short
conditional answer without waiting for DeepSeek. Full native text retains the
assumptions and does not turn the fuel-only counterfactual into safe tire
retention advice. See [the setup, formulas and limits](LIVE_SERVICE_COMPARISON.md).

At that service-comparison milestone, full regression passed
**3,144 tests, with 44 skips**, in **610.51 seconds**;
the focused suite passed 515 tests. Ruff, public-safety including history and
exact staged-file/diff review passed. The unsigned rebuilt EXE passed all
**19 frozen checks** under a system-only child PATH with saved keys removed.
Visible synthetic source/frozen settings QA checked the new fields; source QA
also checked Chinese choices, scrolling and explanation. It did not open the
SDK, audio devices, private captures or provider. Installed copies, shortcuts
and saved settings were not replaced. Real service calibration, tire-benefit
integration, hearing and VR acceptance remain open.

## Full-schema char capture compatibility

Valid pyirsdk char fields no longer stop native recording merely because the getter
returns bytes. Only validated char descriptors admit a reversible byte-to-codepoint
mapping; all octets and NUL padding survive. The unchanged generic JSON guard still
rejects unsupported bytes in other fields/metadata/writer input. Duplicate evidence
uses the persisted representation, and old supported JSON captures retain their
bytes. No raw char content is promoted into live questions, model inputs or voice.
See [the encoding, privacy and verification contract](SDK_CHAR_RECORDING.md).

Synthetic tests cover actual pinned SDK getters on anonymous memory, complete
private recording, strict historical replay and continued proximity after a
malformed recording field. The packaged recomputation fixture now also verifies
char octets and marker exclusion. This is not real-game, headset or VR acceptance.
The rebuilt unsigned local EXE passed all **18 frozen checks**, including that
extended capture check, under a system-only child PATH with no provider credentials.
Existing installed copies, shortcuts and saved settings remain untouched.

Full regression passed **3,100 tests, with 44 skips**, in **590.42 seconds**.
The skips retain existing data/platform/private-deployment and opt-in boundaries.
Ruff, public-safety including history and exact staged review passed. These checks
do not establish authentic SDK collection, hardware hearing or VR acceptance.

## Native reader pacing correction

The connected reader now sleeps only for its unused 10 ms minimum-poll budget,
using the Python runtime's short high-resolution sleep. The stop Event still
interrupts disconnected retries. Stop during a short sleep is observed afterwards;
the requested pause is capped at 10 ms, not a hard scheduling/shutdown deadline.
No system timer settings, SDK data-event semantics, field decoder, recording
format or lap-quality thresholds are changed.

An anonymous-memory diagnostic measured roughly 0.25 ms median field decoding,
so no replacement decoder was introduced. A separate idle timer comparison
measured about 15.07 ms median for the former 10 ms Event timeout versus 10.09 ms
for the new pause. These are local timer measurements, not current game sampling
rate or proof of the historical missing-tick cause. See
[the diagnostic, tests and remaining acceptance](SDK_READER_PACING.md).

The rebuilt unsigned local EXE passed all **18 frozen checks** with a system-only
child PATH and no provider credentials. These exercise numerical, Tk/runtime and
memory-only voice paths, not a real microphone/speaker or simulator. Installed
copies, shortcuts and saved preferences remain unchanged.

Full regression passed **3,073 tests, with 44 skips**, in **593.01 seconds**.
Skips retain the existing data, platform, private-deployment and opt-in boundaries.
Ruff, public-safety including history and exact staged-diff checks passed. No
live acquisition, hardware hearing or VR acceptance is inferred from these gates.

## Native sealed capture recomputation

The **采集复盘** tab directly accepts completed private raw captures, so one
recorded session can support repeated local numerical diagnosis. It reuses the
existing collector validator and native analytical owners, preserving quality
boundaries and missing evidence. Results are historical-only, bounded to 128
recent cards and four latest category cards, never attached to current questions
or audio. Offline-only execution cancels when a simulator connection appears.
User-entered strategy assumptions are not recorded in these clips and are not
silently borrowed from current settings. See [the workflow](NATIVE_CAPTURE_REPLAY.md).

Source tests exercise invented sealed clips, including a 403-field file from
the actual app recorder, not authentic live acceptance. A discovered validator
bug is corrected: the tick-rate set is now sorted/unique even when schema epochs
repeat or reduce the rate. Schema changes still preserve their quality boundary.
The rebuilt unsigned EXE passed all **18 frozen checks**, adding a temporary
invented sealed capture through the real historical owner. Existing installed
copies and device/API settings remain untouched.
The final endurance goal and real microphone/output/VR trial remain open.

## Native historical pit observation and reviewed setup draft

**问耗时 / 这次进站用了多久** now uses local fixed facts from a complete observed
pit-road entry/exit. Timing survives unavailable fuel/opponent models; missing
baseline or service signals cannot invent full loss or service contents. A
two-profile historical counterfactual is separate from observed road time;
negative estimates stay negative. SDK boundaries are not surveyed merge points,
and outside-boundary braking/acceleration loss is not included.

The native settings page can fill four rejoin fields from an eligible visit
only while stopped. Filling does not apply/persist assumptions or touch other
fields. User review and explicit confirmation remain required, with a fresh
revision/source check at application. The result remains `USER_RULE`, not
calibrated future pit cost. The local answer has a ten-second TTL and latched
withdrawal, independent of fuel-model readiness. See
[the contract and workflow](LIVE_PIT_OBSERVATION.md).

The new slice has 59 synthetic observation/query/controller/PTT cases and one
isolated native draft scenario. A separate 1,995-call malformed-snapshot sweep
finished without uncaught exceptions after hardening the draft's source guard.
The rebuilt unsigned local EXE passed all **17 frozen checks**, including the
invented visit through the real owner/local-query/draft path. Visible synthetic
QA confirmed the settings/shortcut layout, disabled unavailable draft, explicit
no-data response and expired-answer withdrawal; the window was closed normally.
No game, physical microphone/speaker, saved credentials or existing installation
was opened or changed for this test. Real pit geometry/service calibration and
in-car headset/VR acceptance remain open.

Full regression passed **3,010 tests, with 44 skips**, in **561.25 seconds**.
The skips retain their data/platform/private-deployment and explicit opt-in
boundaries. Ruff, public-safety including history and exact staged-diff checks
also passed. These gates are not real SDK/driver/headset acceptance.

## Previous milestone: conditional native mapped rejoin

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

That milestone's **500-lap / 892,192-frame** virtual 60 Hz run passed all eight
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

This earlier milestone sent slow analysis and optional private recording to
separate single-owner lanes, each then bounded to 128 observations / 16 MiB of retained
payload accounting including active work. The current recording-only burst budget
is described at the top of this document; analysis limits are unchanged.
Sink construction, writes, finalization,
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
