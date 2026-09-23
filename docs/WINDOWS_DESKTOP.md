# Native Windows engineer

The desktop entry is a real Tk/ttk application, packaged as one windowed
`AEIS-Engineer.exe`. It does not open HTML, embed a WebView, run the HTTP server,
install a service or schedule background launches. Legacy browser/report code
remains available separately; it is not the native UI.

## 使用方法

1. 双击 `AEIS-Engineer.exe`。首次解包可能需要几秒；不需要安装 Python。
2. 自己启动 iRacing 并进入车辆。应用只读遥测，不会启动或控制模拟器。
3. **实时燃油与质量**显示连接、完整圈学习进度、油量和可用的燃油估计。
   缺少有效数据时显示等待，不会捏造策略。
4. 停车后，在**工程师问答与复盘**输入问题，或点问燃油、问策略、问驾驶。
   导入功能只接受经过验证的 engineer-session JSON 报告，不是任意遥测文件。
   历史报告与当前赛况分开，历史分析不能作为当前进站指令。
5. 在**模型与本地设置**中填写 DeepSeek API key、确认模型名称，按需勾选
   启用云端问答，然后应用。不要把 key 发到聊天、GitHub 或问题文本中。
   云端默认关闭；没有 key 时仍能提供受证据约束的本地解释。
6. 正常点窗口右上角关闭。它会等待读取器和设置写入真正结束，之后退出。
   慢磁盘/SDK 可能延长等待；未结束时不会显示关闭成功或再启动第二读取器。

当前源码另外区分**采集连接、分析状态、记录状态、近车状态**。分析卡住或
积压时，燃油数字会撤回，但不会连带关闭近车检测；录制队列满时明确提示
“本段不完整”，不会丢帧后仍显示完成。“记录正在收尾”也不代表已关闭。
若重连时上一段录制仍未退出，会显示“本连接未录制”，需要等其退出后再重启
采集；不会不断启动新的后台写入任务。已有 EXE 不会自动获得这些源码更新。

## VR 语音设置（无需在驾驶时打字）

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
这部分当前为源代码实现：既有 EXE 不会自动升级，耳机听感、真实赛况触发和 VR
负载仍需验证。完整边界见 [近车检测与音频说明](PROXIMITY_SPOTTER.md)。

明确选择的设备失联或名称有歧义时会报错，不会擅自切换到另一个麦克风或
扬声器。重新插拔设备后可刷新列表。输入/输出和按键只保存在本机独立的
`voice.json`，不会覆盖 DeepSeek 的加密 key。

语音链路是 **PTT → 本地 Whisper → 问题文字 → 现有工程问答 → Windows 本地
朗读 → 所选耳机**。录音只存在于内存，不保存音频，也不上传原始声音。
常用燃油短句直接在本地回答；启用 DeepSeek 后，需要模型解释的问题文字与筛选后
的工程摘要才会发送到云端。
不要口述密钥或私密身份信息。没听清/静音/超时不会提交问题；识别门槛只是
启发式过滤，不保证内容正确。请用清楚的短句，避免与语音聊天同时讲话。

可选 **低油量提醒** 默认关闭：只有本人真实车内遥测新鲜、油量估计可用且
满足保守直道/无并排车条件时，才播报低油量事实，至少间隔 90 秒。
它不调用 LLM、不决定进站、不替代比赛提醒。主动问答依然是提问时的快照；
失效后撤回并停止尚在播放的回答。实时多停/交通/轮胎/驾驶指导仍未验收。

源码新增本地短问短答：“还有多少油”“当前燃油还能跑几圈”“每圈用多少油”
“油够到终点吗”“还要加多少油”“还要几停”“该进站了吗”。不等待 DeepSeek，
也不消耗模型次数；云端问题未返回时，可重新按 PTT 提问这些短句。旧回答不会覆盖
新答案。当前油量在耗油学习期也可查询，续航仍需完整有效圈；缺油是累计终点预算，
不是下一次进站加油量。没有油箱容量或交通/规则证据时明确说明，不猜次数或最佳
进站圈。详见[本地燃油问答边界](LIVE_FUEL_QUESTIONS.md)。识别和本地朗读仍有耗时，
未实测端到端延迟；这部分尚未重新打包到已有 EXE。

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
the registered window-close protocol. It also checks audio dependency imports,
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
