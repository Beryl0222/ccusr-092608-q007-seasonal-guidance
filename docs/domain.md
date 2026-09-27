# 领域约定

维护区域季节风险、健康规则、签发版本、渠道回执、消息送达与纠正义务的领域事件。

聚合对象包括 `regional_context`、`guidance_component`、`guidance_rule`、`release_revision`、`exposure_receipt`、`message_dispatch`、`correction_obligation`。所有发生时间都必须携带时区；事件序号 `version` 从 1 开始、按聚合严格递增；基础校验不会改写调用方输入。相同事件标识的业务幂等、冲突隔离和状态推进由上层服务负责；本仓库只定义可稳定交换的基础事实。

## 内容组件（九类，分别版本化）

一条规则由以下组件引用拼装，每个组件以 `COMPONENT_VERSIONED` 独立存证、版本号各自从 1 递增；规则只引用组件的确定版本，事后组件改版不影响已签发或已冻结的规则：

| 组件 | component_kind | 说明 |
| --- | --- | --- |
| 地域气候阶段 | `climate_phase` | 如广西“前湿后燥”阶段、气温/湿度区间 |
| 风险人群 | `risk_group` | 如慢病老人、学龄儿童 |
| 症状组合 | `symptom_set` | 触发评估的症状/指标组合 |
| 暴露场景 | `exposure_scenario` | 花粉、蚊媒、节日聚集等场景及回执键 |
| 生活建议 | `lifestyle_advice` | 普通生活建议，`classification="general"` |
| 停止自我处置条件 | `stop_self_care` | 命中即不得继续自我处置的红旗条件 |
| 转介级别 | `referral_level` | 基层就诊 / 急诊等分级 |
| 专家来源 | `expert_source` | 专家身份与依据描述 |
| 适用时间 | `applicable_window` | 生效/失效时间窗与纠正期限建议 |

`classification` 区分事实强度：`general`（普通生活建议，含中医调养）、`clinical`（临床判读内容）。**中医调养等 `general` 组件不得被自动升级为疾病判断**：规则若将 `general` 组件放入 `stop_self_care`、`referral_level` 等临床槽位，或仅凭生活建议推导出转介结论，签发校验必须拒绝。

## 三权分立的规则生命周期

1. `RULE_PROPOSED`：规则作者只能提出内容，载荷含 `target_group`、`trigger_set` 与各槽位的组件版本引用、`proposed_by`。
2. `CLINICAL_REVIEWED`：临床审阅人独立记录 `decision`（`approved` / `changes_requested`）、`reviewer`、`expert_refs` 及意见。审阅人不得是作者本人。
3. `RELEASE_SIGNED`：发布签发人独立把通过审阅的规则签为发布版，载荷含 `rule_version`、`effective_window`、`signer`。签发人不得是作者或该版审阅人；只有已通过临床审阅的版本可签发。

同一条事实（组件版本）可以被多个规则引用；撤回或纠正组件时，引用它的全部发布版都在影响范围内。

## 县区采用与冻结

`COUNTY_ADOPTION_FROZEN`：县区采用规则时冻结 `rule_version` 及其全部组件版本。上游签发新版不改变县区行为，直到县区显式冻结新版本。评估 API 只使用该县冻结版本所引用的内容。

## 暴露回执合并与停推

- `RECEIPT_RECORDED`：不同渠道就同一暴露事实（`region_code` + `exposure_key`）回报回执，载荷含 `channel` 与由规范化事实计算的 `facts_signature`。
- `RECEIPTS_MERGED`：同键回执合并。签名一致 → `outcome="merged"`；事实内容冲突 → `outcome="conflict"`。
- 冲突时发出 `PUSH_SUSPENDED`（`scope` 标明地区/暴露范围，`reason` 说明冲突），**停止该范围的自动推送**；人工处理后 `PUSH_RESUMED` 恢复。

## 消息与纠正

- `MESSAGE_PREPARED`：按冻结规则生成待发消息（含规则与组件版本依据）；`MESSAGE_DELIVERED` 标记送达与送达时间；未送达前可 `MESSAGE_REVOKED` 撤回。
- 来源更正时 `CORRECTION_RAISED`，载荷含 `affected_releases`（受影响的签发版本范围）与 `due_at`（纠正期限）。
  - **尚未发送**的错误内容：撤回（不再生成、撤回待发消息）。
  - **已送达**消息：保留原文不删改，转为纠正义务；纠正消息必须明确范围（哪些签发版、哪些消息、错在何处）。
- `CORRECTION_FULFILLED` 在纠正按范围送达后关闭义务。逾期未履行由审计端按可控时钟标记。

## 时间与重启

预警生效、过期、纠正期限均由可控时钟（`now()`）判定，不直接读取系统时钟，测试与重放可注入固定时间。全部状态由事件流重放得到；服务重启后读取历史事件即可继续，已处理的 `event_id` 幂等跳过。

## 评估与审计 API

- 评估接口按 **地区 + 人群 + 当下事实**（气候阶段、症状、暴露、时间）返回必要且克制的行动提示：仅返回命中触发器且处于生效窗内的内容，并显式给出停止自我处置条件与转介级别；不命中时不产生提示。
- 审计解释单条消息：为何出现（命中的事实/触发器/窗口/冻结版本）、为何没有出现（被哪条规则版本、窗口、冲突停推或审阅未通过拦截），以及采用了哪位专家、哪一版依据（规则版本 + 各组件版本 + `expert_source` 版本）。

## 事件载荷

- `COMPONENT_VERSIONED`：`component_kind`、`component_version`、`classification`。
- `RULE_PROPOSED`：`target_group`、`trigger_set`。
- `CLINICAL_REVIEWED`：`rule_version`、`decision`。
- `RELEASE_SIGNED`：`rule_version`、`effective_window`。
- `COUNTY_ADOPTION_FROZEN`：`county_id`、`rule_version`。
- `RECEIPT_RECORDED`：`region_code`、`exposure_key`、`channel`、`facts_signature`。
- `RECEIPTS_MERGED`：`exposure_key`、`receipt_ids`、`outcome`。
- `PUSH_SUSPENDED`：`scope`、`reason`；`PUSH_RESUMED`：`scope`。
- `MESSAGE_PREPARED`：`county_id`、`region_code`、`target_group`、`rule_id`、`rule_version`。
- `MESSAGE_DELIVERED`：`message_id`、`delivered_at`；`MESSAGE_REVOKED`：`message_id`、`reason`。
- `CORRECTION_RAISED`：`affected_releases`、`due_at`；`CORRECTION_FULFILLED`：`correction_id`、`fulfilled_at`。
