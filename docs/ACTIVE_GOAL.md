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
[the detector contract](PROXIMITY_SPOTTER.md). B-E and the final endurance goal
remain open. No proximity audio or newly deployed EXE is claimed by Stage A.
Data collection is user-started; no vehicle, simulator-launch or pit-box commands.
LLM use is opt-in, summary-only, for question interpretation and explanation.

## Next implementation slice: reliable delivery

- Give proximity delivery its own bounded worker and fresh detector snapshot;
  do not enqueue it behind PTT recognition, model requests or 2 Hz display work.
- Pre-render and validate a small phrase cache for the selected local voice.
  Cache failure or a changed voice/output selection must remove audio readiness.
- Add explicit output ownership and cancellation acknowledgment. The existing
  `AudioIO` owns a process-wide PortAudio lock and refreshes its device catalog;
  simply starting a second player would contend with recording and other speech.
  Urgent proximity must cancel obsolete output without playing overlapping voices
  or silently losing a held PTT request.
- Bind delivery to connection generation, detector epoch and event sequence;
  revalidate freshness before output and while playing. Expired events are dropped,
  not replayed once the long answer or device operation finishes.
- Separate noncritical analysis faults from acquisition. Decouple durable writes
  only with bounded buffering and explicit overflow/incomplete-capture accounting;
  never silently drop frames and still claim a complete recording.
- Exercise blocked recognition, long answers, held PTT, stale data, device loss,
  default-device changes and shutdown with fake backends before hardware checks.

For each major advance: update status, run Ruff and all pytest tests, scan public
safety including history, review exact staged files/diff, commit with the configured
noreply identity and push public `origin/main`. Never publish private evidence.
