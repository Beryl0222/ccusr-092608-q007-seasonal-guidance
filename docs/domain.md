# 领域约定

维护区域季节风险、健康规则、签发版本和纠正义务的领域事件。

聚合对象包括`regional_context`、`guidance_rule`、`release_revision`、`correction_obligation`。事件类型包括`CONTEXT_RECORDED`、`RULE_PROPOSED`、`CLINICAL_REVIEWED`、`RELEASE_SIGNED`、`CORRECTION_RAISED`。所有发生时间都必须携带时区，版本号从 1 开始递增，基础校验不会改写调用方输入。

## 事件载荷

- `RULE_PROPOSED`：载荷还需包含 `target_group`, `trigger_set`。
- `RELEASE_SIGNED`：载荷还需包含 `rule_version`, `effective_window`。
- `CORRECTION_RAISED`：载荷还需包含 `affected_releases`, `due_at`。

相同事件标识的业务幂等、冲突隔离和状态推进由上层服务负责；本仓库只定义可稳定交换的基础事实。
