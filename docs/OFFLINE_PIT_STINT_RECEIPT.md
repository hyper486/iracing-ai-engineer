# Offline pit, service, and stint receipt

The package-external `offline-m1-pit-stint-v1` builder now turns the pinned
Audi R8 LMS EVO II GT3 × Spa IBT into a deterministic, descriptive M1 receipt.
It is **not** a race-strategy recommendation: the artifact is advisor-only,
`SHADOW_ONLY`, `CANDIDATE_NOT_GOLDEN`, and `NOT_R7_ATTESTED`, and its
`recommendations` array is empty.

Open the current local artifact:
[`audi-spa-offline-pit-stint-v1.json`](../data/derived/audi-spa-offline-pit-stint-v1.json).

The builder accepts only the opaque, same-open-handle `ValidatedIbtRun` from
`open_ibt_telemetry`. Before publishing anything, it must close three
independent trust roots:

- raw IBT SHA-256
  `754d14e6e2870eb00d42368e36c7a15495cb68bfd5f829ab08665d0f95fc7f36`;
- normalized 151,892-sample SHA-256
  `f38d336a4d647886c0a4b8fce32fc4d53ba688b26b7c46d70e780e85c012b07e`;
  and
- shared `telemetry-events-v1` receipt SHA-256
  `6abc981e717fe40311147979db6a5ae0b9d6a44658fabf5ec798af1ffc692800`.

It neither changes the frozen event contract nor joins the Windows/macOS r7
trust chain. The full upstream event receipt and normalized receipt remain in
the output so every derived interval can be traced back to the admitted stream.

## Audi/Spa result

The real recording produces `quality_gate=PASS` with one complete pit-road
cycle and one complete `PitstopActive` service episode:

| Observation | Frozen result |
|---|---|
| Pit-road interval | enter frame 105,895 at 1787.616667 s; exit frame 107,935 at 1821.616667 s; duration 34.000000 s |
| Service-active interval | start frame 106,962 at 1805.400000 s; false edge frame 107,522 at 1814.733333 s |
| Service support | 560 true frames; 9.333333 s |
| Stall interval | enter frame 106,969 at 1805.516667 s; exit frame 107,575 at 1815.616667 s; duration 10.100000 s |
| Positive service/stall overlap | 9.216667 s; service starts 0.116667 s before the stall signal and ends 0.883333 s before its exit |
| Observed tank-level endpoints | 4.154789 L → 25.149794 L; net `+20.995004 L` |
| Stint 1 endpoints | partial start; laps-completed delta 11; tank level 52.000000 L → 4.455528 L |
| Stint 2 endpoints | partial end; laps-completed delta 5; tank level 24.960405 L → 5.137365 L |
| Stint completeness | two partial observations (`FILE_START → ROAD_ENTER` and `ROAD_EXIT → FILE_END`); zero complete stints |

The `+20.995004 L` value is named
`observed_net_tank_change`, not delivered fuel. Endpoint tank level cannot
separate fuel flow from simultaneous consumption, telemetry quantization, or
other service behavior. `PitstopActive` also does not identify tire work,
repairs, or a driver swap. Every service-content field therefore stays
`UNAVAILABLE`, `UNKNOWN`, and `SKIP_NOT_OBSERVABLE`. Tire-set and compound
counters are deliberately not exported as evidence of a tire change.

The capability gates are correspondingly explicit:

- pit/service detection: `PASS_DATA`;
- complete-stint analysis: `WAIT_COMPLETE_STINT`;
- service contents: `SKIP_NOT_OBSERVABLE`; and
- human validation: `WAIT_HUMAN_LABELS`.

## Fail-closed interval rules

A service episode needs adjacent `false → true`, a contiguous true run, and an
adjacent `true → false` edge. It is published only after the containing pit-road
interval also receives complete enter and exit edges. A stall interval supports
the service only when their time intervals have positive overlap; it is not
required to contain the service exactly.

Any rejected, stale, dropped, reset, gap, identity change, or missing required
sample clears all open state. An active signal at the beginning or end of the
file remains partial and cannot become a complete episode. Off-road stints are
complete only when bounded by a prior pit-road exit and a later pit-road enter.
These rules prevent a transition on opposite sides of an evidence gap from
being joined into a fabricated interval.

## Reproduce

After fetching the pinned local-only IBT, run:

```bash
uv run python scripts/build_offline_pit_stint_receipt.py \
  "data/raw/audir8lmsevo2gt3_spa up.ibt" \
  --source-id public-audi-r8-evo2-spa \
  --session-id public-fixture-2023-12-race \
  --expected-source-sha256 \
  754d14e6e2870eb00d42368e36c7a15495cb68bfd5f829ab08665d0f95fc7f36 \
  --expected-normalized-samples-sha256 \
  f38d336a4d647886c0a4b8fce32fc4d53ba688b26b7c46d70e780e85c012b07e \
  --expected-event-receipt-sha256 \
  6abc981e717fe40311147979db6a5ae0b9d6a44658fabf5ec798af1ffc692800 \
  --output data/derived/audi-spa-offline-pit-stint-v1.json
```

The destination is created exclusively and is never overwritten. The canonical
receipt is also written to stdout. A valid `PASS` exits 0, a structurally valid
but degraded receipt exits 5, and input/trust-root/output failure exits 3.

## Current receipt and tests

| Item | Current evidence |
|---|---|
| Builder SHA-256 | `dd2fd457b98c46c06e9da8d9ef1b8db70a96e2c0a058d82d3d112ef07d192841` |
| Test SHA-256 | `e9710eb53f9acbee6b66903dcf38426e3b2e45f7b31adf3e3c93c9fc0f9aa91e` |
| Canonical self-bound receipt SHA-256 | `76a7cec5cf255cd1d7f8fb9e46847b3cae515c8ad3c14acccfffdb0280b906d9` |
| Serialized artifact | 7,462 bytes; SHA-256 `9082df792b8b06682a5c0caf8f8ca6b8cbc59e81c0954c4a6fee2eae0d6f0fb0` |
| Focused tests | 20 passed, including the real 151,892-frame Audi/Spa integration |
| Determinism | The real Audi/Spa receipt is byte-identical under `PYTHONHASHSEED=1` and `987654` |

The focused tests also cover complete and partial edge handling, service outside
pit road, missing channels, dropped ticks, session reset, positive/no stall
overlap, no-pit `WAIT_PIT_SAMPLE`, missing-fuel null endpoint semantics,
independent source/normalized/event trust-root mismatches, strict
validated-run admission, exclusive output, self-hash closure, empty
recommendations, and non-inference from tire counters.

## Remaining acceptance boundary

The validated-run builder now lives in the installable
`iracing_ai_engineer.pit_stint` module and accepts either admitted IBT or
collector input. The documented script is a thin compatibility wrapper. An
independent wheel review confirmed the module and its RECORD entry, while the
Audi/Spa output remained byte-identical. Collector duplicate conflicts, read
errors, and declared-rate/SessionTime disagreement fail closed before an
episode is published.

This closes a finite offline evidence slice of M1. It does not supply a human-
labeled service regression set, a complete real stint, authoritative target-
series event rules, live SDK proof, delivered-fuel truth, tire/service contents,
traffic/rejoin strategy, or an r7 attestation. Those remain separate `WAIT`,
`SKIP`, or live-data gates in the goal audit.
