# Standalone Crew Chief-derived reader prototype

## Scope

This reuses the **acquisition source**, not the running Crew Chief application.
Selected C# code from Crew Chief's `iRSDKSharp` is adapted into an independent,
read-only process. It supplies raw SDK observations to the existing Python
collector and replay pipeline. Crew Chief installation, UI, speech, spotter,
strategy and MQTT are not dependencies. See the pinned
[upstream and modification record](../native/crewchief_reader/UPSTREAM.md)
and [upstream MIT license](../native/crewchief_reader/LICENSE.CrewChief).

The default remains `pyirsdk`. Only `collect-live` gains the optional backend;
`monitor-live`, the protected supervisor and host deployments are not switched.
The Python package still depends on `pyirsdk`, including its safe SessionInfo
YAML parsing rules, but this backend does not use it to open the SDK mapping.

## Build and use

Requirements: Windows, the .NET Framework C# compiler, and the project's normal
Python environment. The build is local and downloads nothing. From the public
repository root in PowerShell:

```powershell
.\scripts\build_crewchief_reader.ps1
uv run python scripts/run_local_cli.py collect-live `
  ..\private-archives\telemetry\crewchief-practice.jsonl `
  --backend crewchief `
  --crewchief-reader .\build\crewchief-reader\CrewChiefReader.exe `
  --source-id local-crewchief-sdk `
  --session-id practice-session `
  --expected-source-kind live `
  --duration-seconds 60
```

The simulator must already be running in the same logged-in Windows desktop
session. The program never launches it. Use a **new private output path** for
each run; an existing output is never overwritten. Build output is ignored by
Git. Do not invoke the native reader to print telemetry into shared consoles
or public logs: its pipe payload is raw, private data.

The build places `LICENSE.CrewChief` and `UPSTREAM.md` next to the executable.
Keep those notices with any redistributed copy; do not distribute only the EXE.

No credentials, broker or network connection is needed for acquisition. The
only child process started by the adapter is the local reader executable the
operator explicitly selects. Use the executable built from this repository;
the adapter is not a sandbox or a signature verifier for arbitrary programs.

## Boundaries

- The production entry point only connects to the fixed local iRacing SDK
  mapping. There is no alternate-map, fixture, replay-file or control mode.
- Mapping and view permissions are explicitly read-only. The extraction does
  not contain the upstream SDK's broadcast, pit, chat or replay-control APIs.
- The reader validates memory ranges and descriptors, then copies a stable
  frame and matching SessionInfo. Torn snapshots are retried a bounded number
  of times; persistent inconsistencies are rejected.
- The pipe protocol is `crewchief-readonly-v1`: one `snapshot` request and one
  bounded JSON response. The Python adapter validates types, schema, array
  cardinality, errors and SessionInfo binding before constructing `RawSdkFrame`.
- Missing or invalid fields are not replaced with zero. A schema change or
  disconnection aborts collection; it cannot silently splice a new connection
  into the same run. Restart collection with a new output after reconnecting.
- Startup can wait for a connection. Pipe timeouts, EOF and shutdown close the
  owned child. EOF does not trigger a simulator connection or any control action.
  `--wait-seconds 0` still allows one bounded reader request (up to two seconds)
  so normal .NET process startup is not mistaken for a disconnected pipe.
- Source kind is determined from simulator metadata, not the selected backend.
  Replay remains `REPLAY_SDK_PROXY`; unknown mode fails closed. A backend name
  is not evidence of an authentic on-track session.
- Existing collector privacy filtering removes `DriverInfo` before persistence.
  Raw telemetry and other session metadata still belong on the private side.

The bridge does not unlock data iRacing does not expose. In particular, it
does not establish continuous physical tire wear, trustworthy curb geometry,
or supported coaching simply because a variable exists.

## Validation and acceptance

Run the normal project gates and the native regression tests:

```powershell
uv run pytest -q tests/test_crewchief_reader_native.py tests/test_crewchief_transport.py
uv run pytest -q
uv run ruff check .
uv run python scripts/check_public_safety.py --include-history
git diff --check
```

Native tests compile a separate synthetic byte-array harness on Windows.
They do not create a fake simulator mapping, connect to iRacing, or feed fixture
data through the production CLI. On systems without the Windows compiler those
native execution tests are explicitly skipped, not counted as verified.

Recorded Windows validation for this milestone (2026-09-21):

- Full suite: 1,083 passed, 45 skipped. Two skipped wheel-build checks were
  separately rerun with the existing `uv` executable on PATH; both passed.
  The remaining 43 skips require absent data, another platform or private
  deployment material. This is not a claim that skipped checks passed.
- Focused reader/transport/SDK/CLI suite: 130 passed. Native execution includes
  24 synthetic test groups and a real C# serializer-to-Python-collector check.
- Ruff, public-safety scan including history, and Git whitespace checks passed.
- The real local connection probe returned `SDK_UNAVAILABLE`, created no raw
  capture and left no reader child running. No simulator was launched.

### First real capture and performance follow-up

A subsequent 60-second real SDK capture (September 21) completed with 335
fields and 1,321 persisted frames: 21.99 Hz, 36.64% tick coverage, 2,284
accounted missing ticks and a largest gap of 29 ticks (0.483 seconds).
There were no field read errors, conflicting duplicates, stale events, schema
changes or session resets. The strict replay reader admitted the structure,
but quality remained **DEGRADED**. The car was stationary in its pit stall:
there were no driven laps or pit transitions. Raw observations remain private.
This proves a real connection, not adequate acquisition performance or product
acceptance. A read-only microprobe of another backend without the same durable
writer is not a fair performance comparison.

The September 22 optimization keeps protocol v1 and the default backend. It
reuses unchanged, validated schema metadata and SessionInfo parsing, and avoids
repeating canonical JSON encoding at the built-in collector writer boundary.
Cache keys preserve exact content and primitive types; a reused SessionInfo
update counter alone cannot establish unchanged content. Per-frame snapshot,
value, freshness, mode and schema checks remain in place. The collector still
filters private driver metadata and flushes/fsyncs every record; no batching,
asynchronous loss window, field reduction or weaker evidence threshold is added.

The reproducible Python benchmark uses **invented** data only, 335 fields
(including 20 arrays), synthetic session metadata, 300 observations and a
three-tick increment to exercise both frame and tick-drop writes:

```powershell
uv run python scripts/benchmark_crewchief_pipeline.py --iterations 300 --tick-step 3
```

It uses the real durable JSONL writer but an in-memory protocol exchange. It
does **not** measure native shared-memory access, C# encoding or pipe latency.
It reports timings, complete file hashes and semantic-record hashes in receipts; timing
values are not CI pass/fail thresholds. Golden-file regression checks bind the
optimized pipeline to the pre-optimization output bytes and receipt.

Local September 22 comparison against the Python modules from `95e0542` used
three fresh processes per version, alternated in both orders, with the same
benchmark runner. The table reports the median of the three per-run medians
and the median of the three per-run p95 values, in milliseconds:

| Measured stage | Before median / p95 | After median / p95 |
|---|---:|---:|
| Python bridge including metadata copy | 3.925 / 6.278 | 1.207 / 1.402 |
| Collector ingest including per-record fsync | 9.182 / 12.770 | 5.221 / 5.673 |
| Combined Python path | 13.193 / 19.083 | 6.460 / 7.073 |

The combined median fell about 51% for this synthetic workload. All six runs
produced the same 6,212,586-byte file and semantic receipt. The invented
three-tick step deliberately retains all 598 missing ticks in the receipt;
optimization does not hide data loss. A separate offline replay of all 1,321
previously captured frames also produced identical record bytes and receipts
before and after the collector change. This was replay of existing private
evidence, not a new live capture.

Optimization regression gates: **1,154 passed, 43 skipped**, with the existing
`uv` executable available to the full suite. Skips require unavailable data,
another platform or private deployment artifacts. The native suite now executes
64 synthetic test groups, including warm-cache torn-snapshot checks and exact
serialized-byte equivalence. Ruff and the history-inclusive public-safety scan
also passed.

### Real spectator-session comparison (September 22)

The user subsequently entered a real online spectator session. Both backends
were measured with all 335 fields, 60-second capture windows, the same 10 ms
minimum read-start interval, private outputs and per-record fsync. The planned
Crew Chief / pyirsdk / pyirsdk / Crew Chief sequence was interrupted by a
SessionInfo consistency error in the third clip; a new pyirsdk file was recorded
after the final Crew Chief clip. No partial file was resumed or repaired.

| Clip / backend | Frames | Observed Hz | Tick coverage | Largest tick gap | Strict replay admission |
|---|---:|---:|---:|---:|---|
| 1 / Crew Chief | 2,406 | 40.07 | 66.74% | 0.200 s | Pass, DEGRADED |
| 2 / pyirsdk | 3,426 | 57.10 | 95.17% | 0.267 s | Pass, with quality rejections |
| 4 / Crew Chief | 2,443 | 40.69 | 67.79% | 0.167 s | Pass, DEGRADED |
| 5 / pyirsdk retry | 3,443 | 57.40 | 95.67% | 0.333 s | Rejected: player car-class identity changed |

Hz uses interframe intervals; coverage counts observed ticks against observed
plus missing ticks. Clip 5's numbers are independently checked raw-record
diagnostics, **not admitted analysis**; its receipt hash matches but identity
validation still fails. Its `PlayerCarClass` scalar changes to zero while
camera/player indices stay fixed and their car-class array slots remain
nonzero. The existing adapter treats zero as present and rejects the identity
change; neither that adapter nor this policy was changed by the optimization.
The observation alone does not establish an official SDK sentinel contract.

The first Crew Chief clip overlapped the tail of the regression run; its
remaining-window coverage and the later repeat were similarly low. Session
conditions can change across sequential clips, so these are observations, not
a claim of controlled driving-performance equivalence. The admitted clips had
identical complete descriptor maps, no field read errors, conflicting
duplicates, schema changes or session resets, and no persisted DriverInfo.
Missing ticks remain quality failures, not a normal exemption for spectators.

Separately, 120 distinct pairs with the **same SDK tick and SessionInfo update
counter** matched all 335 values and error statuses: 40,200 field comparisons,
zero differences. This establishes sampled decoder parity, not sustained rate
or end-to-end engineer acceptance. Read-only latency probes excluded the durable
writer; their throughput must not be substituted for the table above.

Additional boundaries exercised:

- The aborted third clip contained 730 frames but no completion receipt. Strict
  admission rejected it; explicit incomplete-prefix recovery was diagnostic only.
- The fifth clip had a completion receipt but was rejected by the stricter
  player-identity check. Writer completion alone does not establish usable input.
- A capability probe blocked driving/fuel/strategy in spectator context. The
  first monitor run hit the same SessionInfo race; a fresh five-second retry
  completed with 300 frames, 11 `WAIT_CAR` snapshots, zero in-car snapshots and
  no executable output. Its event stream still recorded three quality rejections.
- Camera-car position arrays changed while player speed, fuel and throttle
  remained zero. Camera-car data must not be substituted for player telemetry.

All 15 normalized rejections in clip 2 had equal adjacent capture timestamps
while SDK ticks and SessionTime advanced normally. The local Python 3.12 runtime
uses `GetTickCount64()` for `time.monotonic()` (15.625 ms resolution); normalization
treats a nonpositive capture-time delta as regression/staleness. This is a
confirmed false rejection from coarse clock sampling, not JSON microsecond
rounding or actual source freezing. The monitor's three warnings were not
individually retained/diagnosed and are not independently proven to share it.

At that milestone, remaining work was to reduce Crew Chief full-pipeline latency,
make normal SessionInfo update races recoverable within a bounded retry without accepting
torn snapshots, unify capture and freshness logic on a high-resolution clock
domain, and distinguish spectator identity changes from corrupted in-car
identity. That comparison did not fix or waive these issues; the clock and
SessionInfo retry follow-up is described below.
The default remains pyirsdk. No simulator, camera or vehicle commands were sent;
all raw captures, diagnostics and incomplete files remain private.

### High-resolution clock and bounded metadata retry follow-up

Live captures from both transports, monitor observations, probe sampling and
collector/preflight deadlines now share `runtime_clock.monotonic_now()`, backed
by `time.perf_counter()`. On the tested Windows runtime this is monotonic QPC,
not the coarse GetTickCount64 clock. No synthetic timestamp increments are
inserted: actual nonpositive capture deltas and frozen source ticks still fail
the existing freshness checks. Injected offline clocks remain supported.
The clock origin is unspecified; do not combine timestamps across processes,
boots, old recordings or transports using different clock implementations.
Previously recorded evidence is not rewritten or retroactively admitted.

The pyirsdk transport retries the entire frozen read only when the SessionInfo
update counter increases during that attempt. It releases each frozen buffer
and discards all values/read errors from the unsuccessful attempt. There are
at most three attempts within a 100 ms retry budget; no new retry starts at or
after the deadline, and an overdue retry result is rejected. This budget does
not preempt a synchronous SDK wait or cap an otherwise successful first read.
Invalid/regressing counters, changed schemas and unstable-buffer failures are
still terminal. Persistent metadata churn remains a consistency error, not a
partially accepted frame or a completed capture.

A subsequent real spectator retest completed a fresh 60-second pyirsdk file
with 3,460 frames across 59.988459 seconds (57.66 Hz, 96.11% tick coverage),
335 fields and four SessionInfo records. Strict replay admission passed.
There were zero equal/decreasing capture timestamps and zero
`CAPTURE_TIME_REGRESSION` rejections. Of 3,460 normalized samples, 3,459 were
DEGRADED and one was REJECTED: that sample followed an actual 751,284 microsecond
capture gap (45 SDK ticks). The real-stale guard was preserved, not waived.
There were 140 accounted missing ticks and no read errors, schema changes or
session resets. No retry counter was instrumented; successful metadata updates
do not by themselves prove that a real retry race occurred. Synthetic tests
exercise transient recovery, persistent churn and both budget boundaries.

The planned Crew Chief repeat ended with `SDK_UNAVAILABLE` after an 834-frame,
21.793126-second prefix. The simulator process was subsequently absent; no
restart was attempted. The prefix has no completion receipt and strict admission
rejects it. Its persisted frame timestamps had no equal/decreasing pairs, but that diagnostic
does not admit the incomplete file or establish sustained throughput. A later
15-second monitor attempt failed at connection and produced no terminal receipt.
Thus post-change live-monitor verification and a complete second-backend repeat
remain pending. All observed frames were out of car; no driving, fuel, pit or
strategy acceptance is claimed. Full-pipeline Crew Chief latency and spectator
player-class identity semantics are still open issues.

Follow-up regression: **1,196 passed, 43 skipped**, with lint, whitespace and
history-inclusive public-safety checks passing. This adds 42 offline cases
covering shared clock behavior and bounded retry boundaries; unchanged skips
require absent datasets, private deployment artifacts or another platform.

This is an optional acquisition prototype, not a replacement proven superior
to the default reader. Sustained acquisition quality and recovery behavior
must be established before any default-backend change.
Human-driven laps and a pit sequence remain necessary for end-to-end strategy
and driving acceptance; synthetic tests and out-of-car canaries do not qualify.
