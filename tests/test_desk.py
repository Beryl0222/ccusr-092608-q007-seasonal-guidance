"""签发台端到端领域测试：分权、冻结、窗口、回执冲突、纠正义务、审计与重启。"""

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from seasonal_guidance.api import GuidanceApi
from seasonal_guidance.clock import FixedClock
from seasonal_guidance.model import Facts
from seasonal_guidance.service import DomainError

CST = timezone(timedelta(hours=8))
T0 = datetime(2026, 9, 25, 8, 0, tzinfo=CST)

ACTORS = {
    "author-a": {"name": "内容作者甲", "roles": ["author"]},
    "clinician-b": {"name": "临床审阅乙", "roles": ["reviewer"]},
    "signer-c": {"name": "发布签发丙", "roles": ["signer"]},
    # 兼具两角色的参与方，用于验证“同一人不得跨环节”在角色通过后仍被拦截
    "dual-ar": {"name": "兼著审丁", "roles": ["author", "reviewer"]},
    "dual-rs": {"name": "兼审签戊", "roles": ["reviewer", "signer"]},
}

COUNTY = "450123"          # 示例县区
REGION = "450100"          # 南宁区域码
GROUP = "chronic_elderly"  # 慢病老人


def ts(day: int, hour: int = 8) -> datetime:
    return datetime(2026, 9, day, hour, tzinfo=CST)


class World:
    """搭建一条完整签发链路的测试夹具。"""

    def __init__(self, api: GuidanceApi) -> None:
        self.api = api
        d = api.desk
        # 九类组件分别版本化（中医调养作为 general 生活建议进入）
        d.version_component("cp-autumn", "climate_phase", "clinical",
                            {"phase": "wet_then_dry", "label": "前湿后燥"}, author="author-a")
        d.version_component("rg-elder", "risk_group", "clinical",
                            {"group_id": GROUP, "label": "慢病老人"}, author="author-a")
        d.version_component("ss-cardio", "symptom_set", "clinical",
                            {"symptoms": ["chest_tightness", "breathlessness", "chest_pain"]},
                            author="author-a")
        d.version_component("es-mosquito", "exposure_scenario", "clinical",
                            {"exposure_key": "mosquito", "label": "蚊媒活动"}, author="author-a")
        d.version_component("la-tcm", "lifestyle_advice", "general",
                            {"text": ["适度秋冻、及时补水", "中医调养：温润润燥，忌大汗"]},
                            author="author-a")
        d.version_component("ssc-redflag", "stop_self_care", "clinical",
                            {"conditions": ["胸痛持续不缓解", "明显气促伴口唇发紫"]},
                            author="author-a")
        d.version_component("rl-urgent", "referral_level", "clinical",
                            {"level": "urgent_care", "text": "立即转急诊评估"}, author="author-a")
        d.version_component("exp-zhang", "expert_source", "clinical",
                            {"name": "张医生", "title": "呼吸内科主任医师",
                             "org": "区域临床专家组", "basis": "2026 秋季基层转介共识"},
                            author="author-a")
        d.version_component("aw-sep", "applicable_window", "general",
                            {"season": "autumn"}, author="author-a")
        api._commit()

        self.slots = {
            "climate_phase": ("cp-autumn", 1),
            "risk_group": ("rg-elder", 1),
            "symptom_set": ("ss-cardio", 1),
            "exposure_scenario": ("es-mosquito", 1),
            "lifestyle_advice": ("la-tcm", 1),
            "stop_self_care": ("ssc-redflag", 1),
            "referral_level": ("rl-urgent", 1),
            "expert_source": ("exp-zhang", 1),
            "applicable_window": ("aw-sep", 1),
        }
        self.triggers = {
            "climate_phases": ["wet_then_dry"],
            "symptoms_any": ["chest_tightness", "breathlessness"],
            "exposure_keys": ["mosquito"],
        }

    def publish_v1(self) -> None:
        d = self.api.desk
        d.propose_rule("rule-cardio", GROUP, self.triggers, self.slots, proposed_by="author-a")
        d.clinical_review("rule-cardio", 1, reviewer="clinician-b", decision="approved",
                          expert_refs=[("exp-zhang", 1)], notes="红旗条件齐备，同意")
        d.sign_release("rule-cardio", 1, signer="signer-c",
                       window_start=ts(1), window_end=datetime(2026, 10, 31, tzinfo=CST))
        d.freeze_adoption(COUNTY, "rule-cardio", 1)
        self.api._commit()


def make_api(path: str) -> GuidanceApi:
    schema = json.loads((ROOT / "contracts/domain.schema.json").read_text(encoding="utf-8"))
    return GuidanceApi(FixedClock(T0), path, actors=ACTORS, schema=schema)


class GuidanceDeskTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.api = make_api(str(Path(self._tmp.name) / "events.jsonl"))
        self.world = World(self.api)
        self.world.publish_v1()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def facts(self, symptoms=frozenset({"chest_tightness"}), group=GROUP, region=REGION) -> Facts:
        return Facts(region_code=region, group_id=group, climate_phase="wet_then_dry",
                     symptoms=symptoms)

    # -- 评估克制性 ---------------------------------------------------------

    def test_matched_facts_yield_restrained_prompt(self) -> None:
        # 蚊媒暴露由两个渠道一致回执确认后合并
        self.api.record_receipt(REGION, "mosquito", "weather-station", {"density": "high"})
        self.api.record_receipt(REGION, "mosquito", "grid-cdc", {"density": "high"})
        result = self.api.advise(COUNTY, self.facts())
        self.assertEqual(1, len(result.prompts))
        prompt = result.prompts[0]
        self.assertEqual("rule-cardio", prompt.rule_id)
        self.assertEqual(1, prompt.rule_version)
        self.assertTrue(any("中医调养" in line for line in prompt.lifestyle_advice))
        self.assertTrue(all("停止自我处置" in line for line in prompt.stop_self_care))
        self.assertEqual("urgent_care", prompt.referral["level"])

    def test_no_match_no_prompt_and_explained(self) -> None:
        result = self.api.advise(COUNTY, self.facts(symptoms=frozenset()))
        self.assertEqual((), result.prompts)
        self.assertEqual("triggers_not_met", result.skipped[0].code)

        absent = self.api.audit_absent(COUNTY, self.facts(symptoms=frozenset()))
        self.assertEqual([], absent["emitted"])
        self.assertIn("triggers_not_met", [r["reason_code"] for r in absent["not_emitted"]])

    def test_group_and_county_are_scoped(self) -> None:
        result = self.api.advise(COUNTY, self.facts(group="school_children"))
        self.assertEqual((), result.prompts)
        self.assertEqual("group_mismatch", result.skipped[0].code)

        result = self.api.advise("999999", self.facts())
        self.assertEqual("no_frozen_adoption", result.skipped[0].code)

    # -- 三权分立 -----------------------------------------------------------

    def test_author_cannot_review_own_rule(self) -> None:
        self.api.desk.version_component("la-2", "lifestyle_advice", "general",
                                        {"text": ["新版建议"]}, author="author-a")
        self.api.desk.propose_rule(
            "rule-cardio", GROUP, self.world.triggers,
            {**self.world.slots, "lifestyle_advice": ("la-2", 1)}, proposed_by="dual-ar")
        self.api._commit()
        with self.assertRaisesRegex(DomainError, "审与著"):
            self.api.clinical_review("rule-cardio", 2, reviewer="dual-ar", decision="approved")

    def test_signer_must_be_independent_and_review_first(self) -> None:
        self.api.desk.propose_rule(
            "rule-cardio", GROUP, self.world.triggers,
            {**self.world.slots, "lifestyle_advice": ("la-tcm", 1)}, proposed_by="author-a")
        self.api._commit()
        # 未经临床审阅不能签发
        with self.assertRaisesRegex(DomainError, "临床审阅通过"):
            self.api.sign_release("rule-cardio", 2, signer="signer-c",
                                  window_start=ts(1), window_end=datetime(2026, 10, 31, tzinfo=CST))
        self.api.clinical_review("rule-cardio", 2, reviewer="dual-rs", decision="approved",
                                 expert_refs=[("exp-zhang", 1)])
        # 签发人不能是审阅人
        with self.assertRaisesRegex(DomainError, "签与审"):
            self.api.desk.sign_release(
                "rule-cardio", 2, signer="dual-rs",
                window_start=ts(1), window_end=datetime(2026, 10, 31, tzinfo=CST))

    def test_changes_requested_cannot_be_signed(self) -> None:
        self.api.desk.propose_rule(
            "rule-cardio", GROUP, self.world.triggers, self.world.slots, proposed_by="author-a")
        self.api._commit()
        self.api.clinical_review("rule-cardio", 2, reviewer="clinician-b",
                                 decision="changes_requested", notes="需补转介阈值")
        with self.assertRaisesRegex(DomainError, "临床审阅通过"):
            self.api.sign_release("rule-cardio", 2, signer="signer-c",
                                  window_start=ts(1), window_end=datetime(2026, 10, 31, tzinfo=CST))

    def test_general_advice_cannot_be_escalated_to_clinical_judgement(self) -> None:
        with self.assertRaisesRegex(DomainError, "不得.*升级为疾病判断"):
            self.api.version_component(
                "bad", "stop_self_care", "general", {"conditions": ["中医建议即停药"]},
                author="author-a")
        with self.assertRaisesRegex(DomainError, "lifestyle_advice"):
            self.api.version_component(
                "bad2", "lifestyle_advice", "clinical", {"text": ["诊断性建议"]},
                author="author-a")

    # -- 版本冻结 -----------------------------------------------------------

    def test_county_keeps_frozen_version_after_new_release(self) -> None:
        self.api.record_receipt(REGION, "mosquito", "weather-station", {"density": "high"})
        self.api.record_receipt(REGION, "mosquito", "grid-cdc", {"density": "high"})
        # 上游发布 v2，县区未重新冻结 → 评估仍按 v1
        self.api.version_component("ssc-redflag", "stop_self_care", "clinical",
                                   {"conditions": ["新版红旗条件"]}, author="author-a",
                                   supersedes=1)
        slots_v2 = {**self.world.slots, "stop_self_care": ("ssc-redflag", 2)}
        self.api.desk.propose_rule("rule-cardio", GROUP, self.world.triggers, slots_v2,
                                   proposed_by="author-a")
        self.api.clinical_review("rule-cardio", 2, reviewer="clinician-b", decision="approved",
                                 expert_refs=[("exp-zhang", 1)])
        self.api.sign_release("rule-cardio", 2, signer="signer-c",
                              window_start=ts(1), window_end=datetime(2026, 10, 31, tzinfo=CST))
        prompt = self.api.advise(COUNTY, self.facts()).prompts[0]
        self.assertEqual(1, prompt.rule_version)
        self.assertEqual(["ssc-redflag", 1], prompt.component_versions["stop_self_care"])

    # -- 生效窗由可控时钟判定 -----------------------------------------------

    def test_effective_window_gates_by_clock(self) -> None:
        before = self.api.advise(COUNTY, self.facts(), at=datetime(2026, 8, 31, tzinfo=CST))
        self.assertEqual("not_yet_effective", before.skipped[0].code)
        after = self.api.advise(COUNTY, self.facts(), at=datetime(2026, 11, 1, tzinfo=CST))
        self.assertEqual("expired", after.skipped[0].code)

    # -- 回执冲突停推 -------------------------------------------------------

    def test_conflicting_receipts_suspend_push_until_resolved(self) -> None:
        self.api.record_receipt(REGION, "mosquito", "weather-station", {"density": "high"})
        self.api.record_receipt(REGION, "mosquito", "grid-cdc", {"density": "high"})
        self.assertEqual(1, len(self.api.advise(COUNTY, self.facts()).prompts))
        # 第三渠道回报冲突事实
        self.api.record_receipt(REGION, "mosquito", "festival-app", {"density": "low"})
        result = self.api.advise(COUNTY, self.facts())
        self.assertEqual((), result.prompts)
        self.assertEqual("push_suspended", result.skipped[0].code)
        with self.assertRaisesRegex(DomainError, "自动推送已停止"):
            self.api.prepare_message("msg-blocked", COUNTY, REGION, "rule-cardio", basis={})
        # 人工核定后恢复
        self.api.resolve_conflict(REGION, "mosquito", {"density": "high"}, resolved_by="duty-officer")
        self.assertEqual(1, len(self.api.advise(COUNTY, self.facts()).prompts))

    # -- 消息与纠正 ---------------------------------------------------------

    def _prepare_and_deliver_pair(self) -> tuple[str, str]:
        self.api.record_receipt(REGION, "mosquito", "weather-station", {"density": "high"})
        self.api.record_receipt(REGION, "mosquito", "grid-cdc", {"density": "high"})
        matched = self.api.advise(COUNTY, self.facts()).prompts[0].matched
        self.api.prepare_message("msg-unsent", COUNTY, REGION, "rule-cardio", basis=matched)
        self.api.prepare_message("msg-sent", COUNTY, REGION, "rule-cardio", basis=matched)
        self.api.deliver_message("msg-sent")
        return "msg-unsent", "msg-sent"

    def test_correction_revokes_unsent_and_obligates_delivered(self) -> None:
        unsent, sent = self._prepare_and_deliver_pair()
        due = T0 + timedelta(hours=12)
        self.api.raise_correction(
            "corr-1", ["rel-rule-cardio-v1"], raised_by="source-editor",
            scope_note="转介阈值依据更正：气促标准由专家组 2026-09-24 勘误", due_at=due)
        self.assertEqual("revoked", self.api.state.messages[unsent].status)
        # 已送达消息保留原文
        self.assertEqual("delivered", self.api.state.messages[sent].status)
        corr = self.api.state.corrections["corr-1"]
        self.assertEqual((unsent,), corr.revoked_messages)
        self.assertEqual([sent], list(corr.obligations))
        # 旧版发布不再允许推送
        with self.assertRaisesRegex(DomainError, "来源更正"):
            self.api.prepare_message("msg-x", COUNTY, REGION, "rule-cardio", basis={})

    def test_correction_due_date_tracked_by_clock_and_fulfilled(self) -> None:
        _, sent = self._prepare_and_deliver_pair()
        self.api.raise_correction(
            "corr-1", ["rel-rule-cardio-v1"], raised_by="source-editor",
            scope_note="依据勘误", due_at=T0 + timedelta(hours=12))
        self.assertEqual([], self.api.overdue_corrections())
        self.api.clock.set(T0 + timedelta(hours=13))
        self.assertEqual(["corr-1"], self.api.overdue_corrections())
        self.api.fulfill_correction("corr-1", [sent])
        self.assertEqual([], self.api.overdue_corrections())

    # -- 审计依据 -----------------------------------------------------------

    def test_audit_explains_message_with_expert_and_versions(self) -> None:
        self.api.record_receipt(REGION, "mosquito", "weather-station", {"density": "high"})
        self.api.record_receipt(REGION, "mosquito", "grid-cdc", {"density": "high"})
        matched = self.api.advise(COUNTY, self.facts()).prompts[0].matched
        self.api.prepare_message("msg-1", COUNTY, REGION, "rule-cardio", basis=matched)
        self.api.deliver_message("msg-1")
        audit = self.api.audit_message("msg-1")
        why = audit["why_appeared"]
        self.assertEqual(1, why["frozen_rule_version"])
        self.assertEqual("rel-rule-cardio-v1", why["release_id"])
        self.assertEqual("mosquito", why["prepared_basis"]["exposures"][0])
        prov = audit["provenance"]
        self.assertEqual("author-a", prov["proposed_by"])
        self.assertEqual("clinician-b", prov["reviewed_by"])
        self.assertEqual("signer-c", prov["signed_by"])
        self.assertEqual("approved", prov["review_decision"])
        expert = prov["expert_sources"][0]
        self.assertEqual(("exp-zhang", 1), (expert["expert_source_id"], expert["version"]))
        self.assertEqual("张医生", expert["name"])
        self.assertEqual(1, prov["components"]["stop_self_care"]["version"])

    def test_audit_unknown_message(self) -> None:
        self.assertFalse(self.api.audit_message("nope")["found"])

    # -- 重启续算与幂等 -----------------------------------------------------

    def test_restart_replays_state_and_continues_versions(self) -> None:
        self.api.record_context(REGION, "wet_then_dry", {"humidity_pct": 82})
        path = self.api.store.path
        before_events = len(self.api.state.events)
        # 重放同一事件流必须幂等
        self.api.desk.load(self.api.store.read_all())
        self.assertEqual(before_events, len(self.api.state.events))

        restarted = make_api(str(path))
        self.assertEqual(before_events, len(restarted.state.events))
        self.assertEqual(1, restarted.state.rules["rule-cardio"][1].version)
        self.assertEqual(1, restarted.state.adoptions[(COUNTY, "rule-cardio")])
        # 重启后继续工作：同一 context 聚合的版本号顺延
        event = restarted.record_context(REGION, "wet_then_dry", {"humidity_pct": 79})
        self.assertEqual(2, event["version"])


if __name__ == "__main__":
    unittest.main()
