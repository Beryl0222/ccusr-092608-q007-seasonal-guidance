"""应用门面：把签发台、可控时钟、事件存储、评估与审计组装为可重启的服务。

写操作以事件为单位：领域校验通过后一次性落盘；重启时重放全部事件续算，
已见 event_id 幂等跳过。
"""

from __future__ import annotations

from datetime import datetime
from typing import Mapping

from .clock import Clock
from .evaluator import Evaluator, EvaluationResult
from .model import Facts, State
from .service import RuleDesk
from .store import EventStore


class GuidanceApi:
    def __init__(self, clock: Clock, store: EventStore | str, *,
                 actors: Mapping[str, Mapping[str, object]] | None = None,
                 schema: Mapping[str, object] | None = None) -> None:
        self.clock = clock
        self.store = store if isinstance(store, EventStore) else EventStore(store)
        self.state = State()
        self.desk = RuleDesk(clock, self.state, actors=actors, schema=schema)
        # 重启后继续：重放历史事件还原全部状态
        self.desk.load(self.store.read_all())
        self.evaluator = Evaluator(self.state, clock)

    # -- 持久化 -------------------------------------------------------------

    def _commit(self) -> list[dict]:
        events = self.desk.drain_events()
        if events:
            self.store.append(events)
        return events

    def __enter__(self) -> "GuidanceApi":
        return self

    def __exit__(self, *exc) -> None:
        if exc[0] is None:
            self._commit()

    # -- 写入委托（每个方法在成功后落盘）------------------------------------

    def record_context(self, *args, **kwargs):
        result = self.desk.record_context(*args, **kwargs)
        self._commit()
        return result

    def version_component(self, *args, **kwargs):
        result = self.desk.version_component(*args, **kwargs)
        self._commit()
        return result

    def propose_rule(self, *args, **kwargs):
        result = self.desk.propose_rule(*args, **kwargs)
        self._commit()
        return result

    def clinical_review(self, *args, **kwargs):
        result = self.desk.clinical_review(*args, **kwargs)
        self._commit()
        return result

    def sign_release(self, *args, **kwargs):
        result = self.desk.sign_release(*args, **kwargs)
        self._commit()
        return result

    def freeze_adoption(self, *args, **kwargs):
        result = self.desk.freeze_adoption(*args, **kwargs)
        self._commit()
        return result

    def record_receipt(self, *args, **kwargs):
        result = self.desk.record_receipt(*args, **kwargs)
        self._commit()
        return result

    def resolve_conflict(self, *args, **kwargs):
        result = self.desk.resolve_conflict(*args, **kwargs)
        self._commit()
        return result

    def prepare_message(self, *args, **kwargs):
        result = self.desk.prepare_message(*args, **kwargs)
        self._commit()
        return result

    def deliver_message(self, *args, **kwargs):
        result = self.desk.deliver_message(*args, **kwargs)
        self._commit()
        return result

    def raise_correction(self, *args, **kwargs):
        result = self.desk.raise_correction(*args, **kwargs)
        self._commit()
        return result

    def fulfill_correction(self, *args, **kwargs):
        result = self.desk.fulfill_correction(*args, **kwargs)
        self._commit()
        return result

    # -- 评估 API：必要且克制的行动提示 -------------------------------------

    def advise(self, county_id: str, facts: Facts, *, at: datetime | None = None) -> EvaluationResult:
        return self.evaluator.evaluate(county_id, facts, at=at)

    # -- 审计端 -------------------------------------------------------------

    def audit_message(self, message_id: str) -> dict:
        """解释一条已产生的消息为何出现、依据哪位专家哪版内容。"""
        message = self.state.messages.get(message_id)
        if message is None:
            return {"message_id": message_id, "found": False,
                    "why": "消息不存在，因此从未出现"}
        rule = self.state.rules[message.rule_id][message.rule_version]
        experts = []
        for cid, cver in rule.reviewed_expert_refs:
            comp = self.state.component(cid, cver)
            experts.append({"expert_source_id": cid, "version": cver, **comp.body})
        components = {}
        for kind, (cid, cver) in sorted(message.component_refs.items()):
            comp = self.state.component(cid, cver)
            components[kind] = {"component_id": cid, "version": cver,
                                "classification": comp.classification, "body": comp.body}
        corrections = [cid for cid, c in self.state.corrections.items()
                       if message_id in c.obligations or message_id in c.revoked_messages]
        return {
            "message_id": message_id, "found": True,
            "status": message.status,
            "why_appeared": {
                "county_id": message.county_id,
                "frozen_rule_version": message.rule_version,
                "release_id": message.release_id,
                "prepared_basis": _basis_of(self.state, message_id),
                "effective_window": {
                    "start": rule.effective_start.isoformat() if rule.effective_start else None,
                    "end": rule.effective_end.isoformat() if rule.effective_end else None},
                "prepared_at": message.prepared_at.isoformat(),
                "delivered_at": message.delivered_at.isoformat() if message.delivered_at else None,
            },
            "provenance": {
                "rule_id": message.rule_id, "rule_version": message.rule_version,
                "proposed_by": rule.proposed_by,
                "reviewed_by": rule.reviewed_by,
                "review_decision": rule.review_decision,
                "signed_by": rule.signed_by,
                "expert_sources": experts,
                "components": components,
            },
            "corrections": corrections,
            "revoke_reason": message.revoke_reason,
        }

    def audit_absent(self, county_id: str, facts: Facts, *, at: datetime | None = None) -> dict:
        """解释在给定地区/人群/当下事实下，消息为何没有出现。"""
        result = self.advise(county_id, facts, at=at)
        return {
            "at": result.at.isoformat(),
            "county_id": county_id,
            "emitted": [p.rule_id for p in result.prompts],
            "not_emitted": [
                {"rule_id": s.rule_id, "rule_version": s.rule_version,
                 "reason_code": s.code, "detail": s.detail} for s in result.skipped],
        }

    def overdue_corrections(self, *, at: datetime | None = None) -> list[str]:
        return self.desk.overdue_corrections(at=at)


def _basis_of(state: State, message_id: str) -> dict:
    """从 MESSAGE_PREPARED 原始事件取出当时记录的命中依据。"""
    for event in reversed(state.events):
        if event["event_type"] == "MESSAGE_PREPARED" and event["aggregate_id"] == message_id:
            return dict(event["payload"].get("basis", {}))
    return {}
