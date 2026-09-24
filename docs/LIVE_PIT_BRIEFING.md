# Same-action pit briefing

**综合进站 / 综合进站方案 / 进站简报** adds a local native/PTT answer that
keeps each mapped pit action's fuel dose, service alternatives, traffic and
conditional tire-performance coverage together. It does not choose a winner or
send simulator commands. No provider call is needed.

## One action, separate evidence

The entry/exit positions, distances, arrival fuel, target fuel, dose, next-stint
budget and later-stop count come from the exactly revalidated
[mapped rejoin projection](LIVE_MAPPED_REJOIN.md). Grouping uses these actual
values, not the names "early" and "late" from the different whole-lap model.
There are at most two mapped actions and two service variants per action.
Internal action hashes bind source publication/configuration and those values;
hashes and opponent identifiers never enter provider facts.

Each service retains its own complete-loss interval and independently projected
ahead/behind ranges or explicit unknown. The UI's "完整耗时假设" is the complete
net time cost relative to continuing on track, not an observed entry-to-exit
wall-clock duration; the historical pit-visit question is separate.
Four-tire benefits occur **after**
rejoin; they are never subtracted from the immediate pit loss to make the
predicted exit traffic look better. Missing tire calibration leaves fuel,
service and traffic visible. Unknown traffic in every service variant does not
erase an otherwise supported fuel/service calculation. If the underlying mapped
owner cannot construct actions at all, this briefing waits; independent fuel
questions and the proximity lane remain available.

Fixed complete-loss settings remain an unspecified-service action, not a
fuel-only versus four-tire comparison. Component service settings explicitly
cover pumping, parallel/sequential tire work and all other non-overlapping
overhead. Common transit/overhead cancel only within the same-stop assumption;
the extra tire time is not obtained by subtracting independent interval bounds.

## Complete laps, not a fractional-lap invention

The current tire model is an empirical whole-lap age slope, without a spatial
tire-loss profile. It cannot justify prorating a partial lap by distance or
time. This briefing therefore retains the missing portions explicitly.

For current position `P`, mapped entry `I`, exit `E` and mapped next-stint budget
`N`, let `H = I + N`, `S = ceil(E)`, `T = floor(H)`, and `C = max(0, T-S)`.
Only the `C` complete track laps inside `[E,H]` are modeled. The unmodeled total
partial distance is `max(0,H-E-C)`; overlapping head/tail fragments are not
double-counted when there are no complete laps. Positions retain the mapped
owner's nine-decimal precision, including exact finish-line exits.

For current confirmed-origin counter increase `a`, hypothetical new-set ages
at complete-lap ends span `S+1-floor(E)` through `T-floor(E)`. Old-set ages add
`D = a + floor(E) - floor(P)`. A fractional exit therefore does not pretend its
first partial crossing was a full lap. Under the historical linear-age slope
envelope `[s_low,s_high]`, modeled covered gain is `[s_low*D*C,s_high*D*C]`.

For each whole-lap start `x` in `S..T-1`, fuel is `target - burn*(x-I)`, using
the same entry-based fuel budget as the mapped owner, **not** measured pit-lane
consumption. The entire complete-lap age/fuel block must fit the calibration's
observed marginal domains. No supported prefix is quietly substituted for an
unsupported block. No complete laps means unknown benefit, not zero benefit.

The answer separates covered gain, extra tire service and covered gain minus
service. `net_stint_gain_range_s` always remains null: partial segments, tire
warmup, traffic effects, future conditions and event rules are not validated.
There is no ranking, wear percentage, safe-keep judgment or pit recommendation.
The driver-confirmed origin, unchanged-condition/linear-model assumptions and
limits of marginal bounds from [the tire contract](LIVE_TIRE_COMPARISON.md)
continue to apply. An empirical envelope is not statistical confidence.

## Native, voice and failure behavior

The engineer page has a **综合进站** shortcut in the second supplemental row.
Exact PTT questions use fixed local facts. The first budget action is summarized
with a short fuel/extra-service budget, not selected as optimal. Separate local
questions **综合换胎收益**, **综合仅加油交通** and **综合换胎交通** report its
covered tire gain or each service's traffic. They use outward whole-second
speech intervals; full text retains finer intervals, both actions and mandatory
limits. Splitting the response avoids extending freshness to fit a monologue.
Every query takes a fresh snapshot; they are not a locked multi-question plan.
Arbitrary provider-assisted questions can only select locally rendered facts.
Calibration samples, raw profiles, hashes and car identifiers are not forwarded.

The combination is calculated in the question/evidence path, not on SDK ingest
or while holding the shared Spotter lock. It adds no background loop or retained
race-length history. Its fixed failure notice does not disable fuel or proximity.
Answers have the existing ten-second TTL and selected strategy, rejoin, origin,
calibration and coverage bindings. Material domain/coverage changes withdraw old
numbers; fine distance drift is not bound through the per-publication action
hash. Withdrawn answers do not revive after recovery.

## Verification boundary

Invented tests cover real streaming analysis owners, wrap/exact-exit geometry,
partial coverage, both service modes, whole-domain rejection, unavailable lanes,
source tampering, provider privacy, withdrawal and fake PTT playback. The frozen
`SYNTHETIC_SAME_ACTION_PIT_BRIEFING` check requires learned fuel, a pinned invented
training/holdout request, real confirmation mailbox, both actual mapped service
projections, numerical local answer and calibration-clear withdrawal.

The separate frozen `CHINESE_PIT_BRIEFING_TTS_DURATION` check synthesizes the
four production-rendered invented-case replies to memory, requiring each WAV to
fit 9.5 seconds. One local source check measured 8.14–8.23 seconds. This is not
latency, selected-device playback, different-voice speed or hardware acceptance;
state changes can still interrupt an answer. No freshness limit is relaxed.

No authentic calibration, SDK session, microphone, selected-headphone hearing
or VR timing is established. This closes a software integration gap; full
rule-aware multi-stop planning and the original endurance goal remain open.
