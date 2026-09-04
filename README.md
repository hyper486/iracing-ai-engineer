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
- Append-only collection, normalization, lap/stint segmentation and replay.
- Fuel-to-end, stint, pit-service and time-domain rejoin reasoning.
- Conservative tire-performance beliefs that do not invent physical wear.
- Distance-aligned corner evidence and repeated-loss diagnosis.
- Advisor timelines, shadow speech policy and deterministic session reports.
- Fail-closed source, confidence, privacy and advisor-only safety boundaries.

An authentic `SourceKind=SDK_LIVE` transport canary now reaches the running
simulator's shared memory and completes a sealed collection. End-to-end
acceptance is still pending a human-driven, on-track run with enough laps and
pit evidence for strategy and driving advice. See
[the public project status](docs/PUBLIC_PROJECT_STATUS.md).

## Quick start

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
- [Driving diagnosis evidence](docs/OFFLINE_DRIVING_DIAGNOSIS_EVIDENCE.md)
- [Post-session report](docs/OFFLINE_SESSION_REPORT.md)

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
