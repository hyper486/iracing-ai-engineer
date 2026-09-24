# Native Windows engineer

The desktop entry is a real Tk/ttk application, packaged as one windowed
`AEIS-Engineer.exe`. It does not open HTML, embed a WebView, run the HTTP server,
install a service or schedule background launches. Legacy browser/report code
remains available separately; it is not the native UI.

The 2026-09-24 local trial build includes the proximity, local question,
recent-corner, observed-stint/raw-pace, conditional fuel-stop and private replay slices below. See
[the integrated trial checklist](NATIVE_TRIAL_BUILD.md). Installed older copies
are not automatically replaced; run the newly built `dist/AEIS-Engineer.exe`.

## 使用方法

1. 双击 `AEIS-Engineer.exe`。首次解包可能需要几秒；不需要安装 Python。
2. 自己启动 iRacing 并进入车辆。应用只读遥测，不会启动或控制模拟器。
3. **实时燃油与质量**显示连接、完整圈学习进度、油量和可用的燃油估计。
   缺少有效数据时显示等待，不会捏造策略。
4. 停车后，在**工程师问答与复盘**输入问题，或点问燃油、问策略、问交通、问驾驶。
   导入功能只接受经过验证的 engineer-session JSON 报告，不是任意遥测文件。
   历史报告与当前赛况分开，历史分析不能作为当前进站指令。
5. 在**模型与本地设置**中填写 DeepSeek API key、确认模型名称，按需勾选
   启用云端问答，然后应用。不要把 key 发到聊天、GitHub 或问题文本中。
   云端默认关闭；没有 key 时仍能提供受证据约束的本地解释。
6. 正常点窗口右上角关闭。它会等待读取器和设置写入真正结束，之后退出。
   慢磁盘/SDK 可能延长等待；未结束时不会显示关闭成功或再启动第二读取器。

当前试用版另外区分**采集连接、分析状态、记录状态、近车状态**。分析卡住或
积压时，燃油数字会撤回，但不会连带关闭近车检测；录制队列满时明确提示
“本段不完整”，不会丢帧后仍显示完成。“记录正在收尾”也不代表已关闭。
若重连时上一段录制仍未退出，会显示“本连接未录制”，需要等其退出后再重启
采集；不会不断启动新的后台写入任务。已有 EXE 不会自动获得这些源码更新。
新增的交通观测行独立显示前后车纵向距离、缺少匹配赛道长度或分析故障；不需要
等燃油学习完成，也不等同于左右近车 Spotter。没有可定位对手不代表赛道清空。
驾驶分析行独立显示近期可比圈与重复证据。**问驾驶** 或 PTT 问
**哪里可以改进？** 会在本地回答一个有依据的弯道练习假设；不等待 DeepSeek。
至少需三个完整可比圈来寻找参考，且要有两圈重复模式，圈数够了也不保证出建议。
进站、事故或数据中断会撤回旧结论；缺少遥测会说明原因。自动分段编号不是正式
弯名，观测损失不是保证能追回的时间。见 [实时驾驶说明](LIVE_DRIVING_COACHING.md)。

试用版还包含**进站比较**状态行、**比较补油**按钮及本地策略参数。停车进车后，在
**模型与本地设置**确认本场允许的有效油箱容量；加油速率、进站通道损失上下限
可选，通道损失指相对继续跑赛道的时间损失，不含驻站服务。参数只对本次连接
有效，不自动保存或重用；换会话、退车或断线后需重新确认。应用不重启采集。
按 PTT 问**比较进站方案**或**这次进站加多少油**，可得到条件化的完整圈补油预算，
窗口列出早晚边界、下一停补油量及下一段圈数；语音只说简要预算，不等 DeepSeek。
“几整圈后”从提问位置起算，未定位进站口；这不是最佳进站圈、换胎或回场预测。
**还要加多少油**仍回答终点累计缺油，不是下一停加油量。
见 [进站比较说明](LIVE_FUEL_STOP_COMPARISON.md)。旧安装需改用新构建的 EXE。

新增 **问换胎耗时**，或 PTT 问 **换胎会多花多久**：用手填加油速率、四轮换胎耗时
及 **并行／串行** 作业方式，比较仅补油与补油加换胎的时间，不等待 DeepSeek。
若还确认通道净损失及 **其他开销** 上下限，可把完整分项成本带入 **出站预测**，
分别查看两种服务方案的前后车范围。其他开销须覆盖全部额外且不重叠的驻站时间；
没有也要填 `0 / 0`，留空不是零。固定完整总损失仍可用，但须清空其他开销，
且不区分换胎方案。两种模式不能混填。**这不是轮胎健康、是否该换胎或留胎安全的判断。**
具体条件见 [服务比较说明](LIVE_SERVICE_COMPARISON.md)。

**问本段／问轮胎／问配速** 同样走本地快速问答，也可按 PTT 说
**这一段跑了多久／轮胎怎么样／配速变化**。本段状态行区分“观测出站后”和
“中途接入的已观察区间”；只加油出站不会被当成换了新胎。轮胎计数连续不变的
时长不是完整胎龄，六个连续干净可比圈的原始中位配速变化也不等于胎耗。
回答会说明油量变化及尚未做油重修正，不会据此给出轮胎寿命或换胎指令。
见 [本段与配速说明](LIVE_STINT_PACE.md)。有已验证校准和确认胎组时，新增的条件
收益比较如下；真实校准和完整比赛策略验收仍待补齐。

## 条件换胎收益

停车后，在 **模型与本地设置** 输入独立保留的校准请求 SHA-256，再点
**停车载入校准** 选择私有训练／留出验证请求。只有车型、设置、配方和当前干地
天气匹配，才继续检查确认后的胎组计圈、燃油方案和换胎作业耗时。
按 PTT 说 **比较换胎收益** 或 **换胎值得吗**，也可停车点 **问换胎收益**：
本地回答下一段的模型时间收益、额外换胎耗时和净收益区间，不等 DeepSeek。
未就绪会说明缺哪项；不会用未经校准的配速变化编造数字。

**这只是天气和条件不变的整圈预算比较，不是磨损读数或留胎安全判断，也没有
合并映射进站口、出站交通和赛事规则。** 老安装不会自动更新；详见
[计算及限制](LIVE_TIRE_COMPARISON.md) 和 [校准载入](LIVE_TIRE_CALIBRATION.md)。

## 综合进站简报

另外可停车点 **综合进站**，或按 PTT 问 **综合进站方案**：把同一映射入口的补油、
仅加油／加油换四胎的各自出站交通，以及有证据的完整圈轮胎收益放在一起。
需要已确认的进出站位置、服务参数及双方圈历史；没有轮胎校准仍可报告油量和交通。
首尾片段不按距离硬凑收益，交通不确定会明确说明；**这不是整段净收益或最佳方案排名**。
语音先给简短补油／作业预算；可以分别问 **综合换胎收益**、**综合仅加油交通**、
**综合换胎交通**，听首个预算方案的分项结果。每次取新的当前快照，不锁定旧方案；
不延长十秒有效期来朗读长段落。完整文字保留两个方案、细化区间和全部限制。
详细前提见 [综合进站说明](LIVE_PIT_BRIEFING.md)。

## 轮胎安装确认

**模型与本地设置** 顶部新增轮胎安装确认：程序须连续观测进站，车辆停在车位且
服务结束后，选择四胎全换新、本次未换胎或部分／未知。按钮只记录人工确认，
不会替你换胎。出站后问 **轮胎怎么样** 会说明“按车手确认，计圈增加几次”；
断线、缺失数据、未确认的下一次进站等会撤回起点。它不是磨损或换胎收益模型。
确认可停车点原生按钮，或用下节的两次 PTT 复述确认；驾驶中不接受安装确认。
详见 [安装确认与限制](LIVE_TIRE_INSTALLATION.md)。

赛后可在 **采集复盘 → 复盘采集＋换胎确认…** 依次选择完整原始采集和同次完整
诊断日志。新日志按文件哈希及精确帧绑定，重算人工确认后的计圈、进站暂停和
失效原因；旧日志缺少绑定时明确拒绝，不补猜。结果仅供历史复盘，不进入当前
语音或策略，也不是换胎真实性或磨损证明。见 [配对回放说明](TIRE_CAPTURE_REPLAY.md)。

需要建立离线轮胎服务标签时，在同一页先 **准备换胎审核…**，等待核对后点击
**审核并导出…**，逐次选择实际服务类型或“未审核”，引用保留的本机证据，再明确
勾选人工声明。导出只到私有 `tire-reviews` 目录，不上传、不改变实时策略；记录
仍是人工自述而非已认证服务。详见 [赛后审核说明](TIRE_SERVICE_REVIEW.md)。

## VR 语音设置（无需在驾驶时打字）

换胎记录也可在停车位服务结束后用 PTT：说 **记录四胎已换新**、
**记录本次没有换胎** 或 **记录部分换胎**，听完复述后再次按键说 **确认记录**。
第一句不会修改观测；等待“已记录你的确认”，不要把排队当作成功。
说 **取消记录** 仅取消未提交的草稿。详见 [VR 换胎确认](VR_TIRE_CONFIRMATION.md)。

停车时打开第四个页签 **语音与 VR**：

1. 输入、输出分别选择麦克风和耳机；保持 **系统默认** 则在每次录播前重新
   读取 Windows 当前默认设备。不会替你更改 Windows 默认音频设置。
2. 勾选启用按住说话并应用。PTT 默认关闭，不会开机常驻监听；默认语言为中文。
   选择合适的本机朗读声音和音量，用 **试听** 确认耳机路由。
3. 默认按住 **F9**，听到短提示音后说话，松开提交，最长 12 秒。
   超过上限仍未松开时会取消本次收音，不会自动发送被截断的问题。
   也可选 F8–F12，或点 **绑定方向盘按钮**，按下再松开一个按钮完成绑定。
   绑定只读取按钮，不改变方向盘、游戏配置或发送任何按键。
4. 游戏在前台时，程序在后台监听已选 PTT 键/按钮，不依赖 Tk 窗口焦点。
   朗读中再次按住 PTT 会打断旧回答；**停止** 或关闭语音会取消当前操作。
   不支持唤醒词或一直开麦。若独占全屏/权限隔离导致收不到键，停车后测试
   方向盘绑定；不要通过关闭安全保护来解决。

新增的 **启用近车语音** 是独立开关：无需启用麦克风、Whisper 或 DeepSeek。
它先准备本地短语音，在本人驾驶、近车字段新鲜时播报左右/两侧占位与确认清空。
准备、数据等待、输出故障分别显示；“已调用播放”不代表你确实听到了。
近车提示可以打断长回答和正在录制的问题。被打断的录音会丢弃，不发送残缺问题；
两侧持续清空后会提醒重新说。**停止播报** 会暂停近车语音，重新应用设置才能恢复。
音量为零时明确显示暂停。普通 PTT 可以打断取消提示，但不能抢断近车提示。

旧 `native-voice-v1` 设置升级到 v2 时保留原设备、按键和音量，新增近车开关保持关闭。
这部分已打包进当前本机试用版；既有 EXE 不会自动升级，耳机听感、真实赛况触发和 VR
负载仍需验证。完整边界见 [近车检测与音频说明](PROXIMITY_SPOTTER.md)。

明确选择的设备失联或名称有歧义时会报错，不会擅自切换到另一个麦克风或
扬声器。重新插拔设备后可刷新列表。输入/输出和按键只保存在本机独立的
`voice.json`，不会覆盖 DeepSeek 的加密 key。

语音链路是 **PTT → 本地 Whisper → 问题文字 → 现有工程问答 → Windows 本地
朗读 → 所选耳机**。录音只存在于内存，不保存音频，也不上传原始声音。
常用燃油/交通/驾驶短句直接在本地回答；启用 DeepSeek 后，需要模型解释的问题文字与筛选后
的工程摘要才会发送到云端。
不要口述密钥或私密身份信息。没听清/静音/超时不会提交问题；识别门槛只是
启发式过滤，不保证内容正确。请用清楚的短句，避免与语音聊天同时讲话。

可选 **低油量提醒** 默认关闭：只有本人真实车内遥测新鲜、油量估计可用且
满足保守直道/无并排车条件时，才播报低油量事实，至少间隔 90 秒。
它不调用 LLM、不决定进站、不替代比赛提醒。主动问答依然是提问时的快照；
失效后撤回并停止尚在播放的回答。实时多停/交通/轮胎/驾驶指导仍未验收。

本地短问短答包括：“还有多少油”“当前燃油还能跑几圈”“每圈用多少油”
“油够到终点吗”“还要加多少油”“还要几停”“该进站了吗”。不等待 DeepSeek，
也不消耗模型次数；云端问题未返回时，可重新按 PTT 提问这些短句。旧回答不会覆盖
新答案。当前油量在耗油学习期也可查询，续航仍需完整有效圈；缺油是累计终点预算，
不是下一次进站加油量。没有油箱容量或交通/规则证据时明确说明，不猜次数或最佳
进站圈。详见[本地燃油问答边界](LIVE_FUEL_QUESTIONS.md)。识别和本地朗读仍有耗时，
未实测端到端延迟；请使用新试用版，旧 EXE 不会自动升级。

交通短句包括“前后车情况”“前车多远”“后车多远”“现在允许进站吗”。回答的是
提问时可用车辆的沿赛道距离，不是秒差或未来出站车距；超过一公里的语音用约数，
窗口保留米数。进站许可/旗号只是当前观测，不代替赛事规则，也不能单独决定进站。
这类回答最多保留 10 秒；车况或来源变化可提前撤回并停止播报。普通燃油问答仍保留
原有 30 秒上限。详见[交通问答边界](LIVE_TRAFFIC_QUESTIONS.md)。

本地识别固定使用公开 Whisper medium 多语言模型，CPU int8、四个推理线程，
不使用 VR 的 GPU；这不代表已验证对 VR 帧率无影响。识别放在可终止的本机
子进程，有超时与退出回收，并尝试降低自身进程优先级。首次识别需要加载模型。
模型约 1.53 GB，因此单文件 EXE 较大。Windows 中文 TTS 语音
需已安装；缺失时显示不可用，不偷偷改用云端语音服务。

## Local privacy and lifecycle

- Nonsecret preferences live at
  `%LOCALAPPDATA%\iRacingAIEngineer\desktop\settings.json`.
- The masked key box never prefills an existing key. Leaving it empty retains
  the current process key. An existing `DEEPSEEK_API_KEY` environment variable
  is also supported; the application never prints its value.
- Optional **将密钥加密保存在本机 Windows 账户下** uses Windows current-user
  DPAPI in `deepseek-key.dpapi`. Unchecking and applying deletes only this app's
  saved encrypted copy; the current process can still use its in-memory key.
  Close the app to discard that process key. Environment keys remain external.
- DPAPI is not a defense against malicious processes running as the same user.
  Python strings cannot promise secure memory erasure. Nonsecret settings and
  the encrypted key are individually atomic files, not a two-file transaction.
- A failed key decryption preserves valid nonsecret preferences, especially a
  previously disabled recording choice. A failed settings load fails closed.
- Private raw recording defaults on, under
  `%LOCALAPPDATA%\iRacingAIEngineer\captures`, with a total 4 GiB budget per
  application run. It is never uploaded to DeepSeek. Keep raw captures private.
  The record checkbox can disable it; toggling safely reconnects the single
  reader and resets fuel learning. Settings/key storage and capture destinations
  reject links and repository paths, including in the frozen executable.
- The same recording preference now starts a separate private proximity/audio
  journal in `trials`, bounded to 1 GiB per application run. It saves fixed
  numeric/enum diagnostics, not mic audio, recognized text, names or credentials.
  Its health is shown separately; a logging fault does not stop proximity.
  **模型与本地设置 → 回放近车诊断日志…** silently recomputes completed intervals
  in the background. See [the trial replay guide](PRIVATE_TRIAL_REPLAY.md) for
  incomplete prefixes, optional capture-byte checks and hearing limitations.
- Close a separately running legacy browser worker before switching to the
  EXE, to avoid two independent recorders. The native instance lock prevents a
  second native window in the same login session, not a separate CLI process.
- Model changes, report validation and saving settings run in a worker; initial
  bounded settings reads and DPAPI decryption occur during startup. A slow local
  profile can delay initial display. No request or capture is made by self-test.

Cloud requests send only the question and allowlisted engineering summaries.
Do not put identity or secrets in the question. The model selects supported
fact IDs and local code renders the answer, numbers and mandatory limitations.
Defaults remain 60 attempted provider calls per app run, at least 10 seconds
between general questions, and a bounded provider timeout. Routine live fuel
questions have a separate one-second guard and use no provider budget. Changing model or
loading history does not reset the run's request count. Provider errors fall
back locally without interrupting telemetry. See [DeepSeek details](DEEPSEEK_ENGINEER.md).
Closure waits for the answer-planning service to finish. As documented by the
transport, an OS/DNS call that outlives its deadline may still be unwinding in
its bounded daemon I/O thread; closing the process does not keep that worker
running as a separate background service.

## Build and check

Build on Windows x64 with `uv`. Before the first build, explicitly download the
public speech model, then build (the build selects managed Python 3.12/Tcl/Tk):

```powershell
uv run python scripts/fetch_voice_model.py --download
uv run python scripts/fetch_voice_model.py
.\scripts\build_desktop.ps1
```

The downloader uses anonymous Hugging Face CLI access, an exact commit and four
SHA-256 checks from `packaging/voice-model.json`. The model is ignored by Git;
the build bundles only the four allowlisted model files, manifest and license.
Running the app never downloads models. The model comes from
[Systran's CTranslate2 conversion](https://huggingface.co/Systran/faster-whisper-medium)
of Whisper medium under the included MIT license.

The script selects uv-managed CPython 3.12 (including Tcl/Tk), rejects a Conda
base environment and clears PyInstaller's analysis cache. An older Conda C++
runtime caused native speech-model initialization to crash during development;
using the independent runtime fixed the same model load without changing system
DLLs or weakening runtime checks.

The script uses the locked `desktop-build` group (PyInstaller 6.22.3) and creates
ignored `build/desktop/` and `dist/` output. It uses one-file, no-console, no-UPX
packaging and requests no administrator privileges. Target machines need
neither Python nor `uv`. The first launch extracts bundled libraries to a
temporary directory. The binary is unsigned; verify its origin and hash before
running. Do not disable antivirus or Windows security protections.

Packaging behavior follows the [PyInstaller usage guide](https://pyinstaller.org/en/stable/usage.html).
Key persistence uses [Windows DPAPI](https://learn.microsoft.com/en-us/windows/win32/api/dpapi/nf-dpapi-cryptprotectdata)
without the machine-wide protection flag.

`dist/AEIS-Engineer.build.json` records the binary SHA-256, size, packaging and
self-test status. It is a local build receipt, not a signature or live acceptance.
No credential, user configuration, raw telemetry or private receipt is included
by the packaging specification. Only source and build instructions are pushed;
the binary is not committed into Git history.

The default build executes the frozen self-test with a system-only PATH and
Python/Tcl/Conda/provider environment overrides removed. It checks bundled Tk,
an SDK import without startup, a real local-answer worker, worker closure and
the registered window-close protocol. Invented frames also exercise proximity,
learned fuel, repeated-corner evidence and local fuel/coaching/stop answers;
all five numerical check IDs are required by the build. It checks audio
dependency imports,
the exact model hashes and in-memory Chinese TTS-to-STT. No microphone stream
or speaker is opened. Receipt provenance remains `SYNTHETIC`,
with `sdk_accessed=false`, `provider_called=false`, `live_acceptance=false`.

Explicit diagnostic modes (neither uses saved credentials, SDK or cloud):

```powershell
.\dist\AEIS-Engineer.exe --self-test --voice-self-test --self-test-output "$env:TEMP\aeis-new-self-test.json"
.\dist\AEIS-Engineer.exe --ui-smoke-seconds 30
```

Self-test output must be a new file; existing files are never overwritten.
`--ui-smoke-seconds` displays a labeled synthetic window and closes it after
the requested duration. Do not interpret it as connected driving telemetry.

## Acceptance boundary

Native packaging does not change product acceptance. Live multi-stop strategy,
traffic/tire decisions, repeated-corner coaching and reliable racing audio
remain unaccepted. Fuel learning needs an initial observed crossing and at
least five valid full laps by default. Real DeepSeek account/network behavior
and authentic in-car use still need separate tests. No synthetic, replay,
spectator or packaging check is relabeled as accepted `SDK_LIVE` driving.

The executable never sends vehicle, camera, simulator-launch or pit-black-box
control commands. An unavailable estimate is deliberately shown as unavailable.
