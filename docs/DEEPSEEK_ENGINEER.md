# DeepSeek 工程师框架

这是可运行的受约束 LLM 接线版本，不是已通过真实耐力赛验收的完整产品。
模型用于理解问题、选择相关工程证据；本地程序负责计算、校验、生成最终文字。
**模型不能自行编数字、补全不存在的策略、发控制命令或进入比赛语音。**

## 数据如何流动

```text
只读 SDK → 归一化 / 质量门槛 → 实时燃油估计 → 允许外发的事实摘要
                                                ↓
文字问题 → 单任务后台队列 → DeepSeek 选择事实 ID → 本地校验和文字生成 → 页面
                                                ↑
已验证的 engineer-session-v1 → 独立的历史复盘摘要（不是当前赛况）
```

SDK 线程不等待模型。页面每秒读取问答状态，不会自动提交问题或持续调用模型。
实时燃油数字和文字问答各自更新；文字回答基于**提问时**快照，不是滚动进站指令。
超出 30 秒、圈次变化、会话/身份边界变化、加油、证据失效或断线时，旧实时回答撤回。
正常油量递减、显示序号递增不会令每份回答在半秒内失效。

## 已接入与尚未接入

| 入口 | 本版本支持 | 不应据此声称 |
|---|---|---|
| 实时燃油问答 | 学习状态、已有合格圈、保守油耗/续航与受限终点需油解释 | 完整进站决策或保证能跑完 |
| 实时策略/驾驶提问 | 明确指出当前缺少的证据 | 实时多停优化、轮胎服务、交通回场或驾驶教学已完成 |
| 历史复盘 | 验证完整 receipt，再投影既有策略候选、重复弯道损失、轮胎性能区间及未过门槛 | 旧数据适用于当前比赛，或自哈希证明原始遥测真实 |
| DeepSeek | 理解问题，选择允许的事实/限制 ID | 任意自由回答、联网检索、工具执行或自主策略计算 |
| 降级 | 无 key、关闭云端、超时、错误、非法输出、额度耗尽时仍能本地解释 | 本地模板是一次真实模型调用 |

刹车/油门模式只解释已有的描述性证据；路肩、循迹刹车、因果收益和具体练习处方仍受
原有验证门槛限制。历史候选最多说明离线、shadow 范围的结果，不能变成当前进站指令。
输入方式是文字和快捷提问；尚无语音识别或可靠的后台比赛对话。

## 启动 DeepSeek

从仓库根目录运行，API key 只在本机设置，不要贴到聊天、命令参数、Git 或截图中。
下面的 PowerShell 隐藏输入不会把 key 写入命令历史；子进程仅继承本次环境。

```powershell
$deepseekSecret = Read-Host 'DeepSeek API key' -AsSecureString
$deepseekPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($deepseekSecret)
try {
    $env:DEEPSEEK_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($deepseekPointer)
    .\scripts\start_live_engineer.ps1 -DeepSeek
} finally {
    Remove-Item Env:DEEPSEEK_API_KEY -ErrorAction SilentlyContinue
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($deepseekPointer)
    $deepseekSecret.Dispose()
}
```

如果所选端口已有旧 worker，启动器只复用它，**不会注入新 key 或重新配置模型**。
先正常停止那个已核实属于本项目的 worker，或选择空闲端口 `-Port 8766`。
不要停止所有 Python 进程。关闭页面不关闭 worker；最长运行时长仍由 `-Hours` 控制。

无 key 时也可执行 `start_live_engineer.ps1 -DeepSeek`：页面显示缺少密钥并提供本地解读，
不向云端发送请求。直接 CLI 默认 `--llm-provider off`，因此仅有环境 key 不会启用外发。

默认模型 `deepseek-flash`，可用 `-DeepSeekModel` / `--llm-model` 指定账号支持的模型。
接口采用官方 [Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/)
与 [JSON mode](https://api-docs.deepseek.com/guides/json_mode/)；JSON mode 不保证 schema，
本地仍严格校验字段和每个引用 ID。固定 HTTPS 出口，拒绝重定向、工具调用与截断回答。
不显示或保存模型内部 reasoning 内容。

默认每个新 worker 最多 **60 次模型尝试**，两次提问至少间隔 **10 秒**，单次总等待
**12 秒**，最多 **512 个输出 token**，没有自动重试。额度是请求/token 上限，不是金额
封顶；实际计费由账号和模型决定。重启会重置本进程计数，后台未退出的 I/O 不会产生
无限新线程。`-LlmRequestLimit` 可降低次数；CLI 另有
`--llm-min-interval-seconds` / `--llm-timeout-seconds`。

## 历史复盘入口

启动时可指定已有的完整、有效 `engineer-session-v1` JSON：

```powershell
.\scripts\start_live_engineer.ps1 -DeepSeek -SessionArtifact 'C:\Users\racer\Private Data\engineer-session.json'
```

这不是原始 JSONL capture，也不是任意 JSON 报告。加载器有大小/文件稳定性检查，并运行
既有完整 receipt 校验。无效输入阻止启动，不偷偷退化成未经验证的事实。页面才会出现
“历史复盘”入口。原始采集到完整离线 receipt 的构建仍由已有离线分析流程完成；本版本
没有在采集结束后自动补齐校准、规则或人工标签。复盘始终独立于实时状态。

## 隐私与本地 API

只外发允许的本地模板事实、能力状态与本次问题。不外发原始遥测、SessionInfo、
车手身份、用户名、路径、源哈希或整个 receipt。**你自己输入的问题也会外发**，所以
不要在问题中写姓名、账号或私人信息。程序不记录问答或 provider 原始错误正文。
原始遥测录制仍仅在本机私密目录，既不供 HTTP 下载，也不推到公共仓库。

`GET /api/engineer` 返回模型配置状态、调用计数、能力和最后一份回答。
`POST /api/engineer/question` 仅接受 `question` 和可选 `scope=live|session`，要求
严格同源 Host/Origin、页面独立令牌、JSON 类型及小体积限制。它只能排入问答任务，
没有文件读取、任意模型地址、遥测修改或模拟器控制入口。模型 key 永不进入页面。

## 上车前一次演练

```powershell
uv run python scripts/rehearse_llm.py
```

此演练走真实本地 HTTP / 队列 / 校验 / 展示数据结构，使用**合成遥测与假模型**，
不连接 iRacing、不读取 API key、不访问 DeepSeek、不保存伪造的 live capture。
它检查接线和失败边界，不能证明真实模型可用、游戏采集质量或比赛建议正确。
真实 key 配好后，先停车提问一条，确认回答来源为 DeepSeek；随后才按
[上车测试速查](LIVE_TRIAL_ZH.md) 检查本人驾驶数据和退出驾驶后的撤回行为。
