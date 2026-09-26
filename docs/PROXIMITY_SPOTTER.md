# Proximity detection and native priority audio

This covers Stage A detection and the Stage B source implementation of
[the active goal](ACTIVE_GOAL.md). The pure detector never opens an audio device,
calls a language model, connects to the simulator by itself or issues controls.
The separate native voice consumer can play local audio only when explicitly
enabled. Hardware, in-car and VR acceptance are still open.

## Input and decisions

`ProximitySpotter` consumes the existing read-only transport's frozen frames.
It interprets the public `CarLeftRight` enum from
[pyirsdk 1.3.6](https://github.com/kutu/pyirsdk/blob/1.3.6/irsdk.py):

| Value | Meaning |
|---|---|
| 0 | SDK spotter off / unavailable, never clear |
| 1 | Both sides clear |
| 2 / 3 | Car on left / right |
| 4 | Cars on both sides |
| 5 / 6 | Two cars on left / right |

This uses SDK field meanings, not copied third-party application logic or
sound packs. Opponent lap times, lap-distance arrays, fuel learning and completed
laps are not prerequisites. It does not infer rear gaps, passing rights, flags
or safe maneuvers from these lateral occupancy codes.

Admission requires fresh increasing session time and independent session/buffer ticks,
known session/player indices, exact full-simulator provenance and affirmative
on-track/in-car context without replay, pit road or pit stall. Missing fields,
invalid types, relevant read errors and code zero produce a fixed reason instead
of a guessed value. Repeated ticks do not renew freshness.

The SDK publication-buffer counter and the `SessionTick` payload counter need not
have the same origin. Absolute inequality is not a torn read. The transport still
verifies one stable frozen buffer before and after copying. The detector separately
checks both counters for duplicate conflicts, regressions and gaps; neither an
advancing buffer with frozen session data nor advancing payload data under a
reused buffer counter can keep an old alert alive. Old trial journals that recorded
the former equality refusal can disagree with this corrected detector during
current-code replay; such a mismatch is not converted into a replay or hearing pass.

Default configuration uses a 250 ms freshness limit, 30 ms occupied confirmation,
150 ms clear confirmation, five-second still-alongside reminders and a 750 ms
candidate deadline. These are policy parameters, **not measured audio latency**.
Confirmation uses progressing session time, while capture age and expiration use
the monotonic clock. Loss of continuity, session/driver change or disconnection
withdraws candidates and reacquires context; it never invents an all-clear call.
Starting or recovering on a clear track is silent.

## Integration and diagnostics

The live reader feeds the detector before admitting work to independent bounded
recording and analysis lanes. AppState protects the detector with its state lock and
samples observation time while holding that lock. Snapshot polling can therefore
expire stale state without creating a spurious clock regression in a waiting
reader. Detector exceptions latch a separate error until reconnection; they do
not terminate fuel analysis or reveal exception text.

The native source UI displays detector readiness and a separate voice status.
Detector `READY` means usable data, not a speaker/hearing/on-track pass. The pure
detector's legacy `audio_status=NOT_CONNECTED` and `audible=false` describe that
detector alone; the actual native consumer is reported under `voice.spotter`.
Neither promotes `live_acceptance`. Existing binaries do not change with source.

Candidates form a latest-only mailbox, not a backlog. The native consumer binds
connection generation plus detector epoch/sequence, rechecks fresh readiness and
deadline before playback, and stops obsolete playback. It reads the fast detector
snapshot rather than the 2 Hz display/engineer snapshot; short transitions need
not appear in the slow display to reach the audio consumer.

The in-memory audit retains the most recent 128 fixed-shape decisions, aggregate
counts and a rolling SHA-256 digest seeded by contract/configuration/tick rate.
It records candidate, supersession, expiry, invalidation and health changes,
but no driver identity, raw metadata or exception strings. This is a diagnostic
digest, not proof of authentic input, hearing or full-session replayability.

## Reproducible checks

```powershell
uv run pytest -q tests/test_spotter.py tests/test_live_app_reader.py tests/test_desktop_window.py
uv run python scripts/rehearse_spotter.py
```

The standalone rehearsal generates 300 invented frames and checks left,
both-sides, right and all-clear transitions. Its report always says `SYNTHETIC`,
`audible=false` and `live_acceptance=false`. It uses no game, network, key,
microphone or speaker. Tests additionally cover flicker, expiry, duplicates,
missing/invalid data, replay/out-of-car guards, resets, clock behavior, a short
pass between two slow display refreshes and a locally contained detector fault.

A separate accelerated synthetic check fed 1,296,000 invented 60 Hz frames
(six simulated hours), cycling all six occupancy states. The detector remained
ready and retained only 128 audit rows. This is neither a six-hour wall-clock soak
nor a full reader/recorder/audio/VR endurance acceptance test.

## Open boundaries

- The native audio lane below is implemented in source, but actual headphones,
  human speech, output latency, VR load and packaged integration remain unaccepted.
- Recording and broader analysis now have separate worker threads as described
  below. SDK reads, metadata binding and bounded input inspection still run on the
  reader. Threads do not isolate CPU/GIL contention or a hung native SDK operation;
  this is not hard real-time scheduling or measured VR latency isolation.
- The raw private recorder exists, but deterministic reconstruction of this new
  event-to-audio chain from a complete captured session remains Stage E work.
- No authentic in-car proximity, human hearing, VR frame-time or new packaged-EXE
  acceptance has been performed. Strategy and driving acceptance are unchanged.

## Bounded capture and analysis lanes

`FrameWorker` owns a sink's construction, processing, byte-count access, finish
and close. The SDK producer queues frozen transport-owned objects and never waits
for a slow sink inside its sampling loop. Each lane admits at most 128 observations.
Analysis has a 16 MiB conservative retained-payload budget; full-schema recording
has a separate 128 MiB budget to absorb short write bursts with larger metadata.
Both budgets include in-flight work and retain the same terminal overflow behavior;
these are neither serialized-file size nor a total-process RSS guarantee. Queue
entries may include duplicate SDK ticks. Status contains counts, fixed reason
codes and the last owner-observed committed bytes, which can lag an active write.

Overflow is a terminal lane error, not an oldest-frame eviction policy. It rejects
the triggering observation, counts discarded pending work and preserves only the
written prefix without appending a successful completion receipt. Recorder setup,
write, finalization and close errors stay in that lane. A configured file-cap stop
drains all admitted work and may seal that bounded prefix; it does not claim to
have captured the rest of the session. Disconnect drains without marking complete.

Analysis additionally rejects a queue whose oldest active/pending observation is
more than 0.5 seconds old. Published fuel retains its original observation time;
an errored lane withdraws it and cannot republish a late result. A connection
generation prevents an old worker from publishing into a new session. Reconnection
does not create replacement owners while predecessors are alive: proximity keeps
running, analysis can resume once its old owner exits, and a recorder still alive
at reconnection disables recording until a deliberate reader restart. Native
status distinguishes this from currently recording or being disconnected.

Live telemetry events use incremental canonical-array hashing and aggregate
counts instead of a race-long duplicate event list. Event sequences and receipts
are byte-compatible with the retaining offline pipeline. The live app no longer
stops its SDK reader at 50,000 events. Test coverage includes 50,100 invented
rejected observations, exact digest equivalence, blocked sinks, late analysis,
reconnect/recovery and failed SDK release. These are synthetic fault checks, not
hardware endurance evidence. A failed SDK release prevents another SDK owner.
Final app shutdown closes the SDK before waiting for slow lane teardown; it
truthfully remains pending if a sink never returns rather than claiming closure.

## Native voice consumer (Stage B source)

The independent `SpotterVoice` worker pre-renders nine fixed proximity phrases
and two health/cancellation notices. It warms device enumeration, validates
bounded PCM16 WAV, and keeps the cache in memory. Proximity phrases use Windows
[SpeechSynthesizer.Rate](https://learn.microsoft.com/en-us/dotnet/api/system.speech.synthesis.speechsynthesizer.rate)
at +3; normal questions keep their original rate. Only exact-zero leading/trailing
PCM is trimmed, retaining 20 ms margins and all nonzero speech/internal pauses.
Source-provided free text is never spoken on this lane.

The local synthesis-only check produced proximity clips of approximately
0.80-1.16 seconds, versus approximately 1.78-2.29 seconds before acceleration and
trimming. These are file durations for one local voice, **not audio-start or
SDK-to-ear latency**. Cold device enumeration was about 300 ms and warmed calls
about 55 ms in a separate no-stream check; this is why preparation precedes readiness.

`PriorityAudio` gives proximity higher priority than normal PTT/answers; deferred
informational notices have lower priority than new PTT. It cancels the current
owner and waits for actual release, never opening overlapping streams. An owner
that fails to release cannot create an unbounded backlog: the urgent attempt is
bounded by the candidate deadline and a 250 ms wall-clock acquisition limit.
Device resolution/open is followed by another start guard. A 20 ms guard checks
active playback against fresh occupancy/context; these are scheduling parameters,
not hard real-time or measured VR latency guarantees.

The candidate TTL is a **start-by** deadline. Once started, a bounded phrase can
finish while still supported even after its mailbox TTL expires. Side changes,
stale data, session changes, stop and reconfiguration cancel obsolete output.
A newer explicit stop also wins over an older settings save still in flight;
only a subsequent apply may resume the proximity worker.
Microphone capture currently shares audio ownership rather than running duplex:
an urgent event interrupts a held question, discards the partial PCM even if the
driver releases simultaneously, and never submits that prefix to STT/DeepSeek.
A deferred local notice explains cancellation after sides are clear. New PTT
can interrupt that informational notice without interrupting a proximity call.

After previously being ready, sustained data loss can produce one cached warning
per outage, with a 30-second repeat floor. Out-of-car/pit transitions do not invent
clear calls or normal startup warnings. Output errors are latched until settings
are reapplied; there is no silent fallback to another named device. A physically
failed/muted output cannot be promised an audible fault warning: the native status
remains the independent visible signal.

`voice.spotter` separates preparation, waiting for data, ready-to-attempt, playing,
paused, failed and closed states. The bounded audit records attempts, playback
start/completion, cancellation, pre-start drops and start deadline misses. Its
`start_delay_ms` measures the consumer attempt to the backend start call, not
first audible sample. `heard=false` and `live_acceptance=false` remain explicit.
Raw driver identity, microphone data, device labels, keys and backend errors are
not included in this audit. Stage E's new [private trial journal](PRIVATE_TRIAL_REPLAY.md)
persists the fixed proximity/health/audio projections and optional raw-capture
byte links. Silent offline replay recomputes detector decisions and correlates
software receipts; it cannot reproduce hardware timing or prove hearing. The
rest of integrated live acceptance remains open.

```powershell
uv run pytest -q tests/test_priority_audio.py tests/test_spotter_voice.py tests/test_voice_service.py tests/test_voice_audio.py tests/test_voice_windows.py tests/test_voice_settings.py
```

These tests use invented frames/fake devices and cover every blocked question
phase, partial recording cancellation, output faults, slow device open, cached
fixed phrases, settings migration, close and a raw-frame-to-audio path with no
fuel/display snapshot. A separate visible synthetic native preview checked the
new control layout and fixed mouse-wheel access to lower voice controls. It
connected to neither the SDK nor credentials, microphone, speaker or provider.
