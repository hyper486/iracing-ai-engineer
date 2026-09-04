# September 2026 correctness and privacy fixes

## Outcome and scope

This milestone fixes the reproduced defects from the whole-project review.
It is a software-correctness and public-privacy milestone, not final acceptance
as an in-race AI engineer. The simulator, driving controls, system services and
private telemetry are not changed by this work.

## Correctness changes

- **Live health:** A repeated frozen SDK buffer ages against a monotonic clock
  and becomes BLOCKED, including at final output. A health-only observation
  does not become a newly captured frame or additional in-car evidence.
- **Independent SDK metadata:** A changed SessionInfo update counter does not
  create a conflicting duplicate when the frozen vehicle payload is unchanged.
  A genuinely changed payload under the same tick is still rejected.
- **Quality consistency:** Session-time regressions and other event quality
  rejections are reflected in snapshot status and reasons, with recovery on
  subsequent valid observations.
- **Event privacy:** Monitor details use event-specific allowlists. Source reset
  events no longer expose the previous raw source identifier.
- **Current-action traffic gate:** Legacy AVAILABLE input cannot bypass a WAIT
  returned by the estimate calculated for the current proposed stop.
- **Physical rejoin position:** Completed-lap deficits do not make an adjacent
  car appear a lap away. The shared projection uses circular-track distances,
  checks overlap across any integer lap, and declines ambiguous neighbor order.
- **Future stop timing:** The selected number of laps until the stop participates
  in relative-speed projection, service binding and estimate hashes. Broad
  future uncertainty yields WAIT rather than a definite traffic bracket.
- **Recommendation binding:** Independent advisor admission requires the rejoin
  service scenario to match the actual recommendation, not just its own hash.
- **Independent strategy path:** Valid M2 candidates reach shadow speech policy
  without promoting the driving-diagnosis gates. Muting, timing, confidence,
  source admission and advisor-only safety still apply.
- **Observed zero coast:** Simultaneous or overlapping brake release/throttle
  pickup is a measured zero coast interval, not missing evidence.
- **Corner accounting:** Filtered brake dabs and partial boundary runs no longer
  leave holes in the lap-time accounting partition. This does not implement a
  new circular corner-segmentation model.
- **Windows packaging:** Wheel import assertions compare normalized paths rather
  than requiring POSIX separators on Windows.

## Public privacy and history

Public account/ACL examples use an all-zero synthetic Windows domain SID. The
synthetic cross-language security vector is regenerated from those bytes and
does not attest the identity of a private deployment.

The public safety scanner now covers account/domain SIDs, sensitive telemetry
filenames in every reachable historical tree, commit/tag text, and unreviewed
binary content. Its test inputs are invented; private values are not retained
inside the detector as a blacklist. Local telemetry and replay files stay out
of Git.

This privacy correction requires rewriting the affected public history. An
original-history recovery bundle stays on the private side. Existing clones
must not merge or push their old history back; use a fresh clone after the
updated public main is published. A ref rewrite cannot erase someone else's
clone, fork, or cached old commit, and server-side cache removal may require
hosting-provider support.

## Contracts and verification

The rejoin contract is `time-domain-rejoin-estimate-v2`, with method
`physical-progress-envelope-v2`. The service scenario includes
`recommended_lap_from_now`. The advisor timeline contract is
`advisor-timeline-v3`; the outer M2 receipt versions are unchanged. Outputs
whose bytes depend on these contracts must be rebuilt, not relabeled.

Regression tests exercise frozen/slow SDK reads, quality rejection and recovery,
metadata changes, reset privacy, integer-lap invariance, differing stop horizons,
ambiguous future motion, legacy-gate bypass, self-rehashed action mismatches,
real shadow-policy admission without a forced diagnosis gate, zero coast,
filtered brake runs, Windows wheel imports, and privacy leaks removed from the
current tree but retained in history. Synthetic adapter tests run without a
private Audi/Spa fixture; explicitly data-dependent acceptance tests still skip
when their inputs are absent.

The publication gates are Ruff, the full pytest suite, the history-inclusive
privacy scan, whitespace/staged-file review, and verification in a fresh clone.
No synthetic or replay result is labeled authentic SDK_LIVE acceptance.

## Still outstanding

Real on-track collection awaits the user's driving setup. The project still
needs field validation of the live bridge and rejoin accuracy, real-time
tactical delivery, an audible engineering interface, broader multi-stop
planning, and validated curb/trail-braking coaching. Passing these regressions
does not complete those product requirements.
