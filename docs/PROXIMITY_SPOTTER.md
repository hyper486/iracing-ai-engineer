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

Admission requires fresh increasing ticks/time, a matching frame/header tick,
known session/player indices, exact full-simulator provenance and affirmative
on-track/in-car context without replay, pit road or pit stall. Missing fields,
invalid types, relevant read errors and code zero produce a fixed reason instead
of a guessed value. Repeated ticks do not renew freshness.

Default configuration uses a 250 ms freshness limit, 30 ms occupied confirmation,
150 ms clear confirmation, five-second still-alongside reminders and a 750 ms
candidate deadline. These are policy parameters, **not measured audio latency**.
Confirmation uses progressing session time, while capture age and expiration use
the monotonic clock. Loss of continuity, session/driver change or disconnection
withdraws candidates and reacquires context; it never invents an all-clear call.
Starting or recovering on a clear track is silent.

## Integration and diagnostics

The live reader feeds the detector before synchronous recording and the slower
fuel/display publication. AppState protects the detector with its state lock and
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
- Transport, synchronous recording and broader analysis still share a reader
  thread. A blocked writer can delay subsequent reads, and a non-spotter analysis
  exception can still force reconnection. Full fault and latency isolation is
  not established by this slice.
- The raw private recorder exists, but deterministic reconstruction of this new
  event-to-audio chain from a complete captured session remains Stage E work.
- No authentic in-car proximity, human hearing, VR frame-time or new packaged-EXE
  acceptance has been performed. Strategy and driving acceptance are unchanged.

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
not included in this audit; full durable session replay remains Stage E.

```powershell
uv run pytest -q tests/test_priority_audio.py tests/test_spotter_voice.py tests/test_voice_service.py tests/test_voice_audio.py tests/test_voice_windows.py tests/test_voice_settings.py
```

These tests use invented frames/fake devices and cover every blocked question
phase, partial recording cancellation, output faults, slow device open, cached
fixed phrases, settings migration, close and a raw-frame-to-audio path with no
fuel/display snapshot. A separate visible synthetic native preview checked the
new control layout and fixed mouse-wheel access to lower voice controls. It
connected to neither the SDK nor credentials, microphone, speaker or provider.
