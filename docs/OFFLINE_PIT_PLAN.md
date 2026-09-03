# Offline pit-plan post-processor v1

`offline-pit-plan-v1` is a package-external, deterministic post-processor for a
complete `fuel-model-replay-v2` JSON artifact. It adds a recommendation
lifecycle and illustrative pit-service alternatives without changing the
frozen Python package, Windows r7 bundle, Mac return verifier, or LaunchAgent.

## Evidence boundary

The post-processor requires an independently supplied
`--expected-fuel-replay-sha256`. It then recomputes and checks:

- the complete fuel replay binding;
- the model output and model-semantic receipts;
- the scenario and pipeline configuration receipts;
- the event receipt, including count closure and its inner digest; and
- the exact current `fuel-model-replay-v2` nested schemas, types, labels,
  contract values, and upstream shadow-only/race-blocked capability gates.

The ready one-stop path also closes the scenario against the model: current
fuel, remaining laps, reserve and tank bounds, conservative burn, safe laps,
pit window, cumulative refuel, and refuel time must agree. The derived stop
never clamps a fuel-out state to zero, and its fuel-to-end addition must equal
the model's cumulative refuel requirement.

Its own output is always:

- `derivation_status: POST_ADMISSION_DERIVED`;
- `attestation_status: NOT_R7_ATTESTED`;
- `execution_mode: SHADOW`; and
- `advisor_only: true`.

This artifact is downstream of an admitted fuel receipt. It is not covered by
the r7 Windows/Mac trust root and cannot be used to claim that r7 emitted or
attested the plan.

## Unknown real event rules fail closed

If `--rules` is omitted, or a valid rules object has
`profile_status: UNKNOWN`, the command returns exit code `5` with:

```text
quality_gate.status = WAIT_EVENT_RULES
recommendations = []
service_alternatives = []
race_recommendation.status = BLOCKED
```

Malformed, self-inconsistent, executable, or digest-mismatched inputs return
exit code `3`. An output path is created exclusively and is never overwritten.

## Non-official development example

[`development-smoke-unbound-v1.json`](../data/event_rules/development-smoke-unbound-v1.json)
is intentionally unbound to any car, track, or series. Its service times and
pit-lane loss are illustrative `USER_RULE` values, not Audi, Spa, iRacing, or
series truth. The validator refuses to treat a `DEVELOPMENT_SMOKE` profile as
official or bind it to a named event.

Generate a complete fuel replay first, then derive the smoke-only plan:

```bash
uv run python scripts/run_local_cli.py fuel-replay \
  "data/raw/audir8lmsevo2gt3_spa up.ibt" \
  --source-id public-audi-r8-evo2-spa \
  --session-id public-fixture-2023-12-race \
  --current-fuel-l 20 --tank-capacity-l 120 --refuel-rate-lps 2 \
  --remaining-laps 10 --require-ready > /tmp/audi-spa-fuel-replay.json

uv run python scripts/build_offline_pit_plan.py \
  /tmp/audi-spa-fuel-replay.json \
  --expected-fuel-replay-sha256 \
  1f3b642c43dd6b7cd16e433dee3f26335f9aecd0c83950e02b706a2f79c3a65c \
  --rules data/event_rules/development-smoke-unbound-v1.json \
  --output /tmp/audi-spa-development-smoke-pit-plan.json
```

On the pinned Audi/Spa replay, the checked development run produced:

| Field | Development-smoke value |
|---|---:|
| fuel replay SHA-256 | `1f3b642c43dd6b7cd16e433dee3f26335f9aecd0c83950e02b706a2f79c3a65c` |
| pit-plan SHA-256 | `ce6d8ee146eb70f40aab14a1a77c35c979effe9e94e1aa473a607f3f66bda376` |
| serialized output | `6,946` bytes, SHA-256 `a0773afef4dc2904507182698684b2e49f401fdb202ecc882192232ef9d64d71` |
| quality status | `PASS_DEVELOPMENT_SMOKE` |
| plan scope | `FUEL_FEASIBILITY_ONLY` |
| illustrative candidate | lap `+4`, add `20.986486 L`, `fuel_to_end:no_tires` |
| service alternatives | `4` |
| traffic/rejoin | `SKIP` |
| race recommendation | `BLOCKED` |

The lap, fuel, and service choice above is not a recommendation for a real
race. It only proves that an explicitly unbound rules profile can be applied
deterministically without promoting traffic, tire, or official-rules claims.
Because current tire performance is unavailable, equal-time development-smoke
service choices prefer the no-tire alternative unless the rules explicitly
require tires; this is not a tire-performance claim.

## Lifecycle contract

A smoke candidate contains deterministic `valid_until` recomputation
predicates for the next lap, fuel change, pit-state change, rules change, or a
stale/reset/schema event. Every plan binds the admitted source kind, source ID,
session ID, normalized sample receipt, model/scenario receipts, and rules
lineage. A previous plan is accepted only after exact-schema and digest
revalidation and only for the same source/session identity. Different source
or session identities fail closed; they cannot be represented as lifecycle
continuity. Within one identity, changed input/model/scenario/rules lineages
are named in the revocation event and both old/new lineage digests are retained.

Passing such a previous complete receipt with `--previous-plan` produces one of:

- `NO_CHANGE` when the recommendation ID is unchanged;
- lineage-bound `REVOKE` followed by `ISSUE`, with `supersedes_id`, when it
  changes; or
- `REVOKE` and no new recommendation when event rules become unavailable.

All recommendation forms remain `SHADOW_ONLY` and `executable=false`.
