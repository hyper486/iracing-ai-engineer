# Experimental local fuel dashboard

This Windows prototype displays read-only iRacing fuel estimates in a local
browser. It is **experimental and fuel-only**, not a released endurance strategy
or driving coach. This milestone adds software and offline/synthetic checks;
it does not establish new authentic `SDK_LIVE` driving acceptance. Full milestone
gate results and browser-check boundaries are recorded in
[the public status](PUBLIC_PROJECT_STATUS.md).

The app does not launch iRacing, drive the car, change pit-black-box settings,
or issue a "box now" or tire-service instruction. Existing strategy and driving
admission gates are unchanged. Multi-stop timing, traffic/rejoin decisions,
tire decisions and corner coaching are not integrated into this dashboard.

## Start on Windows

Prepare Python 3.12 with `uv sync --python 3.12` from the repository root. Start
iRacing yourself, or leave the app waiting while you start it later. Then run
this command in PowerShell from the repository root:

```powershell
.\scripts\start_live_engineer.ps1
```

The launcher starts an owned Python worker with a hidden window, opens
[the local dashboard](http://127.0.0.1:8765/), and defaults to a six-hour run.
There is no scheduled task or automatic simulator launch. The service binds
only to `127.0.0.1`; it is not a LAN dashboard. Use the displayed numeric URL,
not a hostname alias.

The launcher defaults to private raw recording. For a one-hour trial without
raw recording:

```powershell
.\scripts\start_live_engineer.ps1 -Hours 1 -NoRecording
```

Launcher options:

- `-Hours`: run duration, 1–12 hours; default 6.
- `-Port`: local HTTP port; default 8765.
- `-ReserveLiters`: fuel kept out of the estimated usable range; default 2.
- `-TankCapacityLiters`: optional verified usable tank capacity for the car and
  event. The default leaves capacity unknown; do not substitute an assumed car
  value or ignore an event fuel-capacity restriction.
- `-NoRecording`: disable raw telemetry recording for this run.
- `-NoBrowser`: start the worker without opening a browser tab.

If a compatible worker already occupies the chosen port, the launcher reuses
it and does **not** apply new options. In particular, adding `-NoRecording` does
not disable recording in an already running worker. Closing the browser does
not stop the worker. A new configuration requires stopping the existing owned
worker first or waiting for its duration to end. The launcher prints its owned
launch-process ID. Windows virtual environments can create a Python child; if
stopping early, target only that verified process tree (`taskkill /PID <printed-id>
/T /F`), not every Python process. Force-stopping can leave an incomplete capture.

For a foreground run that can be stopped with Ctrl+C:

```powershell
.\.venv\Scripts\python.exe scripts\run_local_cli.py live-app --duration-seconds 3600
```

The direct CLI defaults to **no raw recording** unless `--record-directory` is
provided. Its default run duration is also six hours. Options include
`--port`, `--reserve-liters`, `--minimum-valid-laps` (default 5, minimum 2),
`--tank-capacity-liters`, `--record-directory` and `--record-max-mib` (default
4096). The app currently uses the default pyirsdk reader, not the optional
Crew Chief collection backend.

## What to expect while driving

Enter the car in a practice session for the initial trial. A shared-memory
connection alone is insufficient: the app requires live, in-car physics.
Spectating a friend or watching a replay does not expose that driver's usable
fuel evidence through this player-car estimator.

The monitor normalizes incoming ticks and emits display snapshots at about
2 Hz. Fuel learning waits for the first observed start/finish crossing, then
requires **at least five additional valid complete laps by default**. The first
partial lap never counts. Fuel and time at each crossing are linearly
interpolated estimates, not full-rate driving-quality certification.

| Fuel status | Meaning |
|---|---|
| `WAIT_CAR` | A usable live in-car context is absent; no fuel advice is admitted. |
| `LEARNING` | Waiting for a clean crossing or enough valid complete laps. |
| `READY` | A fuel-only estimate can be displayed; not full race-engineer readiness. |
| `BLOCKED` | Evidence or continuity is unsuitable; previous estimates are withdrawn. |

Pit/in/out laps, refueling, incidents, off-track intervals, caution/other
unsuitable flags, missing essential fields and stale/rejected data do not count
toward learning. The monitor retains interval hazards so a clean final tick
cannot hide a short pit visit or bad interval. After an interrupted lap, the
estimator needs a fresh clean crossing before using retained same-session
history. Session/source/car-slot changes and leaving live driving clear that
history. The default history is bounded to the latest 50 valid laps.

Sparse dropped SDK ticks alone do not invalidate these low-rate fuel estimates;
missing display snapshots, time gaps over 1.5 seconds and inconsistent lap
progress do. This does not relax the separate high-rate driving/lap-quality
gates. A crossing whose lap counter and track-position wrap cannot be confirmed
in the same display interval is rejected conservatively and may delay learning.

The conservative burn is the larger of the observed mean and nearest-rank
90th percentile. Estimated range is the number of **whole laps** remaining
after preserving the configured reserve. It is not a guarantee of range under
changing pace, weather, caution periods or fuel-saving behavior.

### Finish and refill fields

These fields are deliberately often blank:

- Finish demand requires a current, frame-bound `SessionType=Race` and a usable
  remaining-lap or remaining-time horizon. A practice-session timer is not a
  race finish. Missing/sentinel horizons, including `SessionLapsRemainEx=32767`,
  must not be turned into an invented finish.
- Known remaining laps are treated as full laps conservatively. Timed horizons
  use the fastest admitted complete lap, round up and add one extra lap by
  default. This is a fuel projection, not a verified event-finish rule model.
- "Fuel needed to finish" includes the configured reserve. "Fuel to add"
  additionally requires a configured tank capacity and the **entire remaining
  demand fitting into one tank**; otherwise it stays blank. It is never a pit
  instruction, and it is withdrawn during pit/refuel or invalid intervals.
- The fuel-only stop-count estimate, when available, is not a multi-stop plan.
  No tire, traffic, service, penalty or mandatory-stop rule is solved here.

The UI and server remove old estimates on disconnect/staleness; reconnect starts
fresh learning. A `READY` label does not promote the M2/M3 strategy or driving
acceptance gates.

## Optional practice speech

Speech starts muted. Click **启用练习语音** only when you want experimental spoken
fuel facts. The browser must offer an English voice reporting
`localService === true`; otherwise speech remains unavailable. There is no
fallback to a cloud or non-English voice. The app does not send speech text to
an external speech service.

Speech is restricted to confirmed `Practice` sessions, fresh in-car fuel
estimates and a bounded low-workload window. Braking/steering, nearby-car or
quality hazards cancel the intent. Messages expire and are not queued for later
playback. **All `Race` sessions, including official races, remain muted**, as do
unknown session types, qualifying, spectator/replay contexts and synthetic demos.

When the browser page becomes hidden, it automatically mutes and cancels speech.
Returning to the page requires manual re-enabling. Therefore this prototype
**does not guarantee speech while iRacing is foreground or full-screen**. It is
not yet a reliable background race-audio interface. The **立即静音** button cancels
current speech immediately.

## Recording, privacy and stopping

The hidden launcher stores raw clips under
`%LOCALAPPDATA%\iRacingAIEngineer\captures` and launcher logs under the sibling
`logs` directory. These are private local files, outside the public checkout.
DriverInfo is filtered, but telemetry and remaining SessionInfo are still private:
do not publish them, upload them with bug reports, or commit them to Git.

The default app-level recording budget is **4 GiB per worker run**, shared across
reconnections. Recording stops near the limit with finalization headroom; it
does not delete older captures. This is not a total-disk retention limit. Use
`-NoRecording` or the direct CLI without `--record-directory` if recording is not
wanted. Recording inside the public checkout is rejected.

A recorder error disables further recording for that worker but does not, by
itself, terminate the fuel UI. Check the recording status instead of assuming a
capture exists. A normal finalized clip can have a `COMPLETE` receipt;
disconnects, errors and force-stops may leave an incomplete prefix that is not
admitted evidence. Even `COMPLETE` does not establish adequate capture quality
or product acceptance.

The **保存本次摘要** link downloads an aggregate JSON summary, not raw telemetry
or SessionInfo. It keeps `live_acceptance=false`; a successful UI run or a learned
fuel estimate cannot change that field into strategy/driving acceptance.

## Validation boundary

Offline tests exercise learning, invalidation, private recording, HTTP/freshness
guards and browser rendering/speech policies. Browser tests use controlled
synthetic states; they do not establish that a real local voice was heard in a
running game. No authentic driving, pit sequence, background audio, calibrated
fuel accuracy or full race-engineer acceptance is claimed by this milestone.
See [the public status](PUBLIC_PROJECT_STATUS.md) for the separately recorded
live evidence and remaining acceptance requirements.
