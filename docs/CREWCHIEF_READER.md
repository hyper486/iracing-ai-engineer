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

This is an optional acquisition prototype, not a replacement proven superior
to the default reader. Real-session field parity, acquisition rate, drop rate
and recovery behavior must be measured before any default-backend change.
Human-driven laps and a pit sequence remain necessary for end-to-end strategy
and driving acceptance; synthetic tests and out-of-car canaries do not qualify.
