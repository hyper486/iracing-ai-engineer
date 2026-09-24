# Native post-session tire-service review

The native **采集复盘** tab now connects an exact capture/journal pair to the
existing `reviewed-tire-service-history-v1` input format. This is an explicit
human-review workflow, not automatic approval, authenticated service truth,
physical wear, model calibration or live acceptance.

## 使用方法

1. 正常结束录制，退出游戏。在 **采集复盘 → 准备换胎审核…** 依次选择完整原始
   采集和同次完整诊断日志；等待后台核对完成。不能在游戏连接时进行此审核。
2. 点击 **审核并导出…**。列表显示可定位出站的 tick、完成圈数、胎种和套数计数；
   车内按钮的自述只作对照，不会默认选入审核结果。字段或连续性不可用的帧数也
   明确显示；缺失的出站不会补猜。
3. 逐项选择整套四条新胎、没有换胎、部分更换／不确定，或未审核。已审核项须
   选择人工核对过的本机服务记录／回放证据；可填写视频定位的整数秒数。必须
   自行确认实际服务，不能把进站请求、胎数变化或车内按钮当证明。原始采集和
   确认日志（包括改名副本）不能作为独立服务证据。
4. 不确定时可选 **未审核**：不会生成该次服务标签，后续旧胎龄可能因此失效。
   “部分更换／不确定”是有证据的已审核分类，也不会建立整套新胎起点。每一行
   都必须明确选择；至少一行须有已审核证据。程序不会替用户勾选人工声明。
5. 勾选人工核对声明，再点击导出。程序重新读取、校验同一对输入，核对草稿未
   变化，并只在本机计算证据摘要。证据不上传、不执行、不播放、不复制；请保留
   原件及位置。后台工作可取消，游戏重新连接也会取消未完成的工作。
6. 成功后，状态提示新目录名。私有输出位于
   `%LOCALAPPDATA%\iRacingAIEngineer\tire-reviews\tire-review-<random-id>\`：
   - `service-history.json`：现有离线分析直接接收的服务历史及其 `history_sha256`。
   - `review.json`：草稿、每项人工选择、独立证据哈希／大小／位置，以及标签收据。

导出使用新目录和 CreateNew 文件，不覆盖既有记录。写入失败或取消可能留下本次
新建的不完整目录；只有应用报告成功的记录才应使用。不要把任何私有审核文件或
原始证据提交到公开仓库。应用未保存证据文件路径，请自行保留与摘要对应的原件。

## Exact lineage and downstream use

The plan reuses paired capture-byte/frame validation and the offline tire
tracker's normalized pit-exit rules. Label coordinates use the **first captured
off-pit sample**, not the earlier in-stall confirmation tick. The exact adapter
input-evidence digest and the existing ten-field event-identity digest are
computed from that same held, fully validated capture. They are not substituted
with a pathname, arbitrary user digest or raw-file hash.

At most 256 exits are retained; exceeding the limit fails instead of truncating.
Multiple captured sessions or contradictory tire sequences cannot be flattened
into the tick-keyed history format. Missing channels/continuity break exit
tracking. Unsupported input does not alter ordinary raw-only or paired replay.
The UI plan remains separate from live/model/voice snapshots. Starting a new
replay clears it, and stale-dialog submissions must match the current plan hash.

Export requires one explicit disposition per proposed exit, independently
selected local evidence for reviewed rows and a positive user attestation.
Every label receipt binds that exact plan/exit, selected service kind and
evidence digest/size/optional seconds locator. Repeated references to one video
are hashed once in that export, and the total distinct evidence-read budget is
4 GiB. Private regular-file, no-link/reparse and checkout-exclusion guards apply.
No evidence contents, filenames, paths, player slots or arbitrary metadata enter
the output. File size/hash binds retained evidence, but cannot prove it actually
shows the claimed service or was reviewed by a particular human.

The user can supply `service-history.json` and its retained history digest to
the existing `finalize-live-analysis` / `verify-live-analysis` arguments:

```text
--tire-service-history <private-export>/service-history.json
--expected-tire-service-history-sha256 <retained history_sha256>
```

The existing same-capture replay remains mandatory. No-change labels preserve
only an already known origin; partial or unreviewed stops withdraw it. A manual
correction of a driver assertion is retained as a distinct review decision,
not written back into the original journal. Export does not load a model or
modify live settings, strategy, telemetry, microphone, headphones or simulator.

## Trust and acceptance

The review receipts explicitly say `SELF_ATTESTED_NOT_AUTHENTICATED`,
`source_authenticity=UNVERIFIED`, and `live_acceptance=false`. The legacy history
format's reviewed provenance denotes the user's review assertion, not software
authentication. Downstream consumers still trust supplied labels at that input
boundary; public hashes are not signatures or truth detectors. The capture's
source-authenticity proof, independent condition/fuel calibration, performance
model admission, holdout checks and live tire/rejoin decisions remain separate.

Synthetic tests exercise actual paired files, export/readback, exact adapter
lineage and the existing tire-context consumer. They cover manual corrections,
unreviewed stops, partial service, changed input, absent attestation/evidence,
copied source files, cancellation, evidence bounds and private paths. Native
tests cover empty initial choices, the unchecked declaration, evidence/locator
selection, minimum-window visibility, delegation and busy/stale rejection.
The frozen self-test only creates invented evidence/choices in its own temporary
directory. Display-only synthetic UI mode cannot export a real review.
