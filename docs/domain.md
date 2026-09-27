# 领域约定

维护区域季节风险、健康规则、签发版本和纠正义务的领域事件。

聚合对象包括`regional_context`、`guidance_rule`、`release_revision`、`correction_obligation`。事件类型包括`CONTEXT_RECORDED`、`RULE_PROPOSED`、`CLINICAL_REVIEWED`、`RELEASE_SIGNED`、`CORRECTION_RAISED`。所有发生时间都必须携带时区，版本号从 1 开始递增，基础校验不会改写调用方输入。

## 事件载荷

- `RULE_PROPOSED`：载荷还需包含 `target_group`, `trigger_set`。
- `RELEASE_SIGNED`：载荷还需包含 `rule_version`, `effective_window`。
- `CORRECTION_RAISED`：载荷还需包含 `affected_releases`, `due_at`。

## 服务层语义

契约只定义可稳定交换的基础事实；以下业务语义由 `seasonal_guidance.service.IssuanceService` 落实。

### 分别版本化的内容

地域气候阶段（`climate_phase`）、风险人群（`risk_group`）、症状组合（`symptom_set`）、暴露场景（`exposure_scenario`）、生活建议（`life_advice`）、停止自我处置条件（`stop_condition`）、转介级别（`referral_level`）、专家来源（`expert_source`）各自独立登记、各自从 1 开始递增版本；适用时间以 `TimeWindow` 表达。规则只引用内容标识，签发时把当时的内容版本整体冻结进 `content_snapshot`。

### 签发工作流

规则作者只能提出内容（`propose_rule`，含修订即版本递增）；临床审阅（`clinical_review`）不得由作者本人执行；发布签发（`sign_release`）不得由作者或审阅人执行。被驳回或未审阅的规则不能签发；修订后必须重新审阅。县区采用（`adopt_release`）即冻结 `rule_version`，之后的签发不影响已采用的县，重新采用才升级。

### 提醒的克制原则

- 只有签发且处于适用时间内的版本才产生提示；触发条件必须由当下事实满足。
- 同一条事实可以支持多个提醒；跨版本同文案的提示去重。
- 中医调养建议（`tcm_wellness`）只以非诊断生活建议出现，绝不自动升级为疾病判断；停止自我处置与转介提示必须引用独立的临床内容（停止条件与转介级别成对）。
- 停止自我处置条件命中时，同规则的普通生活建议让位，保证就医触发条件醒目。

### 来源更正

`raise_correction` 仅撤回尚未发送（`queued`）的消息；已发送、已送达的消息保留原文，并生成范围明确到县、人群与消息清单的纠正义务（`correction_obligation`，含 `due_at`）。被更正的签发版本停止再产生提示，直到修订后重新签发。纠正期限由注入的时钟推进，逾期仍未履行的义务标记为 `overdue`。

### 暴露回执合并

同一暴露（`exposure_key`）来自不同渠道的回执合并为一条记录；事实字段取值不一致即标记冲突，冲突期间停止自动推送，查询评估也不采用其事实，直至人工裁定（`resolve_conflict`）后恢复。

### 时钟与重启

预警生效、过期与纠正期限都以注入的 `Clock` 为准（`ManualClock` 用于演练）。全部状态变更追加写入 JSONL 日志，重启后重放日志继续，事件版本号不回头。

### 审计

每次评估都留下决定记录（`emitted` / `suppressed` / `held` 及原因）。`audit.explain_message` 解释一条消息为何出现（规则、签发版本、专家来源及其版本、内容依据）；`audit.explain_absence` 列出某地区某人群没有收到提示的原因。
