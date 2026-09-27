import unittest

import support
from seasonal_guidance import audit


class AuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = support.make_service()
        _, self.release = support.make_clinical_release(self.service)
        support.adopt(self.service, self.release)

    def tearDown(self) -> None:
        self.service.close()

    def test_explain_message_why_it_appeared(self) -> None:
        self.service.ingest_receipt("rc-1", "exp-1", "村级上报", support.REGION, {"pollen_index": 4})
        message = self.service.messages[0]
        explanation = audit.explain_message(self.service, message.message_id)
        self.assertEqual("queued", explanation["status"])
        self.assertEqual(self.release.release_id, explanation["release"]["release_id"])
        self.assertEqual(1, explanation["release"]["rule_version"])
        self.assertEqual("签发丙", explanation["release"]["signed_by"])
        expert = explanation["expert_source"]
        self.assertEqual("韦医师", expert["expert"])
        self.assertEqual(1, expert["version"])
        self.assertEqual("es-wei", expert["item_id"])
        kinds = {c["kind"] for c in explanation["content_basis"]}
        self.assertIn("life_advice", kinds)

    def test_explain_absence_lists_suppression_reasons(self) -> None:
        self.service.current_prompts(support.REGION, support.CHILD, facts={"pollen_index": 4})
        absence = audit.explain_absence(self.service, support.REGION, support.CHILD)
        reasons = {entry["reason"] for entry in absence}
        self.assertIn("audience_mismatch", reasons)

    def test_explain_absence_before_effective_window(self) -> None:
        self.service.clock.set(support.datetime(2026, 9, 1, 10, 0, tzinfo=support.TZ))
        self.service.current_prompts(support.REGION, support.ELDERLY, facts={"pollen_index": 4})
        absence = audit.explain_absence(self.service, support.REGION, support.ELDERLY)
        reasons = {entry["reason"] for entry in absence}
        self.assertIn("outside_effective_window", reasons)

    def test_expert_source_version_is_frozen_in_release(self) -> None:
        # 签发后专家来源更新到 v2，既有签发版本仍引用 v1
        self.service.register_content(
            "expert_source", "es-wei", expert="韦医师", organization="自治区疾控中心", title="秋季呼吸道风险研判（修订）"
        )
        self.service.ingest_receipt("rc-1", "exp-1", "村级上报", support.REGION, {"pollen_index": 4})
        message = self.service.messages[0]
        explanation = audit.explain_message(self.service, message.message_id)
        self.assertEqual(1, explanation["expert_source"]["version"])
        # 新签发的版本则引用 v2
        rule = support.propose_clinical_rule(self.service)
        self.service.clinical_review(rule.rule_id, reviewer=support.REVIEWER, approve=True)
        release_v2 = self.service.sign_release(rule.rule_id, publisher=support.PUBLISHER, effective_start=support.WINDOW_START)
        snapshot_source = release_v2.content_snapshot["expert_source"]
        self.assertEqual(2, snapshot_source["version"])

    def test_emitted_decision_records_carry_basis(self) -> None:
        self.service.current_prompts(support.REGION, support.ELDERLY, facts={"pollen_index": 4})
        emitted = audit.explain_decisions(self.service, region=support.REGION, outcome="emitted")
        self.assertTrue(emitted)
        self.assertEqual("韦医师", emitted[0]["expert_source"]["expert"])
        self.assertEqual(self.release.release_id, emitted[0]["release"]["release_id"])


if __name__ == "__main__":
    unittest.main()
