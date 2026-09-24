# iRacing AI Engineer

A local-first, replayable and explainable advisor for solo iRacing endurance
racing.

The goal is to behave like a careful human race engineer: combine fuel, tire
and stint state with nearby-car context to reason about pit timing and rejoin
tradeoffs, then use repeated corner evidence to explain where lap time is being
lost and whether braking, throttle, curb use or trail braking is the supported
practice opportunity.

> **Experimental and advisor-only.** This project never sends steering,
> throttle, brake, clutch, shift, simulator-launch or pit-black-box commands. It
> is not affiliated with or endorsed by iRacing.com Motorsport Simulations.

## What is implemented

- Defensive `.ibt` and live-SDK telemetry adapters with explicit provenance.
- An optional standalone Crew Chief-derived, read-only Windows acquisition backend.
- Append-only collection, normalization, lap/stint segmentation and replay.
- Fuel-to-end, stint, pit-service and time-domain rejoin reasoning.
- Conservative tire-performance beliefs that do not invent physical wear.
- Distance-aligned corner evidence and repeated-loss diagnosis.
- Advisor timelines, shadow speech policy and deterministic session reports.
- A privacy-safe JSONL live-state bridge for future overlays and speech consumers.
- A native Windows desktop EXE with real-time fuel status, constrained DeepSeek
  questions, VR push-to-talk and selectable audio devices; no browser or WebView needed.
- Tick-level proximity detection plus opt-in cached Chinese proximity audio,
  independent of fuel/STT/LLM work, with priority cancellation and readiness diagnostics.
- Bounded analysis/recording workers, explicit incomplete-capture failures and
  streaming live-event digests; slow sinks are isolated from the SDK reader.
- Incremental recent-lap coaching in source, with bounded asynchronous analysis,
  comparable-condition filters and local PTT answers for repeated corner patterns.
- Private bounded proximity/audio journals and silent native replay in source,
  separating detected events, software playback and unconfirmed human hearing.
- Conditional live fuel-stop comparisons in source: session-scoped capacity/rate/
  transit assumptions, next-fill and stint budgets, and local PTT; not optimal pit timing.
- Conditional next-stint tire time comparison: pinned historical model, explicit
  driver-confirmed counter origin, full age/fuel bounds and extra service time;
  local questions, not physical wear or a keep/change safety recommendation.
- Same-action mapped pit briefing: dose, both service traffic projections and
  supported complete-lap tire gain together, with partial-lap coverage explicitly
  unknown; no full-stint ranking or pit command.
- An experimental local fuel dashboard with private recording and opt-in local
  practice speech; race speech stays disabled.
- Fail-closed source, confidence, privacy and advisor-only safety boundaries.

An authentic `SourceKind=SDK_LIVE` transport canary now reaches the running
simulator's shared memory and completes a sealed collection. End-to-end
acceptance is still pending a human-driven, on-track run with enough laps and
pit evidence for strategy and driving advice. See
[the public project status](docs/PUBLIC_PROJECT_STATUS.md).

The [active execution goal](docs/ACTIVE_GOAL.md) prioritizes local proximity
audio, current fuel answers and supported corner coaching on the AEIS framework.
[Detection and native priority audio](docs/PROXIMITY_SPOTTER.md) are implemented
in source and checked with synthetic backends/local synthesis. Existing EXEs are
not automatically upgraded; hardware hearing and in-car/VR acceptance remain open.
The [integrated native trial build](docs/NATIVE_TRIAL_BUILD.md) now packages these
slices with a frozen numerical/voice self-test and a reproducible accelerated
resource diagnostic. It is not a full-duration hardware soak or race acceptance.
The [recent-lap coaching contract](docs/LIVE_DRIVING_COACHING.md) explains the
new **哪里可以改进？** question and why an observed loss is not a promised gain.
The [private trial replay guide](docs/PRIVATE_TRIAL_REPLAY.md) explains how a
recorded proximity input can be recomputed and correlated with software audio
receipts without another game session or cloud call.
The [fuel-stop comparison guide](docs/LIVE_FUEL_STOP_COMPARISON.md) explains the
new **比较进站方案** question, current-connection inputs and the distinction
between cumulative missing fuel and a conditional next-stop dose.

The September 4 correctness/privacy review fixes are documented in
[the review-fix record](docs/REVIEW_FIXES.md). Rejoin estimates now bind the
future stop lap and physical position around the track, and valid strategy
candidates no longer depend on driving-diagnosis promotion to reach the shadow
policy. Reliable race-audio delivery, full live tactical advice and broader
coaching still require work; the fuel dashboard below does not promote those gates.

## Quick start

### Native Windows app (no HTML)

Double-click a locally built `AEIS-Engineer.exe`. Python, a browser and a local
web server are not required on the target computer. In **模型与本地设置**, cloud
questions are off by default; enter your DeepSeek key locally and explicitly
enable them if desired. Without a key, local explanations still work.

中文快速测试步骤：[原生 EXE 上车测试速查](docs/LIVE_TRIAL_ZH.md)。
原生版启动先显示“上车检查”，直达语音/录制/问答和复盘；只显示已应用设置与软件
状态，不自动开启麦克风、近车语音或云端，也不把就绪状态当作真实听音/驾驶验收。

From a source checkout on Windows, build the standalone executable with:

```powershell
uv run python scripts/fetch_voice_model.py --download
.\scripts\build_desktop.ps1
.\dist\AEIS-Engineer.exe
```

The build requires Python 3.12 x64, Tcl/Tk and `uv`; its dependency group is
locked. The binary is an **unsigned experimental build**, not an installer or
an accepted race-engineering release. Native voice is opt-in: choose input/output
in **语音与 VR**, or leave either on Windows system default; hold F9 or bind a
wheel button to speak, release to ask. Local Whisper recognizes speech, DeepSeek
optionally selects grounded facts, and local Windows TTS speaks through the
selected output. Audio is not uploaded. Actual racing/VR reliability is pending.
See [the native desktop guide](docs/WINDOWS_DESKTOP.md) for setup, private
recording, encrypted key storage, self-tests and current limits.

### Source and offline pipeline

Requirements:

- Python 3.12
- [uv](https://docs.astral.sh/uv/)

```bash
git clone https://github.com/hyper486/iracing-ai-engineer.git
cd iracing-ai-engineer
uv sync --python 3.12
uv run pytest -q
uv run ruff check .
uv run python scripts/check_public_safety.py --include-history
```

Fetch the commit-pinned public Audi/Spa sample only when you explicitly want
the network download:

```bash
uv run python scripts/fetch_public_ibt.py
uv run python scripts/fetch_public_ibt.py --verify-only
```

The raw `.ibt` is intentionally ignored by Git. It may contain `DriverInfo` and
must not be redistributed without a separate rights and privacy review.

Run the offline advisor pipeline:

```bash
uv run python scripts/run_local_cli.py offline-demo \
  --preset public-audi-spa \
  --output ./artifacts/audi-spa-offline-demo.json
```

If the raw fixture is absent or evidence is insufficient, the correct result
is an explicit `WAIT_*` state—not fabricated strategy or coaching.

### Legacy experimental browser dashboard (optional)

From PowerShell in the repository root, with the Python environment prepared:

```powershell
.\scripts\start_live_engineer.ps1
```

This starts a hidden worker for six hours and opens
[the local dashboard](http://127.0.0.1:8765/). Start iRacing yourself. The launcher
defaults to private raw recording under
`%LOCALAPPDATA%\iRacingAIEngineer\captures`, capped at 4 GiB per run; pass
`-NoRecording` to disable it when starting a new worker.

This is **experimental fuel-only estimation**, not a multi-stop, tire, traffic or
corner-coaching release. Learning needs the first observed crossing followed by
at least five valid complete laps by default. Practice speech is opt-in and uses
only browser-reported local English voices. Race speech is disabled, and a hidden
browser tab automatically mutes, so game-background speech is not guaranteed.
Read [the setup, recording and safety limits](docs/LIVE_APP.md) before using it.
These browser-only restrictions do not apply to the native app above.
This milestone does not establish new authentic `SDK_LIVE` driving acceptance.

### Optional DeepSeek engineer

The [DeepSeek framework](docs/DEEPSEEK_ENGINEER.md) adds typed questions, quick
topics, constrained fact selection, local rendering and failure fallback. Use
the native app's settings to enable it, or `start_live_engineer.ps1 -DeepSeek`
for the legacy browser version after configuring a key locally; without a key
it still offers local explanations. No model call happens automatically.
Historical validated session receipts can be explained separately from live
fuel, without promoting old or synthetic evidence into current race advice.
Run `uv run python scripts/rehearse_llm.py` for the no-key, no-game synthetic
loopback check. Real model/account and live racing validation remain separate.

### Read-only state bridge

With the simulator already running on the same Windows desktop, stream bounded
read-only state for a future overlay or speech process:

```bash
uv run python scripts/run_local_cli.py monitor-live \
  --source-id local-monitor \
  --session-id practice-session \
  --expected-source-kind live \
  --require-in-car
```

This command emits privacy-safe JSONL and does not persist raw telemetry. It
does not calculate or speak tactical advice. See
[the live-monitor contract](docs/LIVE_MONITOR.md).

To reuse Crew Chief's acquisition source without running its application,
see [the standalone reader prototype](docs/CREWCHIEF_READER.md). It is an
opt-in `collect-live` backend; the default Python SDK reader and protected
deployment remain unchanged.

## Architecture

The numerical pipeline owns all calculations and gates. A future language or
speech layer may explain admitted outputs, but it cannot invent telemetry,
override rules or control the car.

```text
iRacing SDK / IBT
        |
        v
defensive adapters -> normalized replayable telemetry
        |                         |
        v                         v
race strategy models       driving evidence models
        |                         |
        +------------+------------+
                     v
          advisor timeline + report
```

Useful design documents:

- [Product specification](docs/PRODUCT_SPEC.md)
- [Technical architecture](docs/ARCHITECTURE.md)
- [MVP plan](docs/MVP_PLAN.md)
- [Offline demo contract](docs/OFFLINE_DEMO.md)
- [Pit and stint evidence](docs/OFFLINE_PIT_STINT_RECEIPT.md)
- [Strategy readiness](docs/OFFLINE_M2_STRATEGY_RECEIPT.md)
- [Time-domain rejoin estimate](docs/TIME_DOMAIN_REJOIN_ESTIMATE.md)
- [Tire-performance boundary](docs/TIRE_PERFORMANCE_BELIEF.md)
- [Frozen tire-model holdout validation](docs/TIRE_MODEL_VALIDATION.md)
- [Native tire-calibration context preflight](docs/LIVE_TIRE_CALIBRATION.md)
- [Conditional next-stint tire/service comparison](docs/LIVE_TIRE_COMPARISON.md)
- [Same-action mapped pit briefing](docs/LIVE_PIT_BRIEFING.md)
- [Driving diagnosis evidence](docs/OFFLINE_DRIVING_DIAGNOSIS_EVIDENCE.md)
- [Post-session report](docs/OFFLINE_SESSION_REPORT.md)
- [Privacy-safe live monitor](docs/LIVE_MONITOR.md)
- [Experimental local fuel dashboard](docs/LIVE_APP.md)
- [Standalone Crew Chief-derived reader](docs/CREWCHIEF_READER.md)

## Public/private boundary

This repository contains the reusable product core, public/offline tools,
privacy-safe examples and reproducible tests. It deliberately excludes:

- raw or private telemetry and replays;
- machine usernames and user directories;
- Tailscale, RustDesk or other remote-access identifiers;
- EAC/WPR logs, ETL traces, crash dumps and machine receipts;
- the byte-exact, host-bound Aeis deployment and recovery evidence chain.

Examples use placeholders such as `C:\Users\racer`, `/Users/racer`,
`racer@aeis.example.invalid` and RFC 5737 documentation addresses. The private
deployment archive is retained separately so privacy sanitization cannot be
mistaken for a byte-identical deployed artifact.

## Development workflow

Major milestones are committed and pushed to `main` after the test, lint,
diff and public-safety gates pass. A milestone includes a new user-visible
engineering capability, a goal-acceptance change, a deployment/recovery
boundary, a security/privacy fix or a release. Repository-specific agent rules
are in [AGENTS.md](AGENTS.md).

Security issues must be reported privately according to
[SECURITY.md](SECURITY.md). Do not attach telemetry, replay/session data,
credentials or host evidence to public issues.

## License

No license has been granted yet. Public visibility allows inspection, but does
not grant permission to copy, redistribute or create derivative works. A
future license decision will be made explicitly rather than inferred from
repository visibility.
