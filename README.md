# 南方秋季健康规则签发台

面向广西“前湿后燥”换季阶段的健康规则签发领域服务：九类内容组件分别版本化，规则作者、临床审阅、发布签发三权分立，县区冻结采用版本，渠道回执自动合并与冲突停推，来源更正区分撤回与纠正义务，并提供按地区/人群/当下事实的克制评估 API 与可追溯审计。

## 目录

- `contracts/domain.schema.json`：聚合、事件和载荷字段约定。
- `data/sample.json`：可直接校验的联调样例。
- `src/seasonal_guidance/`
  - `contracts.py`：基础信封契约校验（不改写输入）。
  - `clock.py`：可控时钟（`SystemClock` / `FixedClock`），生效、过期、纠正期限均经此取时。
  - `model.py`：九类组件与事件溯源投影（状态只能由事件重放得到）。
  - `service.py`：签发台领域服务（分权、版本冻结、回执合并、纠正）。
  - `evaluator.py`：评估 API，按地区/人群/事实输出必要且克制的行动提示。
  - `store.py`：JSONL 追加事件存储。
  - `api.py`：应用门面，组装时钟、存储、评估与审计，重启重放续算。
  - `cli.py`：命令行契约校验入口。
- `tests/`：信封契约测试与端到端领域测试。
- `docs/domain.md`：领域对象、事件语义与不变量。

## 关键不变量

1. 组件九类（地域气候阶段、风险人群、症状组合、暴露场景、生活建议、停止自我处置条件、转介级别、专家来源、适用时间）各自从 1 递增版本；规则只引用确定版本。
2. 同一事实可支持多个提醒；`general` 组件（含中医调养）不得进入临床槽位，不得自动升级为疾病判断。
3. 作者只能提出；临床审阅人不得是作者；签发人不得是作者或该版审阅人；只有审阅通过的版本可签发。
4. 县区采用时冻结规则版本与全部组件版本；上游新版不改变县区行为，直至显式重新冻结。
5. 同暴露多渠道回执签名一致才合并；事实冲突即停止该范围自动推送，人工核定后恢复。
6. 来源更正：未发送内容撤回；已送达消息保留原文并生成范围明确、带期限的纠正义务，逾期由可控时钟标出。
7. 生效窗、过期、纠正期限只看可控时钟；重启重放事件流续算，已见 `event_id` 幂等跳过。
8. 评估仅返回命中触发器、在生效窗内、未被停推/更正的提示；审计可解释为何出现、为何没出现、采用哪位专家哪版依据。

## 测试

```bash
python3 -m unittest discover -s tests
```

## 编译检查

```bash
python3 -m compileall -q src tests
```

## 样例契约校验

```bash
PYTHONPATH=src python3 -m seasonal_guidance.cli contracts/domain.schema.json data/sample.json
```

样例有效时输出 `valid`；发现问题时逐行给出字段、代码和中文说明，并返回非零状态。

## 最小使用示例

```python
from datetime import datetime, timezone, timedelta
from seasonal_guidance.api import GuidanceApi
from seasonal_guidance.clock import FixedClock
from seasonal_guidance.model import Facts

CST = timezone(timedelta(hours=8))
api = GuidanceApi(FixedClock(datetime(2026, 9, 27, 9, tzinfo=CST)), "events.jsonl",
                  actors={"author-a": {"roles": ["author"]},
                          "clinician-b": {"roles": ["reviewer"]},
                          "signer-c": {"roles": ["signer"]}})

# 1) 组件版本化 → 2) 作者提案 → 3) 临床审阅 → 4) 发布签发 → 5) 县区冻结
d = api.desk
d.version_component("ssc-redflag", "stop_self_care", "clinical",
                    {"conditions": ["胸痛持续不缓解"]}, author="author-a")
# ...其余八类组件同法版本化；之后 propose_rule / clinical_review / sign_release / freeze_adoption

# 渠道回执（内容一致自动合并；冲突自动停推）
api.record_receipt("450100", "mosquito", "weather-station", {"density": "high"})
api.record_receipt("450100", "mosquito", "grid-cdc", {"density": "high"})

# 按地区、人群、当下事实评估：不命中则 prompts 为空，skipped 给出未出现原因
facts = Facts(region_code="450100", group_id="chronic_elderly",
              climate_phase="wet_then_dry", symptoms=frozenset({"chest_tightness"}))
result = api.advise("450123", facts)

# 审计：为何出现 / 为何没有出现 / 专家与版本依据
api.audit_message("msg-1")
api.audit_absent("450123", facts)
```
