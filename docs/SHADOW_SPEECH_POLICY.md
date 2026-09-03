# Shadow speech policy contract

## Status

`shadow-speech-policy-v2` is accepted as a deterministic, advisor-only policy
contract. It is **not** an audio feature and it does not enable speech in an
official race.

The implementation is
[`speech_policy.py`](../src/iracing_ai_engineer/speech_policy.py), frozen for
this receipt at SHA-256
`75b571f6f614814dbb562ecf0b60cdff3e2f511701b632dacbaac3c9d5690c06`.
Its focused regression file is
[`test_speech_policy.py`](../tests/test_speech_policy.py), SHA-256
`09b35e17278b384691ff965f34f9eeec7ad173079a4b8e509adc13a7ef55a92c`.

Every positive decision is only an audit intent:

- `mode=SHADOW_ONLY`;
- `audible=false`;
- `executable=false`;
- no renderer, TTS, audio device, network, simulator, or vehicle-control
  integration exists in this module.

## Accepted behavior

The contract fixes eleven message classes to one versioned template each and
maps them to priorities P0 through P3. Parameters have exact per-class names,
types, ranges, and token allowlists; free text, nested data, executable input,
unknown fields, and non-finite values are rejected.

P0 may bypass the road-safe window and cooldown, but never the stable-quality,
identity, boundary, deadline, or mute gates. P1 and P2 additionally require a
continuous all-safe timing window and cooldown. P3 is lifecycle-log only and
can never produce a speech intent.

Inputs at the same monotonic session timestamp are staged and evaluated as one
canonical batch. This prevents arrival order or chunk boundaries from allowing
a lower-priority candidate to win. A changed candidate must explicitly name
the exact active content revision it supersedes; a delayed old revision cannot
silently restore itself.

`MUTE_ON` and `MUTE_OFF` are receipt-bound inputs. `MUTE_ON` atomically revokes
and clears tactical state. `MUTE_OFF` does not replay an old candidate: a fresh,
lineage-bound `ISSUE` is required. Reset, stale, rejected-quality, dropped-tick,
identity mismatch, time regression, conflicting same-time input, and excessive
timing-sample gaps all fail closed.

V2 adds a receipt-bound `SpeechRefresh` heartbeat. A refresh carries no message
content and may update only the evidence and exclusive session-time deadline of
an active envelope. It must compare-and-swap both the exact active content
revision and the previous full-envelope SHA-256. A delayed, replayed, expired,
revoked, reset, or post-mute refresh therefore cannot revive or silently replace
a plan. Equal-time processing is fixed as mute, revoke, refresh, issue, timing,
then boundary; the final boundary still fails closed.

The finite-run API now returns immutable canonical input records and exact final
active-envelope snapshots in addition to lifecycle events, decisions, and the
receipt. Every record and snapshot has an independently recomputable digest.
`replay_speech_policy()` reconstructs the typed inputs and requires the entire
run to reproduce exactly. V1 persisted objects are rejected with an explicit
contract-version mismatch instead of being silently interpreted as V2.

## Verification receipt

- Focused suite: `59 passed`.
- Adjacent shadow, capability, and event regression: `84 passed`.
- Independent review: `NO_BLOCKER`.
- Independent same-timestamp attack: all `6! = 720` input permutations were
  exercised in batch and item-by-item form (`1,440` runs); lifecycle events,
  decisions, and receipts were identical.
- Independent stale-revision attack: `A -> B -> delayed A` was rejected unless
  the final input explicitly superseded the active B revision.
- Independent timing-gap checks: the exact configured boundary is retained;
  boundary plus one microsecond resets the continuous window.
- Refresh CAS, exclusive-deadline expiry, canonical-record replay, final-active
  snapshots, receipt digests, lifecycle/decision flags, per-class schemas, and
  two `PYTHONHASHSEED` executions were independently rechecked.

## Remaining acceptance gates

This closes only the policy-contract slice of M4. The following remain `WAIT`:

- integration with a reviewed structured Recommendation producer;
- a renderer and local audio/TTS implementation;
- real AI or Hosted-session timing evidence;
- manual cognitive-load, false-prompt, and contradictory-prompt review;
- explicit user acceptance of the mute control in the actual race UI; and
- any official-race speech enablement.

Until those gates pass, the correct product state is
`PASS_SHADOW_POLICY_CONTRACT / WAIT_AUDIO_AND_SESSION_REVIEW`.
