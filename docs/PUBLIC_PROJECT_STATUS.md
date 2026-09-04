# Public project status

Snapshot: 2026-09-03 EDT

## Product objective

Deliver an advisor-only iRacing AI engineer for solo endurance racing that can
reason about fuel, tires, stints, nearby traffic, pit timing and rejoin cost,
and can produce evidence-backed corner coaching and a post-session report.

## Current public milestone

| Boundary | Status | Meaning |
|---|---|---|
| Replayable telemetry foundation | Implemented | Defensive IBT/SDK adapters, normalization and deterministic replay exist. |
| Fuel, stint and pit reasoning | Implemented with evidence gates | Missing event rules or calibration produce `WAIT`, not invented values. |
| Rejoin/traffic reasoning | Implemented with evidence gates | Estimates are time-domain and do not claim opponent fuel. |
| Tire reasoning | Implemented as a performance belief | The project does not claim direct physical tire wear without a supported source. |
| Corner diagnosis | Implemented for repeated comparable evidence | Curb/risk claims remain blocked without trusted labels. |
| Deterministic reports | Implemented | JSON and script-free HTML outputs preserve provenance and limitations. |
| Privacy-safe live state bridge | Implemented; live field check pending | Tick-level normalization feeds bounded JSONL snapshots for future overlay/speech consumers without raw telemetry or control. |
| Advisor-only safety | Required and implemented | No vehicle, simulator-launch or pit-box control path is accepted. |
| Authentic local `SDK_LIVE` acquisition | Proven before acceptance | The running simulator's real shared-memory transport has produced a complete, sealed canary capture. |
| Authentic local `SDK_LIVE` acceptance | Pending on-track evidence | The canary was out of car, so strategy and driving capabilities correctly remained blocked. |
| Final strategy plus driving report | Pending live evidence | Both advice gates must pass on an admitted real capture. |

## Current live boundary

Easy Anti-Cheat and the simulator now start normally. A privacy-safe summary of
the first real shared-memory canary is:

- `SourceKind=SDK_LIVE`, full simulator mode and a 60 Hz SDK tick rate;
- 294 persisted frames across a five-second default-cadence capture;
- six accounted dropped ticks, with no conflicting duplicates, stale events,
  schema changes or session resets;
- explicit `OUT_OF_CAR_OR_REPLAY_VIEW` context, so no race-strategy or driving
  readiness claim was admitted.

The collector cadence was also corrected so `poll_seconds` is a minimum
read-start interval rather than extra sleep added after serialization. The
default 10 ms setting now tracks the native 60 Hz source during the same
full-field, durable-write path without requiring a 1 ms busy-poll setting.

A separate `monitor-live` command now normalizes every distinct tick while
emitting only a bounded, privacy-safe state snapshot at a default 2 Hz. It is a
state bridge rather than a recommendation engine: `READY` means the bridge is
usable, not that strategy or driving evidence has passed. Its deterministic,
privacy, stale, cadence and CLI behavior are covered offline; a field check of
the new command remains pending the next simulator session.

The next blocker is human-driven evidence: configure the physical driving
inputs, enter the car, then record a sufficiently long clean run and pit
sequence. Host-specific telemetry, logs and device details remain private.

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
