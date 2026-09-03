# Offline driving diagnosis evidence v1

`offline-driving-diagnosis-evidence-v1` 是一个 post-admission、只读、
fail-closed 的驾驶规则审计层。可复用实现现已进入安装包中的
`iracing_ai_engineer.driving_diagnosis`，并通过相对导入使用 package
`corner_cards` verifier；原脚本保留为兼容 wrapper。它读取完整的
`driving-model-replay-v1` JSON，
对每个候选窗口逐圈重算以下两个 `distance-driving-v1` 规则的最终谓词：

- `LATE_BRAKING_HURTS_EXIT`
- `THROTTLE_SECOND_LIFT`

它不是 raw telemetry replay。完整 driving replay 默认不包含重采样后的 60 Hz
Brake/Throttle/Steering traces，因此本层只能审计已经派生到
`model_output.corner_metrics` 的 onset、release、pickup、速度、loss 和
`second_lift`。输出固定标记：

- `claim_scope=DERIVED_RULE_AUDIT`
- `raw_telemetry_replayed=false`
- `status=SHADOW_ONLY`
- `advisor_only=true`
- `executable=false`
- `recommendations=[]`

它不产生 action、expected gain、confidence promotion 或因果措辞，也不修改
现有 replay/card、Windows r7 或 live-return bundle。

## 运行

```bash
PYTHONDONTWRITEBYTECODE=1 /opt/anaconda3/bin/python \
  scripts/build_offline_driving_diagnosis_evidence.py \
  data/derived/audi-spa-driving-replay-v1.json \
  --output data/derived/audi-spa-driving-diagnosis-evidence-v1.json
```

输出文件以 exclusive create 创建；目标已经存在时返回
`OUTPUT_CREATE_FAILED`，不会覆盖旧证据。stdout 与文件内容是同一份 canonical
JSON 加一个结尾换行。

## 输入与信任根

构建器先复用 package-external corner-card verifier，对完整 replay 执行 exact
top-level/model/config/metric schema、finite number、source receipt、capability、
lap-duration、phase 和 full-lap closure 校验。随后额外绑定：

- 输入文件原始 bytes 的 `driving_replay_serialized_sha256`；
- 完整解析对象的 `driving_replay_canonical_sha256`；
- producer 的 `driving_replay_sha256`；
- input provenance、normalized samples、track context、model output、model
  semantic 和 pipeline SHA-256；
- source/session/input kind/source content SHA 字段及
  `authenticity_status`。

哈希只能证明所读对象和来源声明一致，不能升级真实性。当前公共 IBT 明确为
`HASHED_LOCAL_FILE_NOT_AUTHENTICATED`。

## 冻结规则

规则不接受 CLI threshold override；所有阈值从已经被 replay pipeline SHA 绑定的
`driving_config` 读取，再进入独立的 `policy_sha256`。

### Late braking hurts exit

同一候选窗口、同一 reference lap 下，一个 comparison lap 必须同时满足：

1. brake onset 至少晚 `event_position_difference_m`；
2. brake release 至少晚相同距离；
3. apex/minimum speed 至少低 `speed_difference_mps`；
4. 50% throttle pickup 至少晚相同距离；
5. exit speed 至少低 `speed_difference_mps`；
6. carry loss 至少为 `minimum_carry_loss_s`；
7. total segment loss 至少为 `minimum_loss_s`。

所有边界都包含等号。当前 Audi/Spa 配置分别为 `8 m`、`0.4 m/s`、
`0.03 s` 和 `0.04 s`，且同弯至少需要 2 个支持圈。

### Throttle second lift

reference 必须有 throttle pickup，且其 `second_lift is False`。comparison lap
必须同时满足：

1. `second_lift is True`；
2. pickup 至少提前
   `max(5.0, event_position_difference_m * 0.625)` 米；
3. exit speed 不高于 reference `+0.2 m/s`；
4. total segment loss 至少为 `minimum_loss_s`。

当前提前距离为 `5 m`。C02/C06/C08 的 reference 自身
`second_lift=true`，所以这些窗口是 `NOT_EVALUABLE_REFERENCE_GATE`，不能写成
`NOT_OBSERVED`。

## 状态

每个 rule × corner 只能得到以下状态之一：

| 状态 | 精确定义 |
|---|---|
| `NOT_OBSERVED` | reference 和所有 comparison 必需指标可评估，但完整支持圈为 0 |
| `WAIT_REPEATED_EVIDENCE` | 完整支持圈大于 0，但少于 replay 绑定的 `min_evidence_laps` |
| `NOT_EVALUABLE_REFERENCE_GATE` | reference pattern 不适合作为该规则基线，例如 reference 本身 second-lift |
| `NOT_EVALUABLE_REQUIRED_METRIC` | reference 缺少必需指标，或支持数未达门槛且至少一个 comparison 缺失必需指标 |
| `RULE_SUPPORT_PRESENT_SHADOW` | 同弯支持数达到阈值，且源模型 diagnosis 与重算结果完全一致；仍仅为 shadow 派生规则支持 |

每个可评估 comparison lap 都保留固定顺序的逐谓词 receipt，包括 observed、
reference、required boundary、包含方向的 margin 和 PASS/FAIL。缺失指标不会被当作
predicate false 或 counterexample。若其他完整 comparison 已经达到同弯证据门槛，
则保持 `RULE_SUPPORT_PRESENT_SHADOW`，同时在缺失圈的逐圈 receipt 中保留
`NOT_EVALUABLE_REQUIRED_METRIC`；这与 frozen v1 排除不可用圈后继续计数的语义一致。

## 双向模型一致性

构建器不会只相信源模型的 `diagnoses` 数组。对于两个受审计规则，它要求：

- 重算达到最少支持数时，源模型必须存在完全对应的 diagnosis；
- 未达到支持数或不可评估时，源模型不得发出该 diagnosis；
- evidence 必须等于同弯全部完整支持圈；
- counterexamples 必须等于全部非 reference eligible 圈减 evidence；
- metric comparisons、median observed loss 和 support-count confidence 必须能从
  同一 metric grid 重算；
- `expected_gain_range_s` 必须精确重算为
  `[round(max(0.01, loss*0.4), 3), round(loss, 3)]`。

eligible、comparison、support 和 counterexample ordinal 在比对前统一升序规范化，
所以相同语义不会因输入数组顺序不同而得到不同判断。

漏报、误报或自洽重哈希后的伪造证据都返回
`FAIL_MODEL_RULE_DIVERGENCE`，不会降级成 WAIT。

## 固定外部门

本 contract 不读取 condition cohort、human labels、golden corner 或 A/B artifact，
因此不能自行把它们置为 PASS：

```text
condition_matching = WAIT_CONDITION_DATA
human_labels       = WAIT_HUMAN_LABELS
golden_corner      = CANDIDATE_NOT_GOLDEN
a_b_validation     = WAIT_AB_PRACTICE
```

当前 Audi/Spa 只有 7 个 clean laps，低于 condition-cohort-v1 默认的 8 个
matched laps；人工 label candidate 仍是 `PENDING_HUMAN_REVIEW` 且
`human_labels=[]`。这些事实与 rule audit 的完整执行相互独立。

## Audi R8 LMS Evo II GT3 × Spa 冻结结果

输入是 `public-audi-r8-evo2-spa / public-fixture-2023-12-race`，reference 为
lap 11，eligible laps 为 `2, 4, 5, 9, 10, 11, 16`，共有 8 个 candidate
windows。

| Rule | Window | 完整支持圈 | 状态 |
|---|---|---|---|
| Late braking hurts exit | C01–C08 | 全部为空 | `NOT_OBSERVED` |
| Throttle second lift | C03 | lap 2 | `WAIT_REPEATED_EVIDENCE` (`1/2`) |
| Throttle second lift | C07 | lap 9 | `WAIT_REPEATED_EVIDENCE` (`1/2`) |
| Throttle second lift | C02/C06/C08 | 无 | `NOT_EVALUABLE_REFERENCE_GATE` |
| Throttle second lift | C01/C04/C05 | 无 | `NOT_OBSERVED` |

late-braking 最接近的 C02/lap 2 只满足 6/7。其 brake onset 是 `2188 m`，
reference 是 `2253 m`，规则边界是 `2261 m`，margin 为 `-73 m`；它实际比
reference 早刹 65 m，所以不能作为 late-braking 证据。

当前冻结 identity：

- source SHA-256:
  `754d14e6e2870eb00d42368e36c7a15495cb68bfd5f829ab08665d0f95fc7f36`
- input serialized SHA-256:
  `b1825535c80d316b5379ae646c9710383050637ff7c335e9fe056a1d29010adf`
- producer driving replay SHA-256:
  `c5a8f19f156c57c3951e112df24ad3e3f07956961b78c68fe972a534955ebb82`
- input canonical SHA-256:
  `a54567a532f25693c1b82a8eacfbf11bc82fb7af94d1fdbb42eb774cb004b1ea`
- model output SHA-256:
  `f7a7165b19dfa08f1576b3f2e495cfbedb2011aa32317b91d3eb725967af3195`
- model semantic SHA-256:
  `74f6f52d5743260cbdcedaa59a0e0620afb1d8c8987195009e31f7cb86399df6`
- pipeline SHA-256:
  `f499f2dbb34fafbe5a1428c3336f7370fbd29bbf0154e701c4301767bc93c90e`
- diagnosis policy SHA-256:
  `1be1c564c323ff638feb822656d4bfff99228d5af68e37857114ec7a3249b00b`
- diagnosis evidence canonical SHA-256:
  `43988361cc71db074ad1a4e91e9dfa2f352705abba021fbe735dd1e83566ce67`
- generated receipt bytes / serialized SHA-256:
  `119,767` /
  `5430af296eeca439cdd566fe1fd636c61e010569b0b9088bb526543670c2e482`

## Acceptance coverage

Focused tests cover the exact real evidence above, all inclusive threshold boundaries,
the dynamic early-pickup formula, strict boolean types, reference/candidate missing
metrics, source diagnosis omission and false-positive attacks, self-consistent comparison
and expected-gain tampering, sufficient-support-plus-missing-candidate precedence,
duplicate/non-finite JSON, exact schemas and trust roots, metric/eligible order invariance,
cross-process hash-seed determinism, canonical self-hash, and exclusive output.
