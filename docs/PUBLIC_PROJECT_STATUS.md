# Public project status

Snapshot: 2026-09-22 EDT

## Product objective

Deliver an advisor-only iRacing AI engineer for solo endurance racing that can
reason about fuel, tires, stints, nearby traffic, pit timing and rejoin cost,
and can produce evidence-backed corner coaching and a post-session report.

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
| Privacy-safe live state bridge | Implemented; live field check pending | Tick-level normalization feeds bounded JSONL snapshots for future overlay/speech consumers without raw telemetry or control. |
| Advisor-only safety | Required and implemented | No vehicle, simulator-launch or pit-box control path is accepted. |
| Authentic local `SDK_LIVE` acquisition | Proven before acceptance | The running simulator's real shared-memory transport has produced a complete, sealed canary capture. |
| Authentic local `SDK_LIVE` acceptance | Pending on-track evidence | Out-of-car, stationary pit-stall and spectator captures do not support strategy or driving acceptance. |
| Final strategy plus driving report | Pending live evidence | Both advice gates must pass on an admitted real capture. |

## Review corrections

The September 22 follow-up reduces repeated schema validation, unchanged
SessionInfo parsing and collector JSON encoding, while preserving per-frame
checks, privacy filtering and per-record durable writes. Offline tests check
cache invalidation and byte-exact receipts; synthetic timings do not establish
live throughput. Full regression: 1,154 passed, 43 skipped. A later real
spectator comparison confirmed sampled field parity but insufficient sustained
coverage, SessionInfo-race interruptions and a spectator player-class identity
rejection. Fifteen normalized false-stale rejections were also traced to the
Windows runtime's coarse monotonic clock; high-resolution clock unification
remains open. Neither the default backend nor deployment is changed.

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
driving-diagnosis promotion remains WAIT. This does not enable audio or vehicle
control, promote unsupported driving advice, or make the product live-accepted.

## Last recorded live boundary

The latest tests used a real online spectator session, with four complete
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
privacy, stale, cadence and CLI behavior are covered offline; a field check of
the new command remains pending the next simulator session.

The next live-validation prerequisite is human-driven evidence: configure the
physical driving inputs, enter the car, then record a sufficiently long clean
run and pit sequence. Host-specific telemetry, logs and device details remain
private. Software work also remains: real-time tactical delivery, an audible
engineering interface, broader multi-stop planning, and calibrated curb/trail
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
