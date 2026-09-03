# Public endurance IBT search receipt

## Decision

Status: `WAIT_PUBLIC_ENDURANCE_IBT`.

The read-only search on 2026-08-23 did not find a public `.ibt` that met all
four admission requirements:

1. an immutable source object;
2. acceptable telemetry-use and redistribution rights;
3. a real endurance session with at least two proven pit/service episodes; and
4. an acceptable privacy boundary.

No candidate was downloaded into this project. A file whose pit count has not
been scanned remains `PIT_COUNT_UNKNOWN`; a repository license is not treated
as proof that the uploader owns every participant's telemetry or identity
data.

## Closest candidates

The closest group was three roughly 50-minute Sebring multiclass GTP race
files in `tfunk1030/racingoptimizer`, fixed at commit
`77ff6b29453e827b0e2b7e56bcdac19073c12fb3`:

| Object | Size | Pre-screened duration | LFS SHA-256 | Privacy observation | Admission |
|---|---:|---:|---|---|---|
| [`fullracebmw.ibt`](https://github.com/tfunk1030/racingoptimizer/blob/77ff6b29453e827b0e2b7e56bcdac19073c12fb3/ibtfiles/fullracebmw.ibt) | 208,437,559 B | 50:13.48 | `0dac608640938ae2ffa964717b2788b7d244e574f8a5c4b3f76e6c8a16e15dd2` | 44 `UserName` fields | `BLOCKED_RIGHTS / PIT_COUNT_UNKNOWN` |
| [`2ndracebmw.ibt`](https://github.com/tfunk1030/racingoptimizer/blob/77ff6b29453e827b0e2b7e56bcdac19073c12fb3/ibtfiles/2ndracebmw.ibt) | 210,732,263 B | 50:46.48 | `cff2729a3538afa4f224e2ab9ae419761eb6bdbdffbcd5ac75201ed8bec533a6` | 51 `UserName` fields | `BLOCKED_RIGHTS / PIT_COUNT_UNKNOWN` |
| [`bmwrace3.ibt`](https://github.com/tfunk1030/racingoptimizer/blob/77ff6b29453e827b0e2b7e56bcdac19073c12fb3/ibtfiles/bmwrace3.ibt) | 208,139,548 B | 50:09.08 | `e51ba3fb07f94909fc120e767ebe901da550fe5c673f82ad9927fa0f81bf9b85` | 47 `UserName` fields | `BLOCKED_RIGHTS / PIT_COUNT_UNKNOWN` |

That repository had no license at the pinned commit. A duration and Race label
do not prove two pit stops, so these objects are not admitted even for a local
derived-data receipt.

Other reviewed objects did not meet the endurance requirement:

- The existing [Audi R8 GT3 / Spa fixture](https://github.com/SVappsLAB/iRacingTelemetrySDK/blob/25a9bd21ead72c01806c0690ac25c0e0499d1256/Sdk/tests/SmokeTests/data/ibt/audir8lmsevo2gt3_spa%20up.ibt)
  is in an Apache-2.0 repository, but the complete local analysis proves only
  one pit/service episode.
- The MIT-licensed [Corvette GT3 fixture](https://github.com/emilioSp/node-iracing-sdk/blob/31d4fc0e1d41294383e511c4851efb8be3850d50/telemetry/corvette_gt3.ibt)
  is only about 6 minutes of Miami practice and exposes 57 `UserName` fields.
- The MIT-licensed [BMW M4 GT4 fixture](https://github.com/jgarbiso/Tenths/blob/aad323a58d97a7d508466c3b159529f3cd9e3f44/tests/data/bmwm4evogt4_midohio%20full%202026-06-02%2020-48-57.ibt)
  is about 8 minutes of offline testing.

## Search and privacy boundary

The search examined roughly 145 related GitHub repositories. Complete recursive
trees were inspected for 38 high-relevance default branches, yielding 237
`.ibt` paths. The largest candidate repository exposed 192 Git LFS pointers,
about 9.96 GB in aggregate. Only immutable tree metadata, LFS pointers, small
header ranges, and SessionInfo ranges were read; participant names were counted
but not recorded or reproduced.

Pre-screening is intentionally ordered to avoid unnecessary data transfer:

1. pin commit, object identity, byte size, and any repository license;
2. range-read the IBT header to derive record duration;
3. range-read SessionInfo to classify session/car/track and count personal-data
   fields without exporting their values; and
4. only after rights and privacy admission, download and scan
   `PitstopActive`, `OnPitRoad`, and stall/service overlap.

The search did not cover private repositories, private Drive/Discord shares,
unindexed sites, every fork, or all deleted history. The result therefore means
"not found in the current verifiable public search," not that no such file
exists anywhere.

## Safe next acquisition path

The preferred next dataset is a user-owned or explicitly authorized AI/Hosted
endurance recording with at least two stops. Before use, preserve the raw file
locally, strip participant identity from exported derivatives, bind the event
rules and car/track/setup/weather metadata, and obtain subjective corner labels.
Until then, the Audi/Spa fixture remains the honest one-stop offline MVP input
and multi-stop strategy remains a synthetic contract test rather than real-data
acceptance.
