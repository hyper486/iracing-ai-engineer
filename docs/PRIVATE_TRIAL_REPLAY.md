# Private proximity / audio trial replay

Source implementation, not an updated EXE or a live acceptance receipt.
Contract: `private-trial-audit-v1`. This is the proximity slice of Stage E;
complete endurance strategy, device hearing and VR acceptance remain open.

## 使用方法

1. 使用包含本次源码更新的原生程序，在 **模型与本地设置** 保留或开启
   **将原始遥测及近车诊断日志记录到本机**。既有关闭记录的偏好保持关闭。
   此开关不会启用麦克风、语音、云端或启动游戏。
2. 自己进行练习。近车判定、音频准备/故障和诊断记录状态分开显示。
   无须在驾驶中输入文字，也不要为测试故意制造危险并排、事故或断线。
3. 结束驾驶后，取消本机记录并等待收尾，或正常关闭应用。
   在 **模型与本地设置 → 回放近车诊断日志…** 选择已结束的一段。
   文件选择器默认定位本机诊断目录。
4. 查看是否有输入、判定候选、播放尝试、软件输出或故障记录。
   “重算一致”只表示记录中的近车输入能重现判定，**不代表 Spotter 已经通过实车验收**。

Default private locations:

- Journal: `%LOCALAPPDATA%\iRacingAIEngineer\trials\trial-<random-id>.jsonl`
- Companion capture: `%LOCALAPPDATA%\iRacingAIEngineer\captures\capture-<random-id>.jsonl`

Both stay private, outside the public checkout. Neither goes to DeepSeek or
GitHub. There is no automatic deletion or rotation of old runs. Existing EXEs
do not automatically acquire this feature. The legacy browser entry does not
start this native trial journal.

## What is recorded

The existing raw capture remains separate. The small journal has three lanes:

| Lane | Fixed projection | Purpose |
|---|---|---|
| Detector | Reset/configuration, connection generation, ten proximity fields, capture/observation clocks, fixed decision rows | Recompute each frame, unavailable transition and time-driven withdrawal |
| Audio | Fixed health/outcome codes, generation/epoch/candidate ID, software start delay, default/selected/unset output choice | Correlate attempts, starts, completions, cancellation, drops and failures |
| Capture | Random local capture ID, connection generation, committed size and hash, open/complete/empty/incomplete state | Locate and optionally verify companion file bytes |

No driver name, `SessionInfo`, microphone PCM, recognized question, model answer,
key, device/voice name or arbitrary exception text enters the journal. Fixed
keys, enums and bounded numeric fields are checked before enqueueing. Invalid
SDK field types are projected to null only when that preserves detector
admission semantics; an unrepresentable numeric trace fails journaling rather
than silently asserting replay equivalence. These records still contain private
session timing and car-slot context and must not be published as sanitized data.

Detector records are captured under the same lock as the decision. The first
clock observation and every state-changing poll are retained. Silent intervening
polls are represented by the next operation's prior high-water clock. Replay
must advance that clock without emitting an omitted event, then reproduce the
recorded operation's exact decision rows and candidate count. This includes
timeouts, duplicates, invalid data, clock regression and reconnects, not only
raw-frame input. Existing detector audit bytes and rules are unchanged.

The audio trace uses the real native consumer's callbacks. A playback exception
is bound to its candidate separately from a global audio-service failure. This
is a record of software actions, **not a simulated rerun of Windows audio or
its scheduling**, and not a recording of what the driver heard.

## Bounds and lifecycle

- One separate file-owner worker per enabled recording interval; all file
  setup/write/hash/finalization/close work stays off SDK, audio and Tk owners.
  Producer projection/validation/copying still uses bounded Python CPU work.
- At most 4,096 queued/active entries and 32 MiB of conservative retained-object
  accounting; each payload is at most 64 KiB by that accounting. This is not a
  process-RSS limit or a measured VR performance guarantee.
- A separate **1 GiB per application-run** journal budget, shared across normal
  recording toggles; the existing **4 GiB** raw-capture budget is separate.
  Recording must be enabled and its destination admissible to start a journal.
  Raw capture can hit its own limit while an already-started journal continues.
- Queue overflow, malformed projections, file limit and I/O failure latch only
  diagnostics; proximity detection/audio are not stopped by a logging failure.
  Failed journals do not restart via repeated recording toggles in that process.
  Restart the application to retry. This also prevents short failed writes from
  escaping the budget through committed-only byte accounting.
- Orderly journal shutdown drains accepted work and writes a count/hash footer.
  The header is not a seal. Overflow or write failure leaves a prefix without
  an intentional successful footer. File-limit status is explicit. A crash may
  lose the last OS-buffered bytes; per-entry fsync is deliberately disabled.
  Finalization/close requests fsync. The observed terminal bytes do not certify
  physical durability or an error-free OS close.
- Closing or switching recording waits for actual owner exit on a background
  lifecycle thread. No replacement writer is started while its predecessor is
  stuck. The window can remain in closing state rather than claim false closure.

The journal footer describes the recorded interval, not the rest of the race,
raw-capture completeness, or hardware success. Before the first detector reset,
a waiting app may record audio health but has no detector input to reproduce.
Changing settings or disabling recording can leave audio receipts belonging to
an earlier unrecorded segment; those remain explicitly unbound.

## Offline inspection

The native button performs silent local validation in a background job. It
summarizes fixed counts/reasons and **does not** open companion raw captures.
Closing the app cancels replay between bounded reads; a blocked OS read still
requires actual thread exit. For optional byte-link verification from source:

```powershell
$trialLog = 'C:\Users\racer\AppData\Local\iRacingAIEngineer\trials\trial-<id>.jsonl'
$captureDir = 'C:\Users\racer\AppData\Local\iRacingAIEngineer\captures'
uv run python -m iracing_ai_engineer.trial_replay $trialLog --capture-directory $captureDir
```

The CLI writes an aggregate report and bounded tails to stdout; keep redirected
output private too. It never opens the SDK, microphone, speaker or provider.
Exit 0 means detector replay matched a sealed interval; exit 1 means incomplete
or no detector input; exit 2 means rejected/cancelled. Exit 0 is not a racing or
audio acceptance gate and does not override a reported companion-file mismatch.

Input is untrusted: strict UTF-8 JSONL, duplicate-key/type/shape/enum checks,
sequence continuity, 32 KiB physical lines, 1 GiB file limit, terminal footer
and stream hash verification, and exact detector recomputation. Plain regular,
singly linked files outside project destinations are required. Descriptor/path
identity and size/time checks detect replacement or mutation during replay.
This is not a defense against an adversarial same-user process forging a whole
consistent journal. Hashes provide consistency checks, not source authentication.

An absent footer or final partial write yields `INCOMPLETE_PREFIX`; malformed
complete records, mismatched hashes or decision differences are rejected. A
runtime detector fault is reported rather than claimed reproducible. Empty
intervals report `NO_DETECTOR_DATA`.

All returned reports remain `source_kind=OFFLINE_REPLAY`,
`source_authenticity=UNVERIFIED`, `heard=false`, `audio_played=false` and
`live_acceptance=false`. Capture checks compare size/hash only; even `MATCH`
does not admit that file as authentic `SDK_LIVE` or recompute its fuel/coaching
analysis. Use the separate existing capture-admission pipeline for those gates.

## Reading the results

- `DETECTED_NO_RECORDED_ATTEMPT`: a candidate exists but no matching attempt
  was recorded. Recorded disabled/preparing/muted state may help explain it;
  missing audio history does **not** prove why it was silent.
- `ATTEMPTED_NOT_STARTED`: a recorded attempt lacks a start. Inspect pre-start
  drop/deadline and detector supersession/expiry/invalidation observations.
- `PLAYBACK_ERROR`: that candidate's playback call raised a fixed-code failure.
- `SOFTWARE_COMPLETED_NOT_HEARING_CONFIRMED`: start and completion were reported
  by software, not confirmed by a listener. Cancellation and starts without a
  terminal receipt have separate results.

Counters cover the streamed interval; the displayed event/capture tails hold
128 records each. Correlation keeps at most 2,048 candidate IDs. Old entries
are summarized, evictions counted, and very late/orphaned receipts stay unbound
rather than being attached to the wrong event. Counts and the tail need not
contain the same number of events. Health counts are transition/receipt counts,
not time spent in each state. `start_delay_ms` begins at the audio attempt,
not the SDK observation or the first audible sample.

Invented-frame, real-reader/fake-transport, fake-audio and hidden-native-window
tests cover this path. No current user capture, selected-device playback, game,
VR load, installed EXE, microphone or provider account was used to accept it.
