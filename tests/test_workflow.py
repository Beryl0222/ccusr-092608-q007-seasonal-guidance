import unittest

import support
from seasonal_guidance.service import IndependenceError, ServiceError, WorkflowError


class WorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = support.make_service()

    def tearDown(self) -> None:
        self.service.close()

    def test_full_flow_emits_contract_events_with_incrementing_versions(self) -> None:
        rule, release = support.make_clinical_release(self.service)
        self.assertEqual(1, rule.version)
        self.assertEqual(f"{rule.rule_id}-r1", release.release_id)
        types = [e["event_type"] for e in self.service.events]
        self.assertEqual(["RULE_PROPOSED", "CLINICAL_REVIEWED", "RELEASE_SIGNED"], types)
        rule_events = [e for e in self.service.events if e["aggregate_type"] == "guidance_rule"]
        self.assertEqual([1, 2], [e["version"] for e in rule_events])
        signed = [e for e in self.service.events if e["event_type"] == "RELEASE_SIGNED"][0]
        self.assertEqual(1, signed["payload"]["rule_version"])
        self.assertEqual(support.WINDOW_START, signed["payload"]["effective_window"]["start"])

    def test_author_cannot_review_own_rule(self) -> None:
        rule = support.propose_clinical_rule(self.service)
        with self.assertRaises(IndependenceError):
            self.service.clinical_review(rule.rule_id, reviewer=support.AUTHOR, approve=True)

    def test_author_cannot_sign(self) -> None:
        rule = support.propose_clinical_rule(self.service)
        self.service.clinical_review(rule.rule_id, reviewer=support.REVIEWER, approve=True)
        with self.assertRaises(IndependenceError):
            self.service.sign_release(rule.rule_id, publisher=support.AUTHOR, effective_start=support.WINDOW_START)

    def test_reviewer_cannot_sign(self) -> None:
        rule = support.propose_clinical_rule(self.service)
        self.service.clinical_review(rule.rule_id, reviewer=support.REVIEWER, approve=True)
        with self.assertRaises(IndependenceError):
            self.service.sign_release(rule.rule_id, publisher=support.REVIEWER, effective_start=support.WINDOW_START)

    def test_sign_requires_approval(self) -> None:
        rule = support.propose_clinical_rule(self.service)
        with self.assertRaises(WorkflowError):
            self.service.sign_release(rule.rule_id, publisher=support.PUBLISHER, effective_start=support.WINDOW_START)

    def test_rejected_rule_cannot_be_signed(self) -> None:
        rule = support.propose_clinical_rule(self.service)
        self.service.clinical_review(rule.rule_id, reviewer=support.REVIEWER, approve=False, note="依据不足")
        with self.assertRaises(WorkflowError):
            self.service.sign_release(rule.rule_id, publisher=support.PUBLISHER, effective_start=support.WINDOW_START)

    def test_amendment_bumps_version_and_requires_fresh_review(self) -> None:
        rule, _ = support.make_clinical_release(self.service)
        amended = support.propose_clinical_rule(self.service, advice_ids=("la-mask", "la-window"), rule_id=rule.rule_id)
        self.assertEqual(2, amended.version)
        self.assertEqual("proposed", amended.status)
        with self.assertRaises(WorkflowError):
            self.service.sign_release(rule.rule_id, publisher=support.PUBLISHER, effective_start=support.WINDOW_START)
        with self.assertRaises(IndependenceError):
            self.service.clinical_review(rule.rule_id, reviewer=support.AUTHOR, approve=True)

    def test_only_original_author_may_amend(self) -> None:
        rule, _ = support.make_clinical_release(self.service)
        with self.assertRaises(WorkflowError):
            self.service.propose_rule(
                author="别人",
                target_group=support.ELDERLY,
                trigger_set=[{"fact": "pollen_index", "op": ">=", "value": 3}],
                climate_phase_id="cp-autumn",
                scenario_id="sc-pollen",
                advice_ids=["la-mask"],
                expert_source_id="es-wei",
                rule_id=rule.rule_id,
            )

    def test_adoption_freezes_rule_version(self) -> None:
        rule, release_v1 = support.make_clinical_release(self.service)
        adoption = support.adopt(self.service, release_v1)
        self.assertEqual(1, adoption.rule_version)
        amended = support.propose_clinical_rule(self.service, advice_ids=("la-mask", "la-window"), rule_id=rule.rule_id)
        self.service.clinical_review(amended.rule_id, reviewer=support.REVIEWER, approve=True)
        release_v2 = self.service.sign_release(
            amended.rule_id, publisher=support.PUBLISHER, effective_start=support.WINDOW_START
        )
        self.assertEqual(2, release_v2.rule_version)
        prompts = self.service.current_prompts(
            support.REGION, support.ELDERLY, county=support.COUNTY, facts={"pollen_index": 4}
        )
        self.assertTrue(prompts)
        self.assertTrue(all(p.release_id == release_v1.release_id for p in prompts))
        self.assertEqual(["la-mask"], [p.basis["content"][0]["item_id"] for p in prompts])

    def test_stop_condition_and_referral_must_pair(self) -> None:
        with self.assertRaises(ServiceError):
            self.service.propose_rule(
                author=support.AUTHOR,
                target_group=support.ELDERLY,
                trigger_set=[{"fact": "pollen_index", "op": ">=", "value": 3}],
                climate_phase_id="cp-autumn",
                scenario_id="sc-pollen",
                advice_ids=["la-mask"],
                expert_source_id="es-wei",
                stop_condition_id="stop-resp",
            )

    def test_unknown_content_reference_rejected(self) -> None:
        with self.assertRaises(ServiceError):
            self.service.propose_rule(
                author=support.AUTHOR,
                target_group=support.ELDERLY,
                trigger_set=[{"fact": "pollen_index", "op": ">=", "value": 3}],
                climate_phase_id="cp-autumn",
                scenario_id="sc-pollen",
                advice_ids=["la-missing"],
                expert_source_id="es-wei",
            )


if __name__ == "__main__":
    unittest.main()
