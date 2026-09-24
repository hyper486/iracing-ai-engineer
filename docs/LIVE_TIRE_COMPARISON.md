# Conditional next-stint tire comparison

**问换胎收益 / 比较换胎收益 / 换胎值得吗** now compares a historical
performance-model interval with the extra time of four-tire service. This is a
local, question-time calculation, not a DeepSeek estimate. A supported normal
**轮胎怎么样** question can use the same comparison. Without support it retains
the existing bounded observation response; the dedicated question explains
what is missing.

## Inputs and meaning

1. [Parked calibration selection](LIVE_TIRE_CALIBRATION.md) must reconstruct a
   pinned private request with `PASS_REVIEWED_HOLDOUT`. The retained frozen model,
   current owned car/setup/event/compound and observed dry-weather match must
   validate. No automatic model fitting, startup reload or widening is added.
2. [Explicit full-new-set confirmation](LIVE_TIRE_INSTALLATION.md) followed by
   a continuously observed exit supplies the current counter origin. This
   remains `DRIVER_CONFIRMED_FULL_NEW_SET`, not an independently reviewed label,
   SDK service truth, full distance on the set or a measured wear percentage.
3. [Whole-lap fuel scenarios](LIVE_FUEL_STOP_COMPARISON.md) must be ready for a
   race. They provide up to two feasible early/late fuel-budget endpoints, next
   stint length and dose, not a mapped pit entry. If no fuel stop is needed, this
   feature does not invent an additional tire-only stop.
4. Explicit [service inputs](LIVE_SERVICE_COMPARISON.md) must cover four-tire
   duration, refuel rate and parallel/sequential timing. Player pit permission
   must be known/open and unresolved yellow/red/safety-car, penalty/repair or
   session-end flags suppress the comparison. Event regulations remain unverified.

The complete new-set range `1..N` and old-set range `A+1..A+N` must stay within
observed training age bounds. Every projected next-stint lap-start fuel level
must stay within observed training fuel bounds. There is no clipped prefix
gain if only part of the stint is supported. A scenario can be unavailable
while the other is supported; its missing numbers are never borrowed.

These are **marginal endpoint bounds**, not joint-support, tire warmup, future
weather or new-vs-old counterfactual validation. The comparison additionally
assumes the historical linear-age relation applies between those endpoints,
under unchanged dry conditions, setup, compound and clean driving. The holdout
report validates only its actual reviewed pairs; it does not establish every
possible future age difference. No statistical coverage or causal guarantee is
implied by the empirical model interval.

## Calculation

Let `a` be the confirmed-origin completed-counter increase now, `k` the future
whole-lap budget before the stop, `A=a+k`, `N` the next stint length, and
`[s_low, s_high]` the frozen seconds-per-age-lap slope envelope.

- Both scenarios use the same next-stint fuel amounts, so the linear fuel term
  cancels; future common age increases cancel under the linear-age assumption.
- Modeled on-track gain: `[s_low*A*N, s_high*A*N]` seconds.
- Extra service: `max(fuel_time, tire_time)-fuel_time` for parallel work, or
  `tire_time` for sequential work. Shared transit/other costs cancel only under
  the same-stop assumption; no absolute pit-loss prediction is added here.
- Net range: modeled gain minus extra service. The full interval determines
  `BENEFIT_EXCEEDS_SERVICE`, `SERVICE_EXCEEDS_BENEFIT` or `UNCERTAIN`.

Numbers are rounded outward. Negative net gain means modeled time loss, **not**
that keeping the existing set is safe. `physical_wear` and `race_recommendation`
stay null, `executable` and `live_acceptance` stay false. This does not optimize
the entire race or account for later tire choices, traffic, punctures or rules.
It must not be joined by the name “early/late” to mapped-rejoin scenarios:
those use different physical locations and fractional-lap fuel budgets.

## Native and voice behavior

The native settings page shows a concise readiness or net-time line. The
engineer tab has **问换胎收益**. Exact PTT questions bypass the cloud and produce
a short fixed-template answer; arbitrary questions can select the same grounded
facts, but cannot omit the mandatory assumptions. Raw calibration samples,
source hashes, setup identity and model payload are not sent to the provider.

Answers expire after ten seconds and are withdrawn on selected-evidence loss,
changed lap/fuel plan/service inputs, origin/calibration clear, unacceptable
conditions or source changes. Once withdrawn, an answer cannot revive merely
because conditions recover. Unrelated opponent motion alone does not invalidate
a tire-only comparison, which makes no rejoin claim. Calculation runs on the
coherent copied app snapshot outside the shared Spotter lock; a fault publishes
a fixed unavailable notice without disabling proximity or fuel observations.

## Checks and remaining acceptance

Invented tests cover signed interval arithmetic, both service modes, crossing
zero, complete-domain rejection, warmup-range rejection, one supported endpoint,
no-stop behavior, strict model reconstruction, tampering, expiry, withdrawal,
provider limits and failure isolation. The packaged
`SYNTHETIC_CONDITIONAL_TIRE_COMPARISON` check actually builds and verifies an
invented training/holdout request, learns fuel from eight invented laps, records
an assertion through the real analysis-owner mailbox and requires the local
numerical answer followed by withdrawal. It uses no real SDK, provider or audio
hardware. No authentic calibration is shipped.

Real reviewed calibration, actual in-car numerical accuracy, selected-device
hearing/VR performance, mapped service/rejoin integration and event-rule-aware
endurance decisions remain open. The full product goal remains active.
