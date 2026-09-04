# Privacy-safe live monitor

`monitor-live` is the first real-time product bridge between the read-only
iRacing SDK transport and a future overlay or speech process. It is deliberately
not a strategy engine: it reports admitted direct state, quality and transition
events without turning incomplete evidence into pit or driving advice.

## Run

With the iRacing simulator already running in the same logged-in Windows
session:

```bash
uv run python scripts/run_local_cli.py monitor-live \
  --source-id local-monitor \
  --session-id practice-session \
  --expected-source-kind live \
  --duration-seconds 30 \
  --require-in-car
```

The command writes JSONL to standard output. `--require-in-car` is optional; it
returns exit code 5 after writing the terminal receipt when the stream never
observes the human driver in the car. SDK absence returns exit code 2, while an
invalid or inconsistent stream returns exit code 3.

## Runtime model

- Every distinct SDK tick is normalized and sent through the shared streaming
  event state machine.
- Same-tick reads are counted but not reprocessed.
- The default SDK poll interval is 10 ms, measured from one read start to the
  next; serialization or normalization time is not added as extra sleep.
- A bounded display snapshot is emitted every 0.5 seconds by default, plus a
  final snapshot when the last observed tick has not yet been projected.
- The transport, normalization and event pipeline remain read-only. The command
  has no simulator-launch, steering, pedal, shift, pit-box, network, audio or
  file-persistence path.

This separation preserves tick-level quality evidence while keeping a future
UI or speech consumer at a low and predictable update rate.

## Snapshot contract

Each `live_monitor_snapshot` is self-hashed and contains only:

- source kind and a one-way binding hash rather than raw source/session IDs;
- `WAIT_CAR`, `READY`, `DEGRADED` or `BLOCKED` state;
- simulator/player context evidence and machine-readable reason codes;
- normalized fuel, lap, speed, input, pit, flag, tire and environment scalars;
- opponent-array availability and slot count, not driver names;
- quality state, stale/drop evidence and transition-event summaries;
- `advisor_only=true` and `executable=false`.

The terminal `live_monitor_receipt` binds the snapshot stream and the shared
event-pipeline receipt, including frame, duplicate, dropped-tick, in-car and
status counts. Raw SDK frames, SessionInfo driver identity, setup names and
telemetry arrays are not emitted.

## Status semantics

| Status | Meaning |
|---|---|
| `WAIT_CAR` | The source is coherent, but the human driver is not in live car physics. |
| `READY` | In-car context and normalized sample quality are ready for a downstream consumer. |
| `DEGRADED` | In-car context exists, but non-fatal quality/read evidence requires caution. |
| `BLOCKED` | Stale/rejected quality, core read errors, unknown mode or conflicting context fails closed. |

`READY` means only that the live state bridge is usable. It does not mean fuel,
pit, traffic, tire or driving advice has passed its own evidence gates. Tactical
recommendations remain the responsibility of the deterministic strategy layer
and shadow speech policy.

## Validation boundary

The state machine, cadence, privacy projection, deterministic hashes, duplicate
handling, replay/full source checks, stale blocking and CLI exits have automated
coverage. The shared-memory collector has already completed a real
`SourceKind=SDK_LIVE` canary, but this new monitor command still requires a
separate live field check when the simulator is next available. An out-of-car
monitor result will not be represented as in-car acceptance.
