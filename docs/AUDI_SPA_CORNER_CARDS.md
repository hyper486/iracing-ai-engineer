# Audi/Spa Top-3 loss cards

`offline-corner-cards-v1` is a package-external reporting layer for a complete
`driving-model-replay-v1` JSON receipt. It verifies the source replay, model,
pipeline, lap-duration delta, per-window phase closure, capabilities, and
diagnosis/recommendation binding before ranking at most three recurrent loss
windows. It does not modify the frozen r7 package or Mac live-return bundle.

## Run it

Create a new full driving replay, then build the card receipt. Both output
paths below are local generated artifacts under the ignored `data/derived/`
directory. The card writer never overwrites an existing file.

```bash
mkdir -p data/derived
set -o noclobber
uv run python scripts/run_local_cli.py driving-replay \
  "data/raw/audir8lmsevo2gt3_spa up.ibt" \
  --source-id public-audi-r8-evo2-spa \
  --session-id public-fixture-2023-12-race \
  --require-ready \
  > data/derived/audi-spa-driving-replay-v1.json
uv run python scripts/build_offline_corner_cards.py \
  data/derived/audi-spa-driving-replay-v1.json \
  --output data/derived/audi-spa-corner-cards-v1.json
```

The second command also prints the same canonical JSON to stdout.

## Frozen Audi/Spa result

The current local artifacts are:

| Artifact | Bytes | Serialized SHA-256 |
|---|---:|---|
| Full driving replay | 64,174 | `b1825535c80d316b5379ae646c9710383050637ff7c335e9fe056a1d29010adf` |
| Top-3 card receipt | 13,094 | `ea569e10989fc614577a072fef367817c301db233bd776e731e3f9054813ef22` |

The card receipt's canonical `corner_cards_sha256` is
`62cd256b569b51c0bf54e293faa22e87df4c19e9c47d27899c89afe7fb1aa638`.
It binds driving replay SHA-256
`c5a8f19f156c57c3951e112df24ad3e3f07956961b78c68fe972a534955ebb82`
and model-semantic SHA-256
`74f6f52d5743260cbdcedaa59a0e0620afb1d8c8987195009e31f7cb86399df6`.
A second fresh rendering produced the same 13,094 bytes and serialized SHA.

| Rank | Candidate window | Median accounted loss | Positive comparison laps | Action gate |
|---:|---|---:|---:|---|
| 1 | `C08` | 0.294 s | 5 / 6 | `action=null` |
| 2 | `C02` | 0.230 s | 5 / 6 | `action=null` |
| 3 | `C01` | 0.163 s | 4 / 6 | `LONG_COAST`, practice only |

The supported C01 action is to move the braking point later only in small
practice steps while keeping one continuous release-to-throttle transition.
Its action evidence and counterexamples are kept separate from the laps that
support the descriptive loss ranking.

## Evidence boundary

- `C01`-`C08` are braking-derived candidate windows, not authenticated Spa
  corner names.
- C08 and C02 identify recurrent time-loss locations, but the current model has
  no supported action diagnosis for either. They remain `action=null` with
  `NO_SUPPORTED_ACTION_DIAGNOSIS`.
- Every card is `SHADOW_ONLY`, `practice_only=true`, `executable=false`, and
  `confidence=LOW`.
- Condition matching and human corner labels remain
  `WAIT_CONDITION_DATA / WAIT_HUMAN_LABELS / CANDIDATE_NOT_GOLDEN`.
- The recording cannot support current-tire-wear, opponent-fuel, traffic,
  trail-braking, line, or curb claims. The report does not fill those gaps.

The current full project suite completed `694 passed in 414.07s`. The focused
card tests completed 15 synthetic passes plus one real Audi/Spa pass; they
cover self-consistent hash tampering, duration/reference closure, diagnosis
allowlisting, recommendation binding, action/loss evidence separation, and
exclusive deterministic output.
