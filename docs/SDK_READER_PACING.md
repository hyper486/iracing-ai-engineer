# Native SDK reader pacing

The native reader keeps its existing **10 ms minimum polling budget** but uses
Python 3.12 `time.sleep` for the unused part, instead of a short
`threading.Event.wait` timeout. This removes one measured source of timer
overshoot. It does **not** establish 60 Hz live acquisition or explain all
historical dropped ticks.

## Runtime boundary

- Reading, metadata binding, proximity delivery and bounded worker submission
  consume the budget first. If they take at least 10 ms, no extra sleep is added.
- A connected short pause requests at most 10 ms. Stop is checked before it and
  on the next loop iteration. Stop during sleep cannot interrupt that pause.
  The request cap is not a hard wall-clock shutdown deadline.
- No busy-spin, process-priority change or system timer setting is introduced.
- The real SDK data-event wait, frozen buffer validation, schema and metadata
  guards are unchanged. No assumption about the SDK event's reset mode is used.
- Disconnected five-second retries still use the interruptible stop Event.
  Worker teardown and SDK release keep their existing ownership contracts.
- Whole-lap **99.9% tick coverage / 0.1 s maximum gap** gates are unchanged.
  Scheduling, GIL contention, disk workers, analysis and VR load can still cause
  insufficient acquisition quality.

Python documents a high-resolution Windows timer for `time.sleep` on supported
Windows versions, but explicitly allows actual suspension to exceed the request
because of scheduling. Timer resolution is not end-to-end latency. See the
[Python 3.12 documentation](https://docs.python.org/3.12/library/time.html#time.sleep).

## Reproducible, offline-only diagnostic

```powershell
uv run python scripts/benchmark_sdk_reader.py --iterations 2000 --wait-samples 120
```

The fixture creates **owned anonymous memory**, invented field names and invented
values. It uses the pinned pyirsdk headers/getters and the existing strict frozen
reader, but never constructs/starts a live transport, opens a named SDK mapping
or event, contacts a provider, or accesses audio hardware. Only aggregate results
and an invented-value digest are printed; no capture or host receipt is written.

The CPU path has 335 fields, including 20 arrays of 64 elements. It reports frozen
decode, metadata binding and queue-accounting costs separately. Every iteration
must preserve the invented-value digest. The optional timer comparison alternates
the first-tested implementation and takes at most 300 samples of each; omit
`--wait-samples` for no timer experiment. CI checks contracts, not timing thresholds.

One local idle run measured the following, with 2,000 CPU iterations and 120 timer
samples per implementation:

| Measurement | Median | p95 |
|---|---:|---:|
| Frozen field read | 0.250 ms | 0.296 ms |
| Bound metadata | 0.003 ms | 0.008 ms |
| Queue accounting | 0.273 ms | 0.300 ms |
| Old 10 ms Event timeout | 15.073 ms | 16.517 ms |
| New 10 ms reader pause | 10.095 ms | 10.193 ms |

These are separate idle measurements, not rates from running iRacing. The original
field decoder remains unchanged because these results did not justify a second
decoder implementation. They do not measure the real SDK event, recording fsync,
concurrent worker contention, microphone/output latency or VR performance.

The CPU/queue benchmark excludes char fields by default to preserve the original
timing fixture; `--include-chars` now opts into all six raw SDK types. The pacing
fix itself did not change recording encoding. A subsequent schema-bound
[char-recording correction](SDK_CHAR_RECORDING.md) admits valid raw char bytes
without relaxing the generic JSON bytes guard. The CPU diagnostic still does not
measure recording durability or prove actual game sampling rate.

## Verification and remaining acceptance

Deterministic tests cover spent/partial budgets, stopped state, stop during pause,
the request cap and real reader shutdown. An invented immediate-read 60 Hz source
with fixed work cost demonstrates the difference between a precise virtual pause
and a simulated coarse timeout. That fixture models no real SDK event and cannot
certify game sampling rate. Existing SDK consistency, recorder isolation and
disconnect tests remain authoritative for their respective contracts.

A user-driven in-car recording under VR load must still show per-lap coverage and
gap quality, authentic proximity calls, current fuel accuracy and supported corner
evidence. No benchmark, synthetic trace, replay or frozen self-test closes those
acceptance requirements.
