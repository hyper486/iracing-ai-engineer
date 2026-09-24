# Observed stint and raw pace questions

This advisor-only slice adds current observations to the native engineer. It is
not a tire-wear sensor, physical tire-age model, calibrated degradation model,
remaining-tire-life forecast or recommendation to change tires. The final
endurance tire/service/rejoin goal remains unfinished.

## What can be asked

- **这一段跑了多久 / 这段跑了几圈**: elapsed observation time and the increase in
  the completed-lap counter. Attachment during a run is explicitly partial. An
  observed pit-road exit starts a new driving stint, not a new tire set.
- **轮胎怎么样 / 该换胎了吗**: how long the current compound and used-set counters
  have remained readable and unchanged, plus raw pace comparison when available.
  Unchanged counters do not establish the age or condition of all four tires.
- **配速变化**: compare the median of three recent clean laps with the median of
  the preceding three consecutive clean comparable laps. Report the change in
  median starting fuel too. No fuel-weight correction or causal tire attribution
  is applied; driving execution, tire state and unobserved conditions can matter.

These exact routine questions use local facts, no provider request or wait.
Free-form explanations can use the existing opt-in DeepSeek evidence selector;
only allowlisted rendered facts, not raw traces or tire-state guesses, are sent.
Offline fallback retains relevant observations. Full detail stays in the window;
the PTT answer is shorter and retains the interpretation limits. New shortcuts
**问本段 / 问轮胎 / 问配速** and a dedicated observation status row are native Tk.
Stint speech rounds observation time to an explicitly approximate whole minute;
the full text retains tenths. Tire speech prioritizes the unchanged-counter
observation and missing change-decision evidence; detailed raw pace remains in
the full answer and is spoken by **配速变化**.

## Admission, reset and ownership

The stint tracker runs in the existing analysis owner and keeps two origins plus
the last numeric point; it adds no thread, SDK read, disk write or growing history.
It requires current owned in-car/full-simulator data, direct readable counters,
monotonic time/ticks and matching session/player. It resets observations on
source/identity loss, counter regression or jumps, and gaps over 0.25 seconds.
Joining midway or recovering a gap starts another partial observation.

Pit entry/exit changes stint state. A fuel-only pit exit **preserves** the tire
counter observation. A changed or missing compound/set counter resets only that
observation; it never proves a full fresh set was installed. Brief read loss
between the half-second UI publications still advances the revision, so a later
good frame cannot silently preserve an old answer. Module faults remain visible
until reconnection and do not disable fuel, traffic, coaching or proximity.

Pace uses the existing bounded recent-lap worker, not a second lap collector.
The six laps must be consecutive and admitted through the existing clean-lap,
99.9% coverage, 0.1-second maximum gap, traffic, weather, fuel and tire-context
filters. Every pair must satisfy the observed-condition bounds. An intervening
rejected or incomparable lap is not skipped to manufacture a six-lap trend.
The same latest-lap/source/worker bindings apply; the median comparison is
independently recomputed from the six bounded numeric summaries.

Question-time answers expire after ten seconds. Their own observation/model
revision, relevant completed-lap or source change withdraws them permanently;
unrelated fuel-model or traffic churn does not cancel a standalone observation.
Neither freshness nor synthetic SDK-shaped tags establish authentic live origin.

## Verification boundary

Regression tests cover fuel-only stops, partial attachment, missing/changed
counters, source gaps, session/player changes, malformed projections, transient
read loss, fault isolation, six-lap medians and rejected intervening laps. Fake
PTT tests exercise the real local question service, and hidden native controls
exercise the three shortcuts. They are not microphone, playback or VR tests.

The frozen EXE self-test now requires seven numerical checks, including actual
local stint/tire-observation and raw-pace answers. Its streaming fixture needs
at least eight synthetic laps including capture guards. It does not connect to
the SDK, call a provider, play audio or produce a live acceptance receipt.

Still required: genuine in-car observations, confirmation of available tire and
service signals for the car/session, matched performance/fuel calibration,
tire-change service costs, strategy integration and hardware/VR acceptance.
Offline pit-bounded stint age must not be promoted to physical tire age or a
calibrated current-tire model. The application never operates the vehicle,
launches the simulator or changes the pit black box.
