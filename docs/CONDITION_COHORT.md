# Condition Cohort v1

`condition-cohort-v1` 在一个已经由 adapter 验证、且仍处于打开状态的
IBT 或 collector run 内，为指定目标圈寻找条件可比的 clean laps。它的用途是
给后续驾驶差异分析建立可审计的比较组，不是直接生成驾驶建议。

当前输出始终保留 `recommendations=[]`。`current_tire_wear`、
`traffic_model` 和 `personalized_coaching` 仍为 `SKIP`；本模块不会声称已经
知道当前轮胎磨损、交通造成的时间损失，或某项驾驶动作能带来因果收益。

## 五个匹配维度

每个 clean lap 都生成五维观测，再逐圈与目标圈比较。任何维度为
`INVALID` 时该 pair 为 `FAIL`；否则依次为 `NO_MATCH`、
`WAIT_CONDITION_DATA` 或 `MATCH`。

| 维度 | 直接证据 | v1 匹配规则 |
|---|---|---|
| 天气 | `TrackTempCrew`、`AirTemp`、`Precipitation`、`WindVel/Dir` | 干地门通过，温度和平均风向量均在阈值内 |
| 赛道状态 | 与输入源绑定的人工逐圈标签 | 目标圈和候选圈都必须标为 `DRY_STABLE` |
| 起始油量 | 整圈 `FuelLevelPct` | 无圈中加油，且起始比例差在阈值内 |
| 轮胎使用上下文 | `PlayerTireCompound`、`TireSetsUsed`、`LapCompleted`、`OnPitRoad` | 同配方，且相对 stint age 可比 |
| 直接交通邻近度 | `CarIdx*` 数组和同源赛道长度 | 目标圈和候选圈全程都没有过近的有效对手 |

比较仅在完整 clean-lap 窗口上进行。字段缺失不会被填零或推断；缺失证据是
`UNAVAILABLE`，非法值或自相矛盾的状态是 `INVALID`。

## 固定单位与默认阈值

所有进入配置哈希的阈值都是整数，避免平台相关的浮点序列化差异。比较采用
包含边界的规则：差值等于阈值仍可通过，超过阈值才不匹配或失效。

| 配置项 | 默认值 | 含义 |
|---|---:|---|
| `min_matched_laps` | 8 | 最少 matched laps；包含目标圈自身 |
| `track_temp_tolerance_millic` | 2,000 | `TrackTempCrew` 圈中位数相差不超过 2 °C |
| `air_temp_tolerance_millic` | 2,000 | `AirTemp` 圈中位数相差不超过 2 °C |
| `wind_vector_tolerance_mmps` | 2,000 | 平均风向量欧氏距离不超过 2 m/s |
| `dry_precipitation_max_ppm` | 1,000 | 两圈最大降水比例都不超过 0.1% |
| `fuel_start_tolerance_ppm` | 50,000 | 起始油量比例相差不超过 5 个百分点 |
| `fuel_refuel_jump_tolerance_ppm` | 100 | 相对圈内历史最低值最多回升 0.01 个百分点 |
| `traffic_clearance_mm` | 100,000 | 最近有效对手的环形纵向距离至少 100 m |
| `max_tire_usage_lap_delta` | 1 | 两圈相对 stint age 最多相差 1 圈 |

温度以千分之一摄氏度（`millic`）存储。降水和油量由 `[0, 1]` 比例转换为
百万分之一（`ppm`）。风速转换为两个整数 `mm/s` 分量；交通距离和同源
赛道长度均为整数毫米。

## 天气不等于赛道状态

`TrackTempCrew` 只表示赛道温度，是天气维度的一部分。它不能证明赛道为
干地，也不能替代人工赛道状态标签。`PlayerTrackSurface` 同样不会被提升为
`DRY_STABLE`：它用于 lap segmentation 和判断车辆所在表面，不是逐圈干湿
状态真值。

赛道状态标签必须：

- 以 lap ordinal 为粒度并绑定同一输入 evidence SHA-256；
- 带有明确 reviewer、UTC 时间、`MANUAL_REPLAY_REVIEW` 方法、证据文件
  SHA-256 和固定人工声明；
- 使用 `APPROVED` 状态，且 v1 只有 `DRY_STABLE` 能进入匹配组。

这些检查只能证明标签内容和声明完整、来源绑定一致。当前没有独立身份验证、
签名或可信审批服务，因此标签的真实性明确记录为
`SELF_ATTESTED_NOT_AUTHENTICATED`，绝不等同于人工身份已认证。

## 燃油：检测累计圈中回升

每圈必须具有完整且位于 `[0, 1]` 的 `FuelLevelPct`。匹配值取该圈第一条
样本并转换为 ppm。

圈中加油门不是只比较相邻 tick。算法维护从圈首开始的 running minimum；
任一后续值只要比历史最低值高出超过 100 ppm，就返回
`MID_LAP_REFUEL_OBSERVED` 和 `INVALID`。因此，每 tick 只增加 100 ppm、但
整圈累计增加很多的缓慢加油也无法绕过门控。

## 轮胎：相对 stint age，而非磨损估计

stint 边界只在观测到 `OnPitRoad: true -> false` 时建立。边界处的
`LapCompleted` 是该 stint 的基准，当前圈的 `stint_lap_age` 是累计完成圈数
减去该基准。

同一 stint 内，配方和 `TireSetsUsed` 必须保持一致。跨 stint 匹配则要求
目标和候选两侧都具有已观察到的 pit-exit 起点，并且边界处
`TireSetsUsed` 相对上一 stint 恰好增加 1；随后比较两者的相对
`stint_lap_age`，默认允许相差 1 圈。

`TireSetsUsed` 是累计计数和换胎边界证据，不是轮胎组的全局身份，也不是
当前胎面磨损。代码不读取已拆下轮胎的磨损值来冒充当前磨损，
`current_tire_wear` 因此继续为 `SKIP`。

## 交通：只做直接距离污染门

交通门仅使用有效对手的直接遥测：对手必须在赛道表面、未在 pit road，且
`CarIdxLapDistPct` 位于 `[0, 1]`。算法结合 adapter 从同一输入快照验证的
赛道长度，计算起终点闭环上的最短纵向距离，并取整圈最小值。任何有效对手
低于 100 m 都令该圈 `TRAFFIC_CONTAMINATED`。

这不是 traffic-loss 模型。它不估计跟车损失、超车损失、重返赛道窗口、
对手油量或策略；缺少 `CarIdx*` 数组时返回 `WAIT_CONDITION_DATA`，不会把
“看不到对手”解释为“赛道净空”。

## 数据就绪与可信就绪

两个状态有意分离：

- `readiness_status` 只描述五维数据比较。完整性失败为 `FAIL`；证据缺失为
  `WAIT_CONDITION_DATA`；证据齐全但 matched laps 少于 8 为
  `WAIT_MATCHED_LAPS`；至少 8 圈匹配才为 `PASS`。
- `trusted_readiness_status` 还要求赛道状态标签通过独立真实性门。当前标签
  只能是 `SELF_ATTESTED_NOT_AUTHENTICATED`，所以即使数据匹配为 `PASS`，
  可信状态仍是 `WAIT_HUMAN_AUTHENTICATION`。

对应的 `capabilities.track_state_authenticity` 会明确给出
`authenticated=false`。`readiness_status=FAIL` 时 `quality_gate` 也为
`FAIL`；由于当前真实性门还不能通过，其余当前输出为 `DEGRADED` 并保留
未认证原因。不能仅看到 `readiness_status=PASS` 就把 cohort 当作可用于
个性化建议的可信证据。

## 哈希与来源边界

公共 API 只接受 adapter 创建且仍然 active 的 `ValidatedIbtRun` 或
`ValidatedCollectorRun`，拒绝裸样本、伪造对象和已关闭 run。输出提供四个
不同目的的 SHA-256：

- `condition_config_sha256`：固定契约版本、feature pipeline 和阈值；
- `condition_semantic_sha256`：来源中立的五维观测、pair 和匹配结果；
- `condition_provenance_sha256`：实际 IBT/collector evidence、标准化输入、
  赛道长度证据和标签集；
- `condition_cohort_sha256`：完整输出绑定。

语义相同的 IBT 与 collector 输入应得到相同 semantic hash，但 provenance
和完整 cohort hash 必须不同。

## 公共 Audi/Spa 样本

冻结的 Audi R8 LMS Evo II GT3 × Spa IBT，以目标圈 ordinal 11 运行当前
契约时得到：

- `readiness_status=WAIT_CONDITION_DATA`；
- `trusted_readiness_status=WAIT_CONDITION_DATA`；
- `quality_gate=DEGRADED`；
- 7 个 clean laps、0 个 matched laps，低于默认最少 8 圈。

两个独立进程（第二个使用 `PYTHONHASHSEED=987654`）生成的完整 JSON
字节相同。冻结回执为：

- condition config SHA-256:
  `8c89e01fab4db83c4111662169b4525933106db08de318b617d558383a8e1a5f`
- condition semantic SHA-256:
  `c2f0f9e14445e73862036d6701fba4f7e80b409581e2805205edd2f4ec8d50cb`
- condition provenance SHA-256:
  `074a8f8c34f8eb557ee970950d2d727515628ca9e84280ed7dc818ee862b85bb`
- complete condition-cohort SHA-256:
  `83a20c37b40630e2295630ab54f08d7cbabc63ae8dc6ca7ef4245c3773d4d337`
- normalized input SHA-256:
  `f38d336a4d647886c0a4b8fce32fc4d53ba688b26b7c46d70e780e85c012b07e`

当前回执中的具体阻断原因是：

- `APPROVED_TRACK_STATE_LABEL_MISSING`：没有提供与该 IBT 绑定的逐圈人工
  赛道状态标签；
- `OPPONENT_ARRAYS_MISSING`：文件没有可用的 `CarIdx*` 对手数组，无法证明
  目标圈或候选圈的交通净空；
- `CROSS_STINT_SET_CONTEXT_UNOBSERVED`：部分跨 stint 比较缺少双方均已观察
  的换胎边界证据；
- `INSUFFICIENT_MATCHED_LAPS`：没有达到默认 8 圈。

部分 pair 还记录 `FUEL_START_LOAD_MISMATCH` 和
`TIRE_USAGE_AGE_MISMATCH`。它们表示可观测条件不匹配；前述缺失证据才使
整体保持 `WAIT_CONDITION_DATA`。模块不会为这个公开样本补造交通、赛道状态
或当前轮胎磨损证据。

## CLI 调用

`condition-cohort` 已接入本地 CLI。`--input-kind auto` 根据 `.ibt`、`.jsonl`
或 `.ndjson` 后缀判断输入；非标准后缀必须显式选择 `ibt` 或 `collector`。
IBT 的身份不是文件内稳定字段，因此必须提供 `--source-id` 和
`--session-id`：

```bash
uv run python scripts/run_local_cli.py condition-cohort \
  "data/raw/audir8lmsevo2gt3_spa up.ibt" \
  --source-id public-audi-r8-evo2-spa \
  --session-id public-fixture-2023-12-race \
  --target-lap-ordinal 11 \
  --receipt-only
```

该命令不传标签时会正常打印 `WAIT_CONDITION_DATA` 回执并退出 0。WAIT 是
有效分析结果，不是文件或契约错误。加入 `--require-ready` 后，CLI 只在
`trusted_readiness_status=PASS` 时退出 0；公共样本会退出 5。即使
`readiness_status=PASS`，自声明标签仍会令可信状态为
`WAIT_HUMAN_AUTHENTICATION` 并退出 5。

collector 输入从完整终止 receipt 读取已绑定的 source/session 身份，禁止用
CLI 参数重标：

```bash
uv run python scripts/run_local_cli.py condition-cohort \
  captures/complete-session.jsonl \
  --input-kind collector \
  --target-lap-ordinal 11
```

可选标签文件通过 `--track-state-labels PATH` 提供：

```bash
uv run python scripts/run_local_cli.py condition-cohort \
  "data/raw/audir8lmsevo2gt3_spa up.ibt" \
  --source-id public-audi-r8-evo2-spa \
  --session-id public-fixture-2023-12-race \
  --target-lap-ordinal 11 \
  --track-state-labels path/to/source-bound-track-state-labels.json \
  --require-ready
```

标签 JSON 必须只有 `ApprovedTrackStateLabelSet.to_dict()` 定义的精确字段，
使用 JSON array 表示逐圈标签，并通过内容 SHA-256 和输入 source binding
复核。重复 key、`NaN`/`Infinity`、缺失或额外字段、hash 不一致和错误 source
binding 都是契约错误。CLI 只读取并验证标签，不生成、不修改也不审批标签。

CLI 没有调整 `min_matched_laps` 的参数，始终使用固定默认值 8；也不输出任何
驾驶建议。退出码为：

- `0`：成功生成回执，包括未要求可信就绪时的正常 WAIT；
- `2`：命令行语法或参数类型错误；
- `3`：输入类型、身份、JSON、标签契约、IBT 格式或 adapter 错误；
- `4`：输入文件或标签文件不存在，机器可读错误为 `WAIT_DATA`；
- `5`：显式要求 `--require-ready`，但可信状态不是 `PASS`。

## Python 调用

当前 Python 调用必须在 adapter 上下文中完成，使 run 在消费样本期间保持
打开：

```python
from iracing_ai_engineer.adapters import open_ibt_telemetry
from iracing_ai_engineer.condition_cohort import build_condition_cohort

with open_ibt_telemetry(
    "data/raw/audir8lmsevo2gt3_spa up.ibt",
    source_id="public-audi-r8-evo2-spa",
    session_id="public-fixture-2023-12-race",
) as run:
    receipt = build_condition_cohort(run, target_lap_ordinal=11)
```

这个示例有意不传入标签集，因此复现上面的公共样本
`WAIT_CONDITION_DATA`，而不是演示一个可信 `PASS`。
