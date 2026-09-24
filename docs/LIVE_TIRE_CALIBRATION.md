# Native tire-calibration context preflight

This is a session-only **context check**, not live tire-model admission. Even
`CONTEXT_MATCH_ONLY` retains `live_model_admitted=false` and
`live_acceptance=false`. No tire-change recommendation, game command, new
service assertion or provider request follows from loading a calibration.

## Current car and settings

The reader selects the unique `DriverInfo.Drivers` entry whose `CarIdx` matches
both `PlayerCarIdx` and `DriverInfo.DriverCarIdx`; it does not use the first
driver or a display name. Numeric `CarID` supplies the model id; `CarClassID`
must agree with the current frame's class. The stable metadata update must
match the frame.
Full event identity and a bounded nonempty `CarSetup` are required. In-car
physics, full source scope, fresh monitor evidence and exact source/sequence/
time binding gate the published context. See the upstream
[pyirsdk field reference](https://github.com/kutu/pyirsdk/blob/master/vars.txt).

The versioned fingerprint hashes canonical JSON containing the numeric car
model id, the method name and a bounded plain-JSON setup projection. Method:
`sdk-car-setup-excluding-recorded-tire-measurements-v1`.

Only root `UpdateCount` and these exact fields under
`Tires.{LeftFront,RightFront,LeftRear,RightRear}` are omitted:
`LastHotPressure`, `LastTempsOMI`, `LastTempsIMO`, `TreadRemaining`. They describe
recorded tire measurements, not a configured setup. Unknown fields, fuel
settings, in-car dials and cosmetic settings remain hashed conservatively.
There is no unit normalization, semantic equivalence or completeness claim.
A projection containing only omitted measurements or empty/null/blank leaves
does not establish a setup. Changes can therefore withdraw an otherwise usable
calibration; they cannot silently reuse a different fingerprint.

The reader queues only the bounded selected metadata projection. Hashing stays
on the analysis owner, and metadata faults do not disable fuel or the separate
proximity lane. Snapshots expose numeric ids, fingerprints and numeric current
conditions, not driver/setup names, paths or raw setup values. Existing raw
capture and journal formats are unchanged. Historical captures that omitted
`DriverInfo` cannot retrospectively recover a car model id from this feature.

## Native workflow

1. Prepare the private v1 training/holdout request described in
   [the validation contract](TIRE_MODEL_VALIDATION.md), including an independently
   retained request digest and reviewed matching car/setup declarations.
2. With fresh owned-car data, stop on pit road. In **模型与本地设置**, enter the
   independent request SHA-256 and choose **停车载入校准**. This parked check is
   an interaction restriction, not evidence of completed tire service.
3. The background job bounded-reads the private request, reconstructs its pinned
   training model and holdout report, and requires `PASS_REVIEWED_HOLDOUT`.
   The exact car/setup/event identity must still match when verification ends.
4. Selection is memory-only. A replacement first withdraws the old selection;
   failed verification cannot fall back to it. **清除校准**, connection changes,
   metadata loss, setup changes and stale source withdraw it. An in-flight
   verification cannot restore a canceled or differently bound selection.

The retained object now contains hashes, car/compound ids, the validated frozen
historical model and observed age/fuel/condition bounds. It excludes raw samples
and labels. The separate [conditional comparison](LIVE_TIRE_COMPARISON.md) uses
it only with a confirmed current counter origin and valid fuel/service inputs;
loading alone never generates a benefit. No automatic startup reload, saved-path
preference, source restart or cloud call is introduced.

Each publication checks current compound, numeric air/track temperature and
wind components. SDK precipitation is converted from a fraction to percent;
only observed `TrackWetness=1` and zero precipitation satisfy the dry check.
Missing, wet or out-of-range conditions produce `WAIT`. The weather envelope is
the union of early/late **marginal** training ranges with a numerical epsilon,
not joint support, a forecast or evidence that conditions will remain suitable.
Weather recovery may restore this context-only match; identity/source loss
requires a new explicit selection.

## Validation and open boundaries

Invented-frame tests exercise the production reader/analysis/controller owners,
actual request reconstruction, cancellation, stale/setup withdrawal and native
controls. `SYNTHETIC_TIRE_CALIBRATION_BINDING` in the frozen self-test uses an
explicitly invented prevalidated object to check matching and withdrawal; it
does not verify a real evidence file, contact the SDK/provider or test hearing.

Real reviewed calibration remains absent. A conditional driver-confirmed-origin
comparison now checks full projected age/fuel ranges and an empirical benefit
envelope; it does not admit an independently reviewed live tire belief. Future
condition support, rules and action-bound service/rejoin integration remain open. An
offline holdout pass plus a matching current car is not sufficient to recommend
keeping or replacing tires. Hardware, real in-car and VR acceptance are separate
and remain pending.
