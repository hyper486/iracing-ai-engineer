# Driver-confirmed tire installation observation

Contract: `driver-confirmed-tire-observation-v1`.
Basis: `DRIVER_CONFIRMED_FULL_NEW_SET`, **not** `REVIEWED_FULL_NEW_SET`.
This is an advisor-only live observation prerequisite, not a wear sensor,
calibrated performance model or keep/change recommendation.

## Native workflow

At the top of **模型与本地设置**, the three buttons describe what the driver
believes actually happened: **确认四胎全换新**, **确认本次未换胎**, or
**部分换胎／未知**. They record an assertion; they never request service in
iRacing. Do not operate the window while driving.

The application must observe entry into this pit visit continuously, then see
fresh owned in-car data with `OnPitRoad=true`, `PlayerCarInPitStall=true`,
`PitstopActive=false`, speed at most 0.1 m/s and valid direct tire counters.
Attaching while already parked cannot establish that observed visit. A missing
service field disables confirmation, even when the car appears stationary.

Confirmation is checked twice: at the native mailbox against a publication no
older than 0.75 s, then on the analysis owner after a progressing frame. The
immutable command carries the owner revision, observed visit tick, requested
tick, compound and set counter. The owner rejects changed bindings, moving
state, duplicate commands and commands more than one SDK second old. Reconnects
discard pending commands. One command slot is retained, not an unbounded queue.

After confirmation, an observed pit exit can establish an origin:

- **Full new set:** creates a new driver-confirmed origin at the observed exit.
- **Unchanged:** can preserve a previously known continuous origin; never creates one.
- **Partial/unknown or no confirmation:** withdraws the origin.

The display/question reports the increase of `LapCompleted` since that exit,
not full geometric laps driven since installation. Crossing the start/finish
line shortly after exit can already add one. It is not an independently
verified tire age and is never presented as physical wear or remaining life.
**问轮胎 / 轮胎怎么样** uses a short local answer without a provider call;
the confirmed origin/lap binding expires existing answers when it changes.
Confirmation itself currently requires the parked native UI, not a voice command.

## Withdrawal and isolation

Data gaps over 0.25 s, stale/out-of-car/replay context, session/player changes,
clock regressions, lap regressions/jumps and missing/changed tire fields withdraw
the origin. A counter returning to its old value does not restore it. During a
pit visit the age is suspended until an explicitly confirmed continuous exit.
Service restarting, missing service context after confirmation, or leaving and
re-entering the stall invalidates that confirmation. Correcting a full-new or
partial assertion to unchanged does not resurrect an older origin.

The tracker is constant-space on the existing analysis owner, with no SDK,
provider, disk or audio calls. Failures disable this projection alone; current
fuel, proximity and corner analysis retain their independent owners/guards.
No settings store, installed executable or simulator setting is changed by a
confirmation. Uncertainty is allowed to remain unknown for the entire session.

## Private audit and model boundary

When the existing private recording option is enabled, accepted assertions go
to a fixed numeric/enum lane in `private-trial-audit-v3`. It records connection
generation plus assertion kind, owner revision, visit/request/decision ticks,
lap counter, compound/set counter and session/player slot, plus a fixed-field
frame hash and integer capture clock for paired replay. No free text,
question transcript, driver name or secret is accepted. Recording failure
cannot interrupt tire tracking or the other engineering lanes.

The journal reader accepts legacy v1/v2 logs. New v3 logs require an updated
reader; older binaries may reject them. Reports show the number and a bounded
tail of assertions. **This journal replay does not recompute tire age or verify
service contents.** Its replay-match label still applies only to the proximity
detector. The separate [paired capture replay](TIRE_CAPTURE_REPLAY.md) now binds
the full capture bytes and exact assertion frames, then recomputes the counter
under current rules. A separate [native manual review](TIRE_SERVICE_REVIEW.md)
can now export service labels after explicit human dispositions and retained
independent local evidence. These are self-attested inputs, not authenticated
truth; an assertion is never automatically promoted or used to calibrate degradation.

Native admission of an exact event-matched, independently calibrated tire
performance model, its benefit-versus-service/rejoin comparison, authentic
service truth checking and headset/VR acceptance remain open. Synthetic tests
and the packaged self-test exercise the real mailbox, owner, local question,
withdrawal and failure paths without touching the simulator or audio hardware.
