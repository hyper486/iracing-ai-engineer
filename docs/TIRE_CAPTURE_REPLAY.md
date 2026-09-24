# Paired capture and tire-confirmation recomputation

The native **采集复盘 → 复盘采集＋换胎确认…** action accepts one completed raw
capture and one completed private trial journal. It recomputes the historical
driver-confirmed tire counter under the current live owner's rules. It does
not independently verify service contents, wear, original scheduling or source
authenticity, and does not enter live questions, voice or strategy.

## Use

1. Enable the existing private recording option before the session. New
   confirmations use `private-trial-audit-v3` and the same parked native
   [installation-confirmation workflow](LIVE_TIRE_INSTALLATION.md).
2. Finish/seal recording normally, before losing the simulator connection.
   Wait for both capture and journal to finish, then perform replay offline.
3. Select the capture, then its trial journal. Cancelling either picker starts
   no replay. Filenames do not prove a match: a renamed exact copy is accepted,
   while a similarly named different file is rejected.
4. Review accepted/rejected assertion counts, historical tire-counter cards
   and the state-change list. It shows confirmed counters, suspension in pit
   road and withdrawal reasons, including unconfirmed service and continuity
   changes. Counters are crossings since the observed exit, not complete
   geometric laps since installation.

An unmatched, ambiguous, truncated or changed pair returns no partial tire
result. Assertions outside a prematurely sealed capture also fail the complete
pair requirement; raw-only replay remains available. Older v1/v2 journals can
still be audited on their own. A v2 assertion lacks an exact frame anchor and
is explicitly refused for paired tire recomputation; missing anchors are never
invented. Legacy journals with no assertions can return an explicit empty tire
history after the capture bytes match.

## Binding and validation

The complete journal is first checked by the existing strict reader: schema,
sequence, detector recomputation, assertion projection, seal and private-file
identity. An internal bounded visitor retains no more than 128 completed
capture links and 1,024 assertions. Exceeding either cap fails, not truncates.
Unsealed visitor data is never exposed as a report.

The selected raw file is read through the existing guarded descriptor. Its
full byte SHA-256 and size must match exactly one completed capture link in
the journal. This selects a connection generation without trusting its filename.
The descriptor is rewound, then the complete collector contract, semantic
receipt and unchanged file identity are verified again during recomputation.
A second byte hash must equal the first; cancellation is checked on both passes.

Each v3 assertion carries an integer capture clock and a domain-separated
SHA-256 of a fixed scalar frame projection: the capture clock, buffer tick,
SessionInfo update, source mode, read errors and fourteen fixed session/player,
lap, tire and stationary-service fields. Floating negative zero follows the
collector's canonicalization. No arbitrary SDK dictionary/text is journaled.
The accepted assertion is queued under the same generation lock as resets,
preventing an old confirmation from appearing after a new connection reset.

Every selected assertion must locate exactly one captured frame. The existing
`LiveTireAgeTracker` is fed first; the assertion is then rechecked at that exact
decision frame against its visit, request tick, counters and stationary state.
Its original UI revision is replaced by the current replay owner's revision,
and such substitutions are counted. This is explicitly **current-code
recomputation**, not reproduction of original thread timing or UI admission.
Current-owner rejection is visible and withdraws any older origin, rather than
leaving an old set in force past an unadmitted recorded service.

The original independent capture-segment rules still apply. An unchanged-service
assertion cannot restore an origin lost at a discontinuity. Partial, unknown or
unconfirmed subsequent service withdraws it. The in-memory report retains at
most 128 state changes, with eviction counts, alongside the existing bounded
historical cards. Only fixed vocabulary and bounded numbers leave the owner;
no frame hashes, raw clocks, player slots, paths or source metadata are exported
in the capture report. The assertion-only journal report remains a distinct
private diagnostic and does not itself recompute tire age.

## Acceptance boundary

Synthetic paired tests and the frozen self-test use the real recorder, journal,
analysis owner, confirmation mailbox and private reader. They cover unchanged
and partial service, wrong hashes/clocks, duplicates, missing/legacy assertions,
rejection, cancellation, bounds and repeatability. They do not establish that
the telemetry came from iRacing or that tires were actually changed.

Independently reviewed service-label export, event-matched live performance-model
admission, calibrated benefit-versus-service/rejoin decisions and real driver/VR
acceptance remain open. This feature does not weaken those evidence gates.
