# Audi/Spa Public IBT Acceptance Receipt

Date: 2026-08-08

Combination: Audi R8 LMS EVO II GT3 × Circuit de Spa-Francorchamps, Grand Prix Pit

Conclusion: **This public IBT is admitted for the local-only offline fuel and driving-analysis MVP. It is not approved for redistribution or as evidence for personalized coaching.**

## Frozen source

The sample is the Git LFS asset [`audir8lmsevo2gt3_spa up.ibt`](https://github.com/SVappsLAB/iRacingTelemetrySDK/blob/25a9bd21ead72c01806c0690ac25c0e0499d1256/Sdk/tests/SmokeTests/data/ibt/audir8lmsevo2gt3_spa%20up.ibt) from commit `25a9bd21ead72c01806c0690ac25c0e0499d1256` of [`SVappsLAB/iRacingTelemetrySDK`](https://github.com/SVappsLAB/iRacingTelemetrySDK). The repository carries an [Apache-2.0 license](https://github.com/SVappsLAB/iRacingTelemetrySDK/blob/25a9bd21ead72c01806c0690ac25c0e0499d1256/LICENSE), but the telemetry file has no separate driver/origin statement and its SessionInfo contains DriverInfo records. A repository license cannot establish rights the contributor did not own. The raw sample therefore remains gitignored and must not be redistributed without a separate rights, provenance, and privacy review.

- Local path: `data/raw/audir8lmsevo2gt3_spa up.ibt`
- Bytes: `162,304,117`
- SHA-256 and Git LFS object ID: `754d14e6e2870eb00d42368e36c7a15495cb68bfd5f829ab08665d0f95fc7f36`
- IBT contract: v2, 60 Hz, 151,892 records, 275 variables, 1,068 bytes/record
- File boundary: zero trailing bytes
- Privacy: the raw file contains `DriverInfo`; analysis receipts do not export it

The complete machine-readable provenance and analysis receipt is [`data/public_sources.json`](../data/public_sources.json).

## Data-quality result

| Capability | Status | Evidence |
|---|---|---|
| Deterministic replay | PASS | 151,892 frames; no duplicate/regressing time, tick regression, or dropped tick |
| Lap segmentation | PASS | 17 structural and quality-complete laps |
| Fuel-model smoke test | PASS | 15 eligible no-pit laps; mean burn 3.918 L/lap |
| Driving-analysis smoke test | PASS | 7 clean laps; best clean lap 2:20.797 |
| Personalized coaching evidence | SKIP | The cohort matcher exists, but this sample has only 7 clean laps, no opponent arrays, and no authenticated human labels |

Observed fuel burn ranges from 3.688 to 4.003 L/lap. Those values describe this recording only; they are not a live-race recommendation without fuel mode, setup, track state, weather, traffic, and caution context.

## Deterministic replay receipt

- source: `754d14e6e2870eb00d42368e36c7a15495cb68bfd5f829ab08665d0f95fc7f36`
- normalized frames: `dfaa4e812faccdc9f3d87700d8f8b730b3ecde7ba72ae812a7c28c62c9443677`
- events: `af5c26524730dba768ffaa12b53ca56d4f0199a2076565505415deb08c1654f6`
- results: `e7f5760df246a04b1f2dd5bf8134930a350a283633a6627768c74e74f6d5013a`
- final replay: `2250d4ad4c08ea013885c4df8ad839ecf335f582a63bafc8b9720154201f10ce`

Frame partition sizes 4,096 and 7,777 produced the same replay hashes. Events and lap results remain `batch-v1`, so this proves deterministic offline processing rather than a future streaming state machine.

## Shared normalized event receipt

The IBT adapter now feeds all 151,892 frames through `normalized-telemetry-v3`
and the same `telemetry-events-v1` streaming state machine used for collector
JSONL. Two fresh processes, including one with `PYTHONHASHSEED=987654`, produced
the same receipt:

- accepted/rejected samples: `151,892 / 0`
- source/session epochs: `1 / 1`
- events: `54` (`17` completed laps, `18` timing-line wraps, `13` flag changes,
  and one enter/exit pair for pit road and pit stall)
- events SHA-256: `bdb1598994f62b1c707d1d0aef5c9725d64dcc1b200fa9faa3330433e8a546b3`
- receipt SHA-256: `6abc981e717fe40311147979db6a5ae0b9d6a44658fabf5ec798af1ffc692800`
- input/config-bound event replay SHA-256: `9ce5eb1e38608309b285cc0e33a8eaa7c8994856ada73f9cfea403a7d85d60f9`

This streaming receipt is deliberately separate from the older `batch-v1`
replay receipt above: their event definitions and serialization differ, so
their hashes are not expected to match. It remains `CANDIDATE_NOT_GOLDEN`
until the 54 transitions are checked against replay video or another human
label source. The `event-replay-v1` digest additionally binds the raw IBT
SHA-256, byte size, explicit source/session labels, the normalization profile,
stale threshold, quality gate, and `telemetry-events-v1` receipt.

## Shared normalized fuel-model receipt

`fuel-model-replay-v2` consumes the same normalized `TelemetrySample` stream as
collector JSONL, feeds the same streaming event pipeline, converts source-
neutral fields into `distance-wrap-v2` laps, and runs `fuel-strategy-v1`.
For the explicit 20 L / 10 laps / 120 L tank / 2 L/s smoke scenario:

- modeled samples: `151,892`
- structural / quality-complete / fuel-eligible laps: `17 / 17 / 15`
- lap receipt SHA-256: `3877685b8189e320b217e252547669eefeeef55560d18b8bfef1fc33c7913290`
- model output SHA-256: `5e483f4f3987a542ca296553fc710bbd04038fd3dee2bf3d7a578f5ae1c76c15`
- normalized-stream SHA-256: `f38d336a4d647886c0a4b8fce32fc4d53ba688b26b7c46d70e780e85c012b07e`
- source-neutral model semantic SHA-256: `d68fe9387c7d83db9f6425503f06be98c116d2cab18b63216963a6fa9ec76fe5`
- source/config-bound fuel replay SHA-256: `1f3b642c43dd6b7cd16e433dee3f26335f9aecd0c83950e02b706a2f79c3a65c`
- quality gate: `PASS`

Equivalent synthetic IBT and collector streams are regression-tested to have
identical lap receipts, model outputs, and model-semantic hashes, while their
provenance-bound replay hashes must differ. All resulting recommendations stay
`SHADOW_ONLY`; event rules and traffic remain explicit blockers, and opponent
fuel remains `UNKNOWN` because the SDK does not expose it.

## Shared normalized driving-model receipt

`driving-model-replay-v1` consumes the same normalized stream and event
pipeline, but builds a separate source-neutral semantic-input digest from only
the values and quality state used by the driving model. Track length is not a
caller input: `6,930,000 mm` comes from the exact
`WeekendInfo.TrackLength` field on the same verified IBT handle. All three raw
incident counters are present for every sample; none is zero-filled or inferred.

- semantic-input SHA-256: `033f0c97e50d881fd5e4fb0996041653681f0f7ab306f6998445b0e23a1e255d`
- structural / quality-complete / clean laps: `17 / 17 / 7`
- lap receipt SHA-256: `327d0f9d3a4a579ce5922035301a5d72e38cd33a7cc5467fbf5ee5557cdf5b6a`
- model output SHA-256: `f7a7165b19dfa08f1576b3f2e495cfbedb2011aa32317b91d3eb725967af3195`
- source-neutral model semantic SHA-256: `74f6f52d5743260cbdcedaa59a0e0620afb1d8c8987195009e31f7cb86399df6`
- provenance-bound driving replay SHA-256: `c5a8f19f156c57c3951e112df24ad3e3f07956961b78c68fe972a534955ebb82`
- quality/readiness: `PASS / PASS`

The seven eligible laps remain `2, 4, 5, 9, 10, 11, 16`; real reference lap
11 produces eight closed accounting windows and one medium-confidence,
descriptive `C01 LONG_COAST` practice candidate. Its 0.279 s median loss and
0.112–0.279 s expected range are observational within this recording, not a
causal or personalized claim. Curb use, trail braking, current tire wear, and
traffic remain explicitly unavailable.

## Condition-cohort receipt

`condition-cohort-v1` evaluates the seven clean laps across weather,
human-labeled track state, lap-start fuel, observed tire-use context, and
direct opponent proximity. With target lap 11, this source has zero matched
laps and returns `readiness_status=trusted_readiness_status=WAIT_CONDITION_DATA`.
The output contains no recommendations.

- condition config SHA-256: `8c89e01fab4db83c4111662169b4525933106db08de318b617d558383a8e1a5f`
- condition semantic SHA-256: `c2f0f9e14445e73862036d6701fba4f7e80b409581e2805205edd2f4ec8d50cb`
- condition provenance SHA-256: `074a8f8c34f8eb557ee970950d2d727515628ca9e84280ed7dc818ee862b85bb`
- complete cohort SHA-256: `83a20c37b40630e2295630ab54f08d7cbabc63ae8dc6ca7ef4245c3773d4d337`
- quality: `DEGRADED`

The blockers are missing approved track-state labels, missing opponent arrays,
unobserved cross-stint set context for some comparisons, and fewer than eight
matched laps. Two fresh processes with different hash seeds produced
byte-identical JSON. This receipt proves deterministic fail-closed behavior,
not personalized-coaching readiness.

## Next implementation target

The first implementation target is now complete in [`shadow-report-v2`](SHADOW_REPORT.md): the 7 clean laps feed a distance-normalized reference-lap comparison, and the 15 fuel laps feed a conservative stint estimator. The frozen smoke scenario produces 8 closed corner windows, one descriptive long-coast candidate, and analysis SHA-256 `5d521bdf7443fe5be9e8ab27cd29254bc0ea56ee0dedf2ddd16eda89224554b6`.

Both models stay in shadow mode until a trusted condition cohort,
independently authenticated corner labels, event rules, and a real live-SDK
capture pass their gates.
