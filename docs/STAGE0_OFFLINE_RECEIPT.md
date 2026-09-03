# Stage 0 Offline Acceptance Receipt

Date: 2026-08-07

Combination: McLaren 570S GT4 × Nürburgring Combined 24h

Conclusion: **The offline IBT adapter, quality gates, lap segmentation, and deterministic replay run successfully; the data does not yet support fuel modeling or corner coaching.**

## Frozen input

Four raw `.ibt` files were copied read-only from the configured Windows iRacing sidecar into local `data/raw/`. The source files are gitignored; the repository stores only the de-identified [`data/manifest.json`](../data/manifest.json). Local SHA-256 values match the Windows source files.

| File suffix | Size | Records | Header laps | Structurally complete laps | Clean laps |
|---|---:|---:|---:|---:|---:|
| `23-28-09` | 18.97 MB | 17,663 | 1 | 0 | 0 |
| `23-34-13` | 14.40 MB | 13,410 | 1 | 0 | 0 |
| `23-38-15` | 30.80 MB | 28,776 | 2 | 0 | 0 |
| `23-49-52` | 85.47 MB | 80,012 | 5 | 1 | 0 |

The common input contract is IBT v2, 60 Hz, 277 variables, 1,067 bytes per record, and schema SHA-256 `6085fd35487871a1553c3edcc24c4818943d076afcf17968fe8d00ba6351fe8d`. Every data area ends exactly at the file boundary.

`Header laps` cannot be treated as the number of analyzable laps. The longest file contains lap-counter changes in the pit stall, an out-lap, and a partial tail; those stationary counter changes are marked as resets rather than misclassified as start/finish crossings. The conservative algorithm identifies one structurally complete lap with sufficient tick coverage, but that lap contains one duplicate `SessionTime`. Until time reconstruction based on `SessionTick` is implemented, it is not labeled driving-clean.

## Capability matrix

| Capability | Current status | Reason |
|---|---|---|
| `REPLAY_READABLE` | 4/4 ready | Header, schema, records, time, and file boundaries are verifiable |
| `LAP_READY` | 1/4 ready | Only the longest file contains a trusted complete lap |
| `FUEL_READY` | Closed | Requires at least two complete no-pit laps with positive fuel use; there is only one |
| `DRIVING_READY` | Closed | The smoke test requires at least three time-valid clean laps; current count is zero |
| `COACHING_EVIDENCE_READY` | Closed | The cohort matcher is not attached to this legacy Stage 0 receipt; lap count alone is insufficient |

This is therefore not an analysis failure. Reading and replay pass, while downstream capabilities with insufficient evidence remain closed by design.

## Deterministic replay

Canonical frame serialization for the primary sample is unchanged between `frame_hash_chunk_size=1` and `4096`. Events and lap results are still calculated by the `batch-v1` pipeline; this check does not replace a future streaming state-machine test across chunk boundaries.

- source: `f7c4925cd064ee1d364ddbf0d45dad3cf7cb13859aa36eb955d09bcaf528547e`
- normalized frames: `37d516678bc4c5eb4306c76ee06b7c47231f8db66092b789687445777d8e4a24`
- events: `f0e5865e313fa13cffff10e38f9a03003c0e627a10766bc4e5fb6fa96de7c8ad`
- results: `0416512a73659f8ac06aea5223ded9e7d5e53d9c9e20941c762c86d1fa179f11`
- final replay: `df1002d5f3fd4f850c5f80cc184446057daaf1aa7cebf6e57b01793cfd411431`

These result and event hashes are currently `CANDIDATE_NOT_GOLDEN`. Until pit, reset, and start/finish crossings are manually checked against replay video, algorithm output is not presented as human truth.

## Verification evidence

- `ruff check .`: passed.
- `pytest -q`: 29 passed, including the four real IBT checksum/schema checks, bounds and source-change guards, quality capabilities, lap segmentation, and frame-hash chunk invariance.
- [`01_nurburgring_gt4_data_quality.ipynb`](../notebooks/01_nurburgring_gt4_data_quality.ipynb): 16 cells, all 8/8 code cells executed from a clean start with no error output; includes source verification, capability table, lap evidence, a speed chart, and the replay receipt.

## Known boundaries

- `pyirsdk 1.3.6` does not correctly intercept negative or out-of-range indexes in `IBT.get()`; this adapter checks indexes before calling it and has regression coverage.
- The primary sample contains one duplicate `SessionTime`, while `SessionTick` is continuous and records retain stable order. The frame is retained and time quality is marked `WARN`; it is not misclassified as a session reset.
- SessionInfo exports only non-driver fields such as track, event, and build; `DriverInfo` is excluded from the notebook, manifest, and logs.
- `COACHING_EVIDENCE_READY` stays closed until a cohort receipt and authenticated human labels are attached; clean-lap count alone cannot prove matching weather, fuel, tire, and traffic conditions.
- This legacy run completes only the offline portion of M0. The read-only
  Windows collector and shared live/offline pipeline now exist, but this receipt
  does not contain a real console-session SDK capture; that proof remains
  `WAIT_WINDOWS_HOST / WAIT_LIVE_DATA`.

## Next data package

To enter the real fuel/driving MVP, record under the same combination, dry conditions, and setup:

1. At least eight consecutive clean full laps; target ten to leave room for traffic and incident exclusions.
2. An AI or Hosted endurance session with at least two complete pit stops.
3. The replay as well, for manual confirmation of start/finish crossings, in/out laps, traffic, and off-track labels.
4. Setup/fixed setup, weather, starting fuel, and the event-rules profile.
