import unittest

import support


class ReceiptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = support.make_service()
        _, self.release = support.make_clinical_release(self.service)
        support.adopt(self.service, self.release)

    def tearDown(self) -> None:
        self.service.close()

    def test_same_exposure_from_two_channels_merges(self) -> None:
        self.service.ingest_receipt("rc-1", "exp-1", "村级上报", support.REGION, {"pollen_index": 4})
        record = self.service.ingest_receipt("rc-2", "exp-1", "气象接口", support.REGION, {"pollen_index": 4})
        self.assertEqual("merged", record.status)
        self.assertEqual(["村级上报", "气象接口"], record.channels)
        self.assertEqual(["rc-1", "rc-2"], record.receipt_ids)
        # 合并后同一提醒只排队一次
        queued = [m for m in self.service.messages if m.status == "queued"]
        self.assertEqual(1, len(queued))
        self.assertEqual("life_advice", queued[0].kind)

    def test_conflicting_receipts_halt_auto_push(self) -> None:
        self.service.ingest_receipt("rc-1", "exp-1", "村级上报", support.REGION, {"pollen_index": 2})
        record = self.service.ingest_receipt("rc-2", "exp-1", "气象接口", support.REGION, {"pollen_index": 5})
        self.assertEqual("conflicted", record.status)
        self.assertEqual([2, 5], record.conflicts["pollen_index"])
        self.assertEqual([], self.service.messages)
        held = [d for d in self.service.decisions if d.outcome == "held" and d.reason == "exposure_conflicted"]
        self.assertTrue(held)

    def test_resolve_conflict_resumes_auto_push(self) -> None:
        self.service.ingest_receipt("rc-1", "exp-1", "村级上报", support.REGION, {"pollen_index": 2})
        self.service.ingest_receipt("rc-2", "exp-1", "气象接口", support.REGION, {"pollen_index": 5})
        record = self.service.resolve_conflict("exp-1", {"pollen_index": 5}, resolver="值班专家")
        self.assertEqual("merged", record.status)
        self.assertEqual(5, record.facts["pollen_index"])
        queued = [m for m in self.service.messages if m.status == "queued"]
        self.assertEqual(1, len(queued))

    def test_duplicate_receipt_rejected(self) -> None:
        self.service.ingest_receipt("rc-1", "exp-1", "村级上报", support.REGION, {"pollen_index": 4})
        with self.assertRaises(Exception):
            self.service.ingest_receipt("rc-1", "exp-1", "村级上报", support.REGION, {"pollen_index": 4})

    def test_stop_condition_message_from_exposure_facts(self) -> None:
        self.service.ingest_receipt(
            "rc-1", "exp-1", "村级上报", support.REGION, {"pollen_index": 4, "symptoms": ["胸闷"]}
        )
        kinds = {m.kind for m in self.service.messages}
        self.assertEqual({"stop_self_care", "referral"}, kinds)


if __name__ == "__main__":
    unittest.main()
