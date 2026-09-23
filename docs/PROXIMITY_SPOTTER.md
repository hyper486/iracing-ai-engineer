# Proximity detector: diagnostic Stage A

This is the first software slice of [the active goal](ACTIVE_GOAL.md), not an
audible spotter release. It never opens an audio device, calls a language model,
connects to the simulator by itself or issues vehicle/pit commands.

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

The native source UI displays readiness and explicitly states that proximity
audio is not connected. `READY` means detector data is usable, not that a voice,
speaker, selected headset or on-track product check has passed. Snapshot output
includes `audio_status=NOT_CONNECTED`, `audible=false` and `live_acceptance=false`.
Existing desktop binaries are not changed by editing source.

Candidates form a latest-only mailbox, not a backlog. A future audio consumer
must bind the connection generation plus detector epoch/sequence, recheck fresh
readiness and deadline immediately before playback, and stop obsolete playback.
That consumer is not implemented here. A transition between two display refreshes
is present in the detector audit even if neither display snapshot sees it; an
audio consumer must not use that slow display cadence.

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

## Open boundaries

- Priority audio, cached phrases, output arbitration, long-answer interruption
  and selected-device playback remain Stage B work.
- Transport, synchronous recording and broader analysis still share a reader
  thread. A blocked writer can delay subsequent reads, and a non-spotter analysis
  exception can still force reconnection. Full fault and latency isolation is
  not established by this slice.
- The raw private recorder exists, but deterministic reconstruction of this new
  event-to-audio chain from a complete captured session remains Stage E work.
- No authentic in-car proximity, human hearing, VR frame-time or new packaged-EXE
  acceptance has been performed. Strategy and driving acceptance are unchanged.
