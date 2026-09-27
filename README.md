# 南方秋季健康规则签发台

维护区域季节风险、健康规则、签发版本和纠正义务的领域事件，并提供规则签发、暴露合并、提示评估与审计解释的服务层实现。

## 目录

- `contracts/domain.schema.json`：对象、事件和载荷字段约定。
- `data/sample.json`：可直接校验的联调样例。
- `src/seasonal_guidance/contracts.py`：基础契约校验。
- `src/seasonal_guidance/models.py`：分别版本化的内容项、规则、签发版本、消息、暴露与纠正义务。
- `src/seasonal_guidance/clock.py`：可控时钟（预警生效、过期、纠正期限都以此为准）。
- `src/seasonal_guidance/service.py`：签发工作流、县区采用冻结、来源更正、暴露回执合并、日志持久化与重启续跑。
- `src/seasonal_guidance/advisory.py`：按地区、人群与当下事实评估必要且克制的行动提示。
- `src/seasonal_guidance/audit.py`：解释一条消息为何出现、为何没有出现、采用了哪位专家的哪版依据。
- `src/seasonal_guidance/cli.py`：命令行校验入口。
- `tests/`：信封、时间、版本、事件载荷与签发台行为测试。
- `docs/domain.md`：领域对象、事件与服务层语义。

## 测试

```bash
python3 -m unittest discover -s tests
```

## 编译检查

```bash
python3 -m compileall -q src tests
```

## 样例校验

```bash
PYTHONPATH=src python3 -m seasonal_guidance.cli contracts/domain.schema.json data/sample.json
```

样例有效时输出 `valid`；发现问题时逐行给出字段、代码和中文说明，并返回非零状态。

## 服务层速览

```python
from datetime import datetime, timedelta, timezone
from seasonal_guidance import IssuanceService, ManualClock

clock = ManualClock(datetime(2026, 9, 25, 10, 0, tzinfo=timezone(timedelta(hours=8))))
service = IssuanceService(journal_path="journal.jsonl", clock=clock)

# 内容分别版本化登记
service.register_content("climate_phase", "cp-autumn", region="guangxi-nanning", name="前湿后燥",
                         window={"start": "2026-09-01T00:00:00+08:00", "end": "2026-11-30T00:00:00+08:00"})
service.register_content("risk_group", "rg-elderly", label="慢病老人")
service.register_content("life_advice", "la-mask", text="花粉浓度高，外出请佩戴口罩", category="general")
service.register_content("expert_source", "es-wei", expert="韦医师",
                         organization="自治区疾控中心", title="秋季呼吸道风险研判")
# ……症状组合、暴露场景、停止自我处置条件、转介级别同样分别登记

# 作者提出 → 临床审阅 → 发布签发（三者身份相互独立）
rule = service.propose_rule(
    author="作者甲", target_group="rg-elderly",
    trigger_set=[{"fact": "pollen_index", "op": ">=", "value": 3}],
    climate_phase_id="cp-autumn", scenario_id="sc-pollen",
    advice_ids=["la-mask"], expert_source_id="es-wei",
)
service.clinical_review(rule.rule_id, reviewer="审阅乙", approve=True)
release = service.sign_release(rule.rule_id, publisher="签发丙",
                               effective_start="2026-09-20T00:00:00+08:00",
                               effective_end="2026-10-31T00:00:00+08:00")

# 县区采用即冻结版本；暴露回执合并后自动推送；冲突即停推
service.adopt_release("马山县", "guangxi-nanning", release.release_id)
service.ingest_receipt("rc-1", "exp-1", "村级上报", "guangxi-nanning", {"pollen_index": 4})

# 查询 API：必要且克制的行动提示
prompts = service.current_prompts("guangxi-nanning", "rg-elderly")

# 来源更正：仅撤回未发送内容，已送达保留原文并生成纠正义务
service.raise_correction([release.release_id], "花粉阈值引用过时", "2026-09-30T18:00:00+08:00")
```

重启时用同一 `journal_path` 构造 `IssuanceService` 即可从日志恢复全部状态，事件版本号继续递增。
