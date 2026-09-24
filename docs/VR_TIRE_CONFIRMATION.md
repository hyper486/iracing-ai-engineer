# VR tire-record confirmation

The native PTT lane can now record the driver's tire-service assertion without
removing the headset or clicking the parked-only buttons. This uses the existing
read-only observation mailbox and assertion journal, never an iRacing command,
LLM decision, reviewed service label or calibrated tire-performance model.

## 中文操作

先启用本机语音，并选好输入、输出及 PTT。程序必须连续观测本次进站；在停车位
静止、服务结束且数据新鲜时，按住 F9 或绑定的方向盘按钮说一句，松开：

- 四条都已换新：**记录四胎已换新**。
- 本次完全没换胎：**记录本次没有换胎**。
- 部分更换，或不能确认：**记录部分换胎**（记录为“部分／不确定”，不建立新胎起点）。

听完程序复述并核对内容，再次按住说 **确认记录**，松开。第一句只创建本次进站
的待确认草稿，不改变胎组观测。草稿从创建起最多保留 30 秒；必须用第二次 PTT
明确确认。说 **取消记录** 可以丢弃待确认草稿；它不会撤销已提交的记录。

“已排队”不等于成功。只有当前分析线程返回匹配记录后，才会播报“已记录你的
确认”。未确认结果、过期、移动车辆或服务状态变化时，不会声称保存成功。需要时
说 **轮胎状态** 核对当前观测；四胎换新的起点仍须随后连续观测出站。

这是你对实际服务的陈述，不是要求模拟器换胎。“确认换胎”、疑问句、否定句、
附带其他操作的混合口令都不会作为确认执行。识别为“记录…”但不匹配的内容会
在本地拒绝，不猜测含义、不转发模型。其他普通问题仍走原有问答流程。

## Ownership, cancellation and trust

- Both PTT captures must start and finish recognition in the same fresh,
  owned, stationary, service-complete pit visit. A draft binds generation,
  monitor identity, session/player, owner revision, visit, compound and set.
- The owner increments the command revision on changes to the parked/service-
  complete predicate. A short service restart, movement or missing service
  field invalidates a draft even if recovered before the next UI publication.
  This also rejects an already queued command from an earlier parked episode.
- The native callback atomically checks the original binding again, then queues
  one fresh immutable assertion. Existing next-frame/one-SDK-second admission,
  gap/reset withdrawal, journal privacy and tire-origin rules are unchanged.
- A draft becomes confirmable only after software completion of its readback.
  Muted application volume, interrupted output or output failure cannot arm it.
  Software completion does **not** prove the driver heard or understood it.
- Unrelated questions, rejected recognition, stop, settings/binding changes,
  input errors, proximity preemption and close discard pending review. Proximity
  retains the existing independent urgent-audio lane. No failed command retries
  automatically. Stopping speech after a successful enqueue does not roll back
  that assertion; query the current state if its acknowledgement was interrupted.
- A matching current owner receipt is required for success speech, checked again
  after synthesis and during output. Merely QUEUED/APPLIED status is insufficient.
  The acknowledgement wait is two seconds, never a cloud wait.
- No new permission or consent is saved. Device selection, keys, installed copies
  and simulator settings are unchanged. Audio/transcripts are not written here;
  accepted assertions retain the existing fixed-field private audit when enabled.

## Evidence and remaining acceptance

Fake-PTT tests exercise exact matching, two utterances, real mailbox/owner
receipts, wrong receipts, context changes, callback races, cancellation, output
withdrawal and no provider calls. The frozen check uses invented frames and fake
audio endpoints through the actual voice handler and analysis owner. The explicit
memory-only voice check additionally synthesizes and recognizes the three short
record phrases plus confirm/cancel using the packaged local TTS/STT backend.
Longer alternatives can be misrecognized; approximate spellings are not promoted
to actions merely to make a recognition check pass.

These checks do not establish real microphone accuracy, speaker hearing,
source/service authenticity, physical wear, live model admission, end-to-end
latency or VR acceptance. [The installation boundary](LIVE_TIRE_INSTALLATION.md)
and [manual post-session review](TIRE_SERVICE_REVIEW.md) remain separate.
