import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import support
from seasonal_guidance.clock import ManualClock
from seasonal_guidance.service import IssuanceService

TZ = timezone(timedelta(hours=8))


class ClockWindowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = ManualClock(datetime(2026, 9, 15, 10, 0, tzinfo=TZ))
        self.service = support.make_service(clock=self.clock)
        _, self.release = support.make_clinical_release(self.service)
        support.adopt(self.service, self.release)

    def tearDown(self) -> None:
        self.service.close()

    def _prompts(self):
        return self.service.current_prompts(support.REGION, support.ELDERLY, facts={"pollen_index": 4})

    def test_alert_not_active_before_window(self) -> None:
        self.assertEqual([], self._prompts())
        reasons = {d.reason for d in self.service.decisions}
        self.assertIn("outside_effective_window", reasons)

    def test_alert_active_inside_window(self) -> None:
        self.clock.set(datetime(2026, 9, 25, 10, 0, tzinfo=TZ))
        self.assertTrue(self._prompts())

    def test_alert_expires_after_window(self) -> None:
        self.clock.set(datetime(2026, 11, 1, 10, 0, tzinfo=TZ))
        self.assertEqual([], self._prompts())


class RestartTests(unittest.TestCase):
    def test_restart_continues_from_journal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "journal.jsonl"
            clock = ManualClock(support.START)
            service = support.make_service(journal=journal, clock=clock)
            _, release = support.make_clinical_release(service)
            support.adopt(service, release)
            service.ingest_receipt("rc-1", "exp-1", "村级上报", support.REGION, {"pollen_index": 4})
            service.raise_correction([release.release_id], "来源更正", "2026-09-30T18:00:00+08:00")
            event_count = len(service.events)
            service.close()

            # 重启：新实例读取同一日志，状态与期限处理继续
            restored = support.make_service(journal=journal, clock=clock, register_base=False)
            self.assertEqual(event_count, len(restored.events))
            self.assertEqual(1, len(restored.exposures))
            self.assertEqual(1, len(restored.obligations))
            message = restored.messages[0]
            self.assertEqual("retracted", message.status)
            clock.advance(days=6)
            changed = restored.refresh_obligations()
            self.assertEqual(1, len(changed))
            self.assertEqual("overdue", restored.obligations[0].status)

            # 事件版本在重启后继续递增，不回头
            restored.record_context(support.REGION, {"humidity_pct": 55})
            last = restored.events[-1]
            self.assertEqual(event_count + 1, len(restored.events))
            self.assertEqual(1, last["version"])
            self.assertEqual("CONTEXT_RECORDED", last["event_type"])
            restored.close()

    def test_restart_keeps_delivered_messages_and_prompts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "journal.jsonl"
            clock = ManualClock(support.START)
            service = support.make_service(journal=journal, clock=clock)
            _, release = support.make_clinical_release(service)
            support.adopt(service, release)
            service.ingest_receipt("rc-1", "exp-1", "村级上报", support.REGION, {"pollen_index": 4})
            service.dispatch()
            service.confirm_delivery("msg-0001")
            service.close()

            restored = support.make_service(journal=journal, clock=clock, register_base=False)
            self.assertEqual("delivered", restored.messages[0].status)
            prompts = restored.current_prompts(support.REGION, support.ELDERLY, county=support.COUNTY)
            self.assertTrue(prompts)
            restored.close()


if __name__ == "__main__":
    unittest.main()
