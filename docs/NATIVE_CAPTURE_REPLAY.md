# Native private capture recomputation

The **采集复盘** tab accepts a completed private `capture-*.jsonl` produced by
the native recorder. This is historical recomputation under the current code,
not current telemetry, original thread scheduling, original voice playback or
source authentication. It does not promote live, strategy or driving acceptance.

## Use

1. Opt into private raw recording before the driving session.
2. Before exiting the simulator, uncheck raw recording in **模型与本地设置**
   and wait for saving to finish. This drains/seals the current recording and
   restarts only the app's read-only connection with recording disabled. Then
   exit the simulator. Alternatively, close AEIS normally before exiting the
   simulator, then reopen AEIS for replay. Simply losing the game connection
   leaves an incomplete prefix and is rejected here. Re-enable recording
   explicitly before the next session if you turned it off.
3. Open **采集复盘 → 复盘原始采集…**. The picker starts in the private capture
   directory next to the desktop settings directory, not the public checkout.
4. Review historical fuel observations/learned burn, repeated corner evidence,
   stint/tire-counter/raw-pace observations and completed pit observations.
   Every card identifies its independent continuous segment and observed lap.
5. Use **取消复盘** to stop. Connecting to the simulator or closing the app also
   cancels; partial results are never promoted to a completed report.

No historical card is copied into the live question service or spoken. No
DeepSeek request, SDK transport, microphone, speaker, simulator launch or control
command is involved. Existing model/device settings and live state are unchanged.
The separate **回放近车诊断日志…** entry remains the detector/software-audio audit;
raw-capture recomputation does not reproduce or certify audible spotter calls.

## Calculation and boundaries

The existing strict `live-collector-v2` validator checks each record, source mode,
schema/session/event order, semantic hashes and terminal receipt. A descriptor-
bound private-file read checks regular-file identity before and after reading,
rejecting public paths, reparse/hardlink aliases and concurrent changes. Input is
capped at 4 GiB, each line at 1 MiB, schema epochs at 128, fields at 4096 (matching
the recorder's full SDK schema rather than just analysis-selected fields), cars at
128 and declared sampling rate at 360 Hz. These are resource caps, not quality
threshold relaxations. No raw file is copied or rewritten.

Frames run through a separate `AppState` and the actual `_LiveAnalysis` owners,
with recorded capture time and exactly update-bound SessionInfo. Accelerated
input waits at corner-worker boundaries; it is not a real-time latency test.
Stale/resumed source, conflicting duplicate, clock regression, schema/session
boundaries and same-update metadata changes start independent analytical
segments. Gaps in the SDK tick sequence still pass through the existing quality
gates. No clock, metadata, clean lap or reference geometry is invented.

Only allowlisted fixed fact templates escape the private owner. Results are
withheld until the entire file and its unchanged identity pass validation.
At most 128 recent cards and four latest category cards are retained; evictions
are counted. Missing facts stay unavailable, and historical "current" means
the time shown on that card. Fuel cards show observed amount and learned burn,
not a reconstructed historical pit instruction or finish plan. The capture
does not contain the user's historical strategy inputs, so capacity, pit loss,
service/rules and rejoin assumptions are not borrowed from today's settings.

Reports are in-memory only, labeled `OFFLINE_REPLAY`, `UNVERIFIED`,
`live_acceptance=false`, with no raw identifiers or metadata. Recomputing an
internally consistent file cannot establish it came from a real simulator.
Corner practice points remain hypotheses; tire pace is not physical wear; pit
elapsed/net-loss observations are not calibrated future service costs.

## Verification

Tests generate invented sealed captures through the real collector and replay
the real numerical owners. Checks cover repeatable fuel/corner/pace evidence,
pit observations, discontinuities, unavailable metadata, incomplete/tampered
files, cancellation, bounded history, path identity and native UI isolation.
The frozen self-test adds an invented temporary capture → validator → pit
observation check, deletes the temporary fixture afterwards and returns only
an aggregate synthetic result. It is not authentic SDK_LIVE evidence.
