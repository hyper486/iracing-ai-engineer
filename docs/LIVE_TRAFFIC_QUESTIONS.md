# Local traffic and pit-state observations

Source implementation, not an updated EXE or an in-car acceptance result.
This adds question-time observations to the native engineer; it does not release
an optimal pit strategy, future rejoin forecast or a second collision Spotter.

## Questions and display

The native **问交通** button and exact PTT questions use locally rendered facts,
without DeepSeek, a provider budget or fuel-learning readiness:

| Example | Supported answer |
|---|---|
| 前后车情况？ | Nearest usable cars ahead/behind along the circular track |
| 前车多远？ / 后车多远？ | The requested direction, in approximate distance, not seconds |
| 现在允许进站吗？ | SDK permission for this player and available flag observations |
| 该进站了吗？ | Available permission, flags, fuel and traffic, with missing tactical evidence |

As with [fuel questions](LIVE_FUEL_QUESTIONS.md), the exact vocabulary normalizes
case, common punctuation and one courtesy prefix. A one-second duplicate/rate
guard applies. Mixed/explanatory questions retain the constrained planner.
The ordinary local questions can supersede an older pending cloud answer.

The native window has an independent traffic health line. Missing geometry,
stale/ineligible source, longitudinal ambiguity and a latched analysis fault
are distinct from fuel learning and proximity readiness. A traffic-analysis
exception is contained until reconnection; it does not disable the fuel model
or urgent proximity detector, and native exception text is never displayed.

Spoken answers are deliberately brief. Distances at or above one kilometre use
approximate kilometres to one decimal in speech; full text retains rounded
metres and the available-data qualification. Longitudinal distance is not a
time gap, race/class ranking, lateral clearance or future rejoin distance.
For pit questions, full text retains the missing calibration/rules/forecast;
speech explicitly leaves pit timing undecided. A readable `PitsOpen` value is
permission for the current player, not evidence that entering now is optimal.
Flags are observations, not a complete rulebook or a decoded penalty/service plan.

## Data and calculation contract

The SDK owner first feeds the urgent proximity detector. The bounded analysis
lane consumes the same frozen frame through the existing normalizer and computes
traffic at the existing half-second display publication interval. It does not
read the SDK again or call a network service. Bound session metadata is reduced
to a validated metric track-length scalar; raw metadata is not passed to this
analysis worker or the LLM context.

Admission requires a fresh connected live app, full/in-car physics context,
non-stale admitted quality, current player position and matching opponent arrays
with direct provenance. Missing/read-error/malformed arrays are not zero traffic.
The player and eligible opponents must be on the racing surface and not on pit
road. Inactive/pit/non-track slots are excluded. No eligible cars means **no
locatable opponents**, not a clear track.

The existing offline adapter's bounded metric-length parser and integer circular
geometry are reused. Positions wrap at start/finish. Completed-lap counter
differences are not interpreted as whole-lap advantage; two cars straddling the
line may still be only metres apart. Speed is not used to fabricate a time gap.
Any eligible car within five longitudinal metres makes the ahead/behind relation
ambiguous, so both nearest-distance claims are withheld. Lateral occupancy and
clear calls remain the independent [Spotter's responsibility](PROXIMITY_SPOTTER.md).

The `live-traffic-observation-v1` projection binds the monitor identity, sequence
and session time; consumers check types, bounds, counts and source agreement.
This is local consistency, not authentication of the SDK producer. It does not
manufacture the sealed capture receipt required by offline strategy evidence.
Car slots, binding hashes, raw arrays and driver identities are excluded from
the provider context; only locally rendered allowlisted facts are eligible.

Field meanings were checked against the upstream
[pyirsdk field reference](https://github.com/kutu/pyirsdk/blob/master/vars.txt)
and [flag/track-location definitions](https://github.com/kutu/pyirsdk/blob/master/irsdk.py).
No new third-party source was copied for this integration.

## Freshness, isolation and verification

Answers containing traffic or pit observations expire at ten seconds from the
question, including time spent waiting for a provider. They also withdraw on
session/lap/source changes, disappearance of selected facts, changed neighbours,
longitudinal ambiguity, pit permission or flag changes. A published interruption
increments a separate situation revision, so recovery between answer polls does
not revive an old answer. Normal distance drift or metadata refresh alone does
not restart every utterance: it describes the question-time observation.

Standalone traffic/pit answers, including unavailable-data notices with no
selected facts, do not bind fuel's learning/refuel revision.
Mixed fuel/situation answers retain both dependencies. Ordinary fuel-only
answers keep their existing thirty-second limit. Playback checks answer validity
while speaking; an urgent proximity call can still interrupt it. A slower local
voice or delayed synthesis can cause expiry before completion; expired answers
are cancelled, not rebased onto newer telemetry.

Synthetic tests cover circular geometry, overlap, exclusions, malformed arrays,
bound metadata, private-string filtering, independent fuel/traffic health,
published loss/recovery, delayed fake cloud results, the reader-to-native display
and fake PTT-to-answer paths. Invented SDK-shaped frames are not authentic live
evidence. Memory-only local TTS checks of five sample responses produced about
7.1–8.7 seconds of audio; no output device or microphone was opened. Those are
clip durations for one local voice, not response latency or hardware acceptance.

Real fuel comparisons, supported action-bound pit/rejoin integration, incremental
corner coaching, private event/audio replay, packaging and selected-device/VR
acceptance remain separate work under the [active goal](ACTIVE_GOAL.md).
