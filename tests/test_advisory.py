import unittest

import support


class AdvisoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = support.make_service()

    def tearDown(self) -> None:
        self.service.close()

    def test_prompts_only_for_matching_audience(self) -> None:
        support.make_clinical_release(self.service)
        prompts = self.service.current_prompts(support.REGION, support.ELDERLY, facts={"pollen_index": 4})
        self.assertEqual(["life_advice"], [p.kind for p in prompts])
        self.assertEqual([], self.service.current_prompts(support.REGION, support.CHILD, facts={"pollen_index": 4}))

    def test_prompts_only_when_trigger_matches(self) -> None:
        support.make_clinical_release(self.service)
        self.assertEqual([], self.service.current_prompts(support.REGION, support.ELDERLY, facts={"pollen_index": 1}))
        self.assertEqual([], self.service.current_prompts(support.REGION, support.ELDERLY, facts={}))

    def test_stop_condition_takes_precedence_over_life_advice(self) -> None:
        support.make_clinical_release(self.service)
        prompts = self.service.current_prompts(
            support.REGION, support.ELDERLY, facts={"pollen_index": 4, "symptoms": ["胸闷"]}
        )
        self.assertEqual({"stop_self_care", "referral"}, {p.kind for p in prompts})
        referral = [p for p in prompts if p.kind == "referral"][0]
        self.assertEqual("county_hospital", referral.basis["detail"]["level"])
        reasons = [d.reason for d in self.service.decisions]
        self.assertIn("overridden_by_stop_condition", reasons)

    def test_tcm_wellness_never_escalates_to_diagnosis(self) -> None:
        support.make_tcm_release(self.service)
        # 即使同时报告了症状，中医调养建议也只以非诊断生活建议出现
        prompts = self.service.current_prompts(
            support.REGION, support.ELDERLY, facts={"humidity_pct": 50, "symptoms": ["胸闷"]}
        )
        self.assertEqual(1, len(prompts))
        prompt = prompts[0]
        self.assertEqual("life_advice", prompt.kind)
        self.assertTrue(prompt.non_diagnostic)
        self.assertEqual("tcm_wellness", prompt.basis["detail"]["category"])

    def test_one_fact_supports_multiple_reminders(self) -> None:
        support.make_clinical_release(self.service, advice_ids=("la-mask",))
        # 第二条规则由同一事实触发，给出另一条提醒
        rule = self.service.propose_rule(
            author=support.AUTHOR,
            target_group=support.ELDERLY,
            trigger_set=[{"fact": "pollen_index", "op": ">=", "value": 3}],
            climate_phase_id="cp-autumn",
            scenario_id="sc-pollen",
            advice_ids=["la-window"],
            expert_source_id="es-wei",
        )
        self.service.clinical_review(rule.rule_id, reviewer=support.REVIEWER, approve=True)
        self.service.sign_release(rule.rule_id, publisher=support.PUBLISHER, effective_start=support.WINDOW_START)
        prompts = self.service.current_prompts(support.REGION, support.ELDERLY, facts={"pollen_index": 4})
        texts = {p.text for p in prompts}
        self.assertEqual({"花粉浓度高，外出请佩戴口罩", "关闭门窗，减少花粉入室"}, texts)

    def test_duplicate_advice_across_rules_emitted_once(self) -> None:
        support.make_clinical_release(self.service, advice_ids=("la-mask",))
        rule = self.service.propose_rule(
            author=support.AUTHOR,
            target_group=support.ELDERLY,
            trigger_set=[{"fact": "pollen_index", "op": ">=", "value": 3}],
            climate_phase_id="cp-autumn",
            scenario_id="sc-pollen",
            advice_ids=["la-mask"],
            expert_source_id="es-wei",
        )
        self.service.clinical_review(rule.rule_id, reviewer=support.REVIEWER, approve=True)
        self.service.sign_release(rule.rule_id, publisher=support.PUBLISHER, effective_start=support.WINDOW_START)
        prompts = self.service.current_prompts(support.REGION, support.ELDERLY, facts={"pollen_index": 4})
        self.assertEqual(1, len(prompts))

    def test_conflicted_exposure_facts_excluded_from_query(self) -> None:
        support.make_clinical_release(self.service)
        self.service.ingest_receipt("rc-1", "exp-1", "村级上报", support.REGION, {"pollen_index": 2})
        self.service.ingest_receipt("rc-2", "exp-1", "气象接口", support.REGION, {"pollen_index": 5})
        prompts = self.service.current_prompts(support.REGION, support.ELDERLY)
        self.assertEqual([], prompts)
        held = [d for d in self.service.decisions if d.outcome == "held"]
        self.assertTrue(held)


if __name__ == "__main__":
    unittest.main()
