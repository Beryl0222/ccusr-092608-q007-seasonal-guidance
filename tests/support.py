"""测试共用的搭建工具。"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from seasonal_guidance.clock import ManualClock  # noqa: E402
from seasonal_guidance.service import IssuanceService  # noqa: E402

TZ = timezone(timedelta(hours=8))
START = datetime(2026, 9, 25, 10, 0, tzinfo=TZ)
REGION = "guangxi-nanning"
COUNTY = "马山县"
ELDERLY = "rg-elderly"
CHILD = "rg-child"
WINDOW_START = "2026-09-20T00:00:00+08:00"
WINDOW_END = "2026-10-31T00:00:00+08:00"

AUTHOR = "作者甲"
REVIEWER = "审阅乙"
PUBLISHER = "签发丙"


def load_schema() -> dict:
    return json.loads((ROOT / "contracts/domain.schema.json").read_text(encoding="utf-8"))


def make_service(journal=None, clock=None, register_base=True) -> IssuanceService:
    service = IssuanceService(
        journal_path=journal,
        clock=clock or ManualClock(START),
        schema=load_schema(),
    )
    if register_base:
        register_base_content(service)
    return service


def register_base_content(service: IssuanceService) -> None:
    service.register_content(
        "climate_phase",
        "cp-autumn",
        region=REGION,
        name="前湿后燥",
        window={"start": "2026-09-01T00:00:00+08:00", "end": "2026-11-30T00:00:00+08:00"},
    )
    service.register_content("risk_group", ELDERLY, label="慢病老人")
    service.register_content("risk_group", CHILD, label="学龄儿童")
    service.register_content("symptom_set", "ss-resp", symptoms=["胸闷", "呼吸困难", "持续高热"])
    service.register_content("exposure_scenario", "sc-pollen", category="pollen", description="秋季花粉暴露")
    service.register_content("life_advice", "la-mask", text="花粉浓度高，外出请佩戴口罩", category="general")
    service.register_content("life_advice", "la-window", text="关闭门窗，减少花粉入室", category="general")
    service.register_content("life_advice", "la-tcm", text="秋燥宜食百合银耳汤", category="tcm_wellness")
    service.register_content(
        "stop_condition",
        "stop-resp",
        description="出现胸闷或呼吸困难，立即停止自我处置并就医",
        symptom_set_id="ss-resp",
    )
    service.register_content(
        "referral_level", "rl-county", level="county_hospital", instruction="立即前往县级医院呼吸科就诊"
    )
    service.register_content(
        "expert_source", "es-wei", expert="韦医师", organization="自治区疾控中心", title="秋季呼吸道风险研判"
    )


def propose_clinical_rule(service: IssuanceService, advice_ids=("la-mask",), rule_id=None):
    return service.propose_rule(
        author=AUTHOR,
        target_group=ELDERLY,
        trigger_set=[{"fact": "pollen_index", "op": ">=", "value": 3}],
        climate_phase_id="cp-autumn",
        scenario_id="sc-pollen",
        advice_ids=list(advice_ids),
        expert_source_id="es-wei",
        stop_condition_id="stop-resp",
        referral_level_id="rl-county",
        rule_id=rule_id,
    )


def make_clinical_release(service: IssuanceService, advice_ids=("la-mask",), rule_id=None):
    rule = propose_clinical_rule(service, advice_ids=advice_ids, rule_id=rule_id)
    service.clinical_review(rule.rule_id, reviewer=REVIEWER, approve=True)
    release = service.sign_release(rule.rule_id, publisher=PUBLISHER, effective_start=WINDOW_START, effective_end=WINDOW_END)
    return rule, release


def propose_tcm_rule(service: IssuanceService, rule_id=None):
    return service.propose_rule(
        author=AUTHOR,
        target_group=ELDERLY,
        trigger_set=[{"fact": "humidity_pct", "op": "<=", "value": 60}],
        climate_phase_id="cp-autumn",
        scenario_id="sc-pollen",
        advice_ids=["la-tcm"],
        expert_source_id="es-wei",
        rule_id=rule_id,
    )


def make_tcm_release(service: IssuanceService, rule_id=None):
    rule = propose_tcm_rule(service, rule_id=rule_id)
    service.clinical_review(rule.rule_id, reviewer=REVIEWER, approve=True)
    return service.sign_release(rule.rule_id, publisher=PUBLISHER, effective_start=WINDOW_START, effective_end=WINDOW_END)


def adopt(service: IssuanceService, release, county=COUNTY):
    return service.adopt_release(county, REGION, release.release_id)
