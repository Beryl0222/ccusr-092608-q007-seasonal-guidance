import unittest

import support

DUE = "2026-09-30T18:00:00+08:00"


class CorrectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = support.make_service()
        _, self.release = support.make_clinical_release(self.service)
        support.adopt(self.service, self.release)

    def tearDown(self) -> None:
        self.service.close()

    def _queue_two_batches(self):
        # 第一批：普通生活建议，发出并送达；第二批：停止自我处置 + 转介，仍排队
        self.service.ingest_receipt("rc-1", "exp-1", "村级上报", support.REGION, {"pollen_index": 4})
        self.service.dispatch()
        self.service.confirm_delivery("msg-0001")
        self.service.ingest_receipt(
            "rc-2", "exp-2", "气象接口", support.REGION, {"pollen_index": 5, "symptoms": ["胸闷"]}
        )

    def test_correction_retracts_only_unsent_messages(self) -> None:
        self._queue_two_batches()
        self.assertEqual(3, len(self.service.messages))
        first = self.service.messages[0]
        obligation = self.service.raise_correction([self.release.release_id], "花粉阈值引用过时", DUE)
        statuses = {m.message_id: m.status for m in self.service.messages}
        self.assertEqual("delivered", statuses[first.message_id])
        retracted = [s for m, s in statuses.items() if m != first.message_id]
        self.assertEqual(["retracted", "retracted"], retracted)
        self.assertEqual([first.message_id], obligation.scope["message_ids"])
        self.assertEqual([support.COUNTY], obligation.scope["counties"])
        self.assertEqual([support.ELDERLY], obligation.scope["audience_groups"])

    def test_delivered_message_keeps_original_text(self) -> None:
        self._queue_two_batches()
        first = self.service.messages[0]
        original_text = first.text
        self.service.raise_correction([self.release.release_id], "来源更正", DUE)
        self.assertEqual(original_text, self.service.message(first.message_id).text)

    def test_correction_event_carries_scope_and_due(self) -> None:
        self._queue_two_batches()
        self.service.raise_correction([self.release.release_id], "来源更正", DUE, note="阈值由 3 调整为 4")
        event = [e for e in self.service.events if e["event_type"] == "CORRECTION_RAISED"][0]
        self.assertEqual([self.release.release_id], event["payload"]["affected_releases"])
        self.assertEqual(DUE, event["payload"]["due_at"])
        self.assertEqual("阈值由 3 调整为 4", event["payload"]["scope"]["note"])

    def test_corrected_release_stops_emitting(self) -> None:
        self.service.raise_correction([self.release.release_id], "来源更正", DUE)
        prompts = self.service.current_prompts(support.REGION, support.ELDERLY, facts={"pollen_index": 4})
        self.assertEqual([], prompts)
        reasons = {d.reason for d in self.service.decisions}
        self.assertIn("release_not_active", reasons)

    def test_obligation_deadline_processed_by_clock(self) -> None:
        self._queue_two_batches()
        obligation = self.service.raise_correction([self.release.release_id], "来源更正", DUE)
        self.assertEqual("open", obligation.status)
        self.service.clock.advance(days=6)
        changed = self.service.refresh_obligations()
        self.assertEqual([obligation.obligation_id], [o.obligation_id for o in changed])
        self.assertEqual("overdue", self.service.obligations[0].status)

    def test_fulfill_before_due(self) -> None:
        self._queue_two_batches()
        obligation = self.service.raise_correction([self.release.release_id], "来源更正", DUE)
        self.service.fulfill_correction(obligation.obligation_id, note="已电话逐户告知")
        self.service.clock.advance(days=6)
        self.assertEqual([], self.service.refresh_obligations())
        self.assertEqual("fulfilled", self.service.obligations[0].status)


if __name__ == "__main__":
    unittest.main()
