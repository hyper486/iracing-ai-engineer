# Endurance RPY intake

## Frozen facts

| Item | Value |
|---|---|
| Remote file | Private Windows-sidecar locator; excluded from Git |
| Size | 11,782,056,972 bytes (10.97 GiB) |
| SHA-256 | `d5d6ac61fcb04f51ab45e62e89f1f4fd81405eb5e051445a4be6b4044f3d421b` |
| File prefix | `59 4c 50 52` / `YLPR` |
| Current location | Windows sidecar only; not copied into the project |
| Current status | `REFERENCE_ONLY / REPLAY_SDK_PROBE_REQUIRED` |

## Dated sidecar snapshot

Observed at `2026-08-07T13:23:29Z`. Windows `app.ini` had memory SDK and disk telemetry enabled,
while automatic disk logging was disabled:

- `irsdkEnableMem=1`
- `irsdkEnableDisk=1`
- `irsdkAutoLogDisk=0`
- `irsdkLog360Hz=0`

At inspection time, the C: drive had 830.45 GiB free and the telemetry directory contained 40 files totaling 0.658 GiB. Capacity is not currently a blocker.

The read-only `sdk-probe-v1` client is installed in a private, isolated Windows-sidecar virtual environment. Its wheel and installed source hashes were matched after installation. With the simulator absent, the production entry point returned exit code 2 and one valid JSON `SDK_UNAVAILABLE` document; no iRacing setting or replay file was modified.

## Why it does not go directly to the IBT reader

`.rpy` is a replay container read by the native iRacing replay player, not the SDK `.ibt` disk-telemetry layout. There is no frozen, supported RPY decoder, and no evidence that the container contains the original driver's complete pedal, steering, fuel, and tire time series. When the IBT adapter sees the observed `YLPR` prefix, it rejects the file immediately rather than interpreting private-container bytes as variable counts and offsets.

## Verifiable path forward

1. Load the replay in the iRacing UI and play a segment at normal speed that includes on-track driving and a pit stop.
2. Connect read-only to the Windows shared-memory SDK and record the schema, replay frame, SessionTime, and the presence, variability, and tick continuity of target fields.
3. Label replay SDK output `PROBE_ONLY`; even if fields are present, do not immediately treat them as strategy or driving ground truth.
4. Use the replay image to verify laps, lap times, pit intervals, and the target vehicle before deciding whether to create derived data.
5. Derived data must preserve the parent RPY SHA-256, iRacing build, playback speed, pause/seek history, SDK schema fingerprint, missing or constant fields, time range, and its own SHA-256.

Important: changing playback speed, pausing, or dragging the timeline breaks a simple 60 Hz time assumption. The first probe should use continuous `1x` playback without seeking.
