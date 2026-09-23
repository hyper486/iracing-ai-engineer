# Local current-fuel questions

Source implementation; synthetic verification only. This is not an updated EXE,
an in-car timing measurement or a complete pit-strategy release.

## Driver-facing path

The native fuel and strategy shortcuts, and the same questions recognized by
local PTT, use a small exact intent vocabulary. These routine live questions are
answered from locally validated facts without a provider call:

| Example | Supported answer |
|---|---|
| 还有多少油？ | Fresh observed fuel, including while burn is still learning |
| 当前燃油还能跑几圈？ | Estimated whole laps after configured reserve |
| 每圈用多少油？ | Conservative burn, admitted sample count and observed burn range |
| 油够到终点吗？ | Conditional fuel balance against the admitted race horizon |
| 还要加多少油？ | Cumulative deficit to the estimated finish, not a pit-box setting |
| 还要几停？ | Fuel-only lower bound when confirmed capacity permits the calculation |
| 该进站了吗？ | Available fuel evidence and explicit missing tactical evidence |

Case, whitespace, common punctuation and one courtesy prefix are normalized.
Mixed, hypothetical and explanatory questions are not routed by loose keyword
matching; they retain the existing constrained planner and local fallback.
Historical questions are separate and never enter this current-fuel path.

Routine questions have a one-second duplicate/rate guard. They do not use the
provider request budget or wait for its default ten-second question cooldown.
They can replace an older answer while the single bounded cloud request is
still unwinding. The older result cannot overwrite the newer local answer.
Pressing PTT again cancels the old voice wait before asking the new question.
Spotter priority and cancellation still apply to both kinds of question.

The short spoken response retains the limitation relevant to its answer rather
than appending every unrelated unavailable capability. STT and local TTS are
still needed; bypassing DeepSeek is not a guarantee of microphone-to-headset
latency, especially when loading the recognition model for the first time.

## Observation versus prediction

A direct fuel observation requires a fresh connected app snapshot, admitted
full/in-car context, readable `FuelLevel`, no context conflicts and non-stale
monitor quality. Replay, spectator/out-of-car, read failure and disconnection
do not produce a current owned-fuel claim. `FuelLevel` units come from the
[pyirsdk field reference](https://github.com/kutu/pyirsdk/blob/master/vars.txt).

Current fuel does not require five learned laps. A pit/refuel interval may
invalidate burn/range while a valid direct fuel observation remains available.
The default estimator still needs five complete admitted laps after an observed
start/finish boundary; tests may explicitly shorten that threshold. Invalid
intervals cannot become clean training samples.

Reserve, conservative burn, historical min/max burn and race-horizon basis are
separate evidence fields. The observed burn range is not a future confidence
interval. Race finish arithmetic is checked for internal consistency before
publishing the new fuel-balance and stop-count facts. Only exact bound `Race`
metadata enables finish budgets; a practice timer is not a race finish.

For an admitted positive remaining-lap count, the model budgets those full laps.
Otherwise it uses remaining time divided by the fastest admitted lap, rounded
up, plus configured extra laps. This is explicitly an estimated horizon, not a
guarantee about the leader, extra laps, changing pace or event rules. The budget
includes configured reserve. A cumulative fuel deficit may exceed one tank.

Synthetic arithmetic example: 20 L current fuel, 2 L/lap, 2 L reserve and a
15-lap budget imply nine whole laps of current range, 32 L total required and
12 L cumulatively missing. This is **not** an instruction to add 12 L at the
next stop. A configured 50 L capacity permits a one-stop fuel-only lower bound;
it says nothing about mandatory stops, service time or traffic advantage.

The native reader currently uses the default unknown tank capacity. It can
report a cumulative deficit, but withholds a positive minimum-stop count rather
than guessing capacity. The library/CLI supports explicit capacity. Missing
pit-loss calibration, event rules and traffic evidence still block an optimal
pit/rejoin recommendation. Tire wear and driving advice are not inferred here.

## Freshness and verification

Answers bind the session, connection generation, fuel revision and lap. They
expire after 30 seconds or an earlier source/context change. Disappearance of
any selected fact withdraws the entire answer, even when another observation
remains usable. Once withdrawn, an answer does not revive when data recovers.
Normal within-lap fuel consumption does not continually restart an utterance;
the response describes the question-time snapshot, not a continuously updated
command. Stale answers lose their pre-rendered spoken body.

Tests cover invented raw SDK frames through the real monitor and fuel estimator,
learning-to-ready answers, inconsistent budgets, unavailable capacity, non-race
and stale/context guards, provider budget preservation, native presenter state,
and fake PTT/STT/TTS/output while a fake cloud call is blocked. None opens the
game, a microphone, speakers or a provider account. Real lap comparisons,
selected-device hearing, first-call recognition latency and VR load remain open.
