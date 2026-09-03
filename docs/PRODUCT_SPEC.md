# Product Specification: iRacing AI Engineer

## 1. Product goal

Give iRacing drivers without a human engineer two capabilities:

- During a race, maintain a reliable strategy plan that updates and explicitly expires when its assumptions change.
- After a session, use telemetry evidence to identify the corners and concrete actions most worth practicing.

The successful experience is not the one with the most information. It is the one that says the most important sentence at the right time, for example:

> Box this lap, 34 liters, no tires. Expected to rejoin 2.6 seconds ahead of car 23.

Or after the session:

> T5 lost 0.21 seconds, mostly at the exit and on the following straight. You reached 50% throttle 14 meters later than the reproducible reference; braking later did not produce a higher minimum speed. For the next practice group, release the brake earlier instead of moving the braking point later.

## 2. Two operating modes

### 2.1 Race Engineer

During a race, maintain:

- remaining distance and “possibly one more lap” scenarios;
- a distribution of fuel use per lap rather than one average;
- earliest, recommended, and latest pit laps;
- fuel-to-end, splash, and full-fuel plans;
- service time for no-tire, tire-change, and legal-compound options;
- cars ahead and behind, slow traffic, and multi-class traffic after a stop;
- opponent stints and pit probability over the next few laps;
- yellow flags, pit-open/closed state, penalties, and event rules;
- tire performance degradation and a current tire-state estimate that is kept distinct from measured wear.

The Race Engineer emits only structured recommendations that have passed hard constraints:

```text
Recommendation {
  action,
  valid_until,
  reason,
  confidence,
  expected_gain_range,
  risk,
  alternatives,
  supersedes_id,
  evidence_ids
}
```

When an old plan is no longer valid, the system must explicitly revoke it rather than leaving two contradictory spoken plans active.

### 2.2 Driving Engineer

After a session, complete:

- clean-lap filtering and condition matching;
- alignment on a 0.5–1 meter spatial grid rather than timestamps;
- automatic learning of track-corner templates;
- decomposition of each corner into approach, braking, rotation, exit, and carry;
- local time-loss calculation and exit loss carried into the next braking zone;
- checks of braking point, release curve, trail braking, minimum speed, throttle pickup, second lift, and steering corrections;
- line and curb analysis only when evidence is sufficient;
- display of only the top three opportunities, with A/B targets for the next practice group.

The default reference is not an isolated personal best. It is a representative lap from the fastest group of clean laps under the same conditions. A theoretical sector composite may be shown, but it must be labeled as a diagnostic upper bound rather than a drivable reference lap.

## 3. Facts, inferences, and recommendations

Every field must carry a source label:

- `SDK_DIRECT`: directly observed from the current SDK frame;
- `PIT_SNAPSHOT`: refreshed after a pit stop and representing the removed tire set;
- `DERIVED`: deterministically calculated from direct observations;
- `INFERRED`: an estimate with a model and uncertainty;
- `USER_RULE`: supplied by the user or event configuration;
- `UNKNOWN`: insufficient evidence.

Product wording must follow these examples:

- It may say, “Current fuel is 18.4 L.”
- It may say, “The model expects 6.8–7.3 laps remaining.”
- It may say, “Estimated tire performance degradation is about 0.18–0.27 seconds per lap.”
- It must not say, “The opponent has 8 L left,” or “Your current right-front tire has exactly 63% remaining.”

## 4. MVP scope

The first version deliberately narrows the problem to:

- Windows;
- road courses;
- one target car, track, and event-rules profile;
- dry conditions;
- advisor-only operation;
- local data and local decisions;
- race strategy optimized for safe finish time, not directly for iRating;
- driving analysis against the driver's own condition-matched laps;
- line and curb recommendations disabled until path quality and repeated evidence clear their gates.

The MVP includes:

1. Live SDK collection and `.ibt` import.
2. An append-only session log.
3. Tick-by-tick frozen replay.
4. Lap, stint, pit, and flag-event recognition.
5. Fuel distributions, fuel-to-end, and pit windows.
6. Basic pit loss and rejoin traffic.
7. Three high-value driving diagnostics.
8. A shadow-recommendation log.
9. A post-session HTML or local report.

## 5. Non-goals

The first version does not:

- automate driving or modify live driving inputs;
- use screen OCR to obtain state that the SDK intentionally does not expose;
- analyze an opponent's complete pedals, steering, or true fuel;
- use an end-to-end reinforcement-learning policy;
- invent an unsourced “professional ideal line”;
- introduce a high-risk new driving action for the first time during an official race;
- depend on a cloud LLM for critical decisions;
- commercialize the product or upload other people's data. Commercial use requires a separate review of iRacing permissions and data terms.

## 6. Live communication policy

Priority levels:

- `P0`: fuel shortage, pit closed, rules risk, or invalid data;
- `P1`: pit this lap, service content, or a critical strategy change;
- `P2`: window opening soon, undercut opportunity, or fuel-saving target;
- `P3`: overlay/log only.

Speech constraints:

- Put the action first, with at most one reason and the necessary numbers.
- Trigger only after the state is stable for several samples.
- Use cooldown and hysteresis to avoid changing the plan every lap.
- Delay ordinary speech to a long straight with the steering near center, no braking, and no side-by-side traffic.
- Expire and recompute a pit recommendation immediately after the pit entry is missed.
- Provide a one-click mute at all times.

## 7. Evidence gates for driving advice

Each recommendation contains:

```text
DrivingRecommendation {
  corner_id,
  claim_level,          # descriptive | associational | experiment_validated
  diagnosis,
  action,
  target_range,
  expected_gain_range,
  evidence_lap_ids,
  counterexample_lap_ids,
  confidence,
  guardrails,
  practice_only,
  suppress_reasons
}
```

Example rules:

- **Late braking hurts the exit**: braking is later, but release is later too, minimum speed is lower, 50% throttle arrives later, and carry loss is clear. The recommendation should be “brake earlier and shorter, then release pressure earlier,” not “brake even later.”
- **Early braking creates a long coast**: braking occurs earlier than the reference, coasting lasts longer, and neither minimum speed nor exit improves. Move the braking point later only in small steps during practice.
- **Early throttle causes a second lift**: throttle starts early but is followed by a second lift or a large correction, without a faster exit. Recommend a slightly later but stable single throttle application.
- **Too little or too much trail braking**: combine rotation, steering corrections, and the driver's own faster samples; never judge from one brake-at-turn-in value alone.
- **Use more curb**: require repeated condition-matched clean laps showing stable contact by the same-side tires, higher speed, no 1x, no abnormal yaw or vertical spike, and no second lift before allowing a practice experiment.

Race data usually supports descriptive or associational claims only. Causal validation comes from A/B or ABAB lap groups during practice.

## 8. Acceptance metrics

### Race Engineer

- The fuel-out risk of every executable plan stays below the configured threshold.
- Rules-violating recommendations equal zero.
- Fuel-to-end high-quantile coverage reaches its target.
- P50/P90 error for pit-exit gaps is measurable.
- False tactical speech per hour and contradictory recommendations have explicit limits.
- No new tactical recommendation is emitted when data is stale.
- P95 decision latency does not affect simulator frame rate.

### Driving Engineer

- Corner boundaries, braking points, apexes, and throttle-pickup points have a human-labeled regression set.
- Time-loss windows sum to the actual lap-time difference.
- The direction of the top-three recommendations on holdout laps beats a baseline that only compares personal-best speed curves.
- The false-positive rate of high-confidence recommendations is acceptable.
- In forward A/B practice, the target corner and carry time improve without increasing 1x events, second lifts, or large steering corrections.

## 9. Product differentiation

iRacing Season 3 2026 already includes Track Map, Fuel Calculator, pit windows, and AutoFuel. The project therefore does not treat a basic fuel table as differentiation. The core differences are:

- traffic-aware candidate pit simulation;
- opponent stint and pit probability;
- uncertainty and alternatives;
- a personalized tire-performance model;
- traceable, corner-level action recommendations;
- engineer-style explanations of “why” and “what if.”
