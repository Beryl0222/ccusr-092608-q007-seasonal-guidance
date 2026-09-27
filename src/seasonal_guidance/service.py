"""秋季健康规则签发台服务。

职责边界：
- 规则作者只能提出内容；临床审阅与发布签发相互独立，三者身份不得重叠；
- 县区采用规则时冻结版本，之后的签发不影响已采用的县；
- 来源更正仅撤回尚未发送的内容；已送达消息保留原文，并生成范围明确的纠正义务；
- 合并来自不同渠道的同一暴露回执，内容冲突时停止自动推送；
- 以可控时钟处理预警生效、过期与纠正期限；所有状态写入日志，重启后继续。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from .advisory import dedupe_prompts, evaluate_release
from .clock import Clock, SystemClock
from .contracts import validate_event
from .models import (
    ADVICE_CATEGORIES,
    CONTENT_KINDS,
    CONTENT_REQUIRED_FIELDS,
    CLIMATE_PHASE,
    EXPOSURE_CONFLICTED,
    EXPOSURE_MERGED,
    EXPOSURE_SCENARIO,
    EXPERT_SOURCE,
    LIFE_ADVICE,
    MESSAGE_DELIVERED,
    MESSAGE_QUEUED,
    MESSAGE_RETRACTED,
    MESSAGE_SENT,
    OBLIGATION_FULFILLED,
    OBLIGATION_OPEN,
    OBLIGATION_OVERDUE,
    RISK_GROUP,
    RULE_APPROVED,
    RULE_PROPOSED,
    RULE_REJECTED,
    STOP_CONDITION,
    SYMPTOM_SET,
    Adoption,
    ContentItem,
    CorrectionObligation,
    DecisionRecord,
    ExposureRecord,
    GuidanceRule,
    Message,
    Prompt,
    ReleaseRevision,
    TimeWindow,
    parse_instant,
)


class ServiceError(Exception):
    """签发台服务的基础异常。"""


class UnknownAggregateError(ServiceError):
    """引用了不存在的对象。"""


class WorkflowError(ServiceError):
    """状态机不允许的操作。"""


class IndependenceError(WorkflowError):
    """作者、临床审阅与签发身份必须相互独立。"""


class ConflictStateError(ServiceError):
    """暴露回执存在冲突，需先解决。"""


def _require(condition: bool, error: type[ServiceError], message: str) -> None:
    if not condition:
        raise error(message)


class IssuanceService:
    """规则签发台：内容版本化、签发工作流、暴露合并、纠正义务与审计。"""

    def __init__(
        self,
        journal_path: str | Path | None = None,
        clock: Clock | None = None,
        schema: Mapping[str, Any] | None = None,
    ):
        self.clock: Clock = clock if clock is not None else SystemClock()
        self.schema = schema
        self._contents: dict[str, dict[str, list[ContentItem]]] = {kind: {} for kind in CONTENT_KINDS}
        self._rules: dict[str, GuidanceRule] = {}
        self._releases: dict[str, ReleaseRevision] = {}
        self._adoptions: dict[tuple[str, str], Adoption] = {}
        self._exposures: dict[str, ExposureRecord] = {}
        self._messages: dict[str, Message] = {}
        self._obligations: dict[str, CorrectionObligation] = {}
        self._region_facts: dict[str, dict[str, Any]] = {}
        self._decisions: list[DecisionRecord] = []
        self._events: list[dict[str, Any]] = []
        self._aggregate_versions: dict[str, int] = {}
        self._journal_path = Path(journal_path) if journal_path else None
        self._journal_fh = None
        if self._journal_path and self._journal_path.exists():
            self._replay()
        if self._journal_path:
            self._journal_fh = self._journal_path.open("a", encoding="utf-8")

    # ------------------------------------------------------------------
    # 日志与重放
    # ------------------------------------------------------------------

    def close(self) -> None:
        if self._journal_fh:
            self._journal_fh.close()
            self._journal_fh = None

    def __enter__(self) -> "IssuanceService":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _append(self, kind: str, data: Mapping[str, Any]) -> None:
        if self._journal_fh:
            self._journal_fh.write(json.dumps({"kind": kind, "data": data}, ensure_ascii=False) + "\n")
            self._journal_fh.flush()

    def _replay(self) -> None:
        assert self._journal_path is not None
        for line in self._journal_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            self._apply(record["kind"], record["data"])

    def _apply(self, kind: str, data: Mapping[str, Any]) -> None:
        if kind == "event":
            envelope = dict(data)
            self._events.append(envelope)
            key = f"{envelope['aggregate_type']}:{envelope['aggregate_id']}"
            self._aggregate_versions[key] = int(envelope["version"])
        elif kind == "content":
            item = ContentItem.from_dict(data)
            self._contents.setdefault(item.kind, {}).setdefault(item.item_id, []).append(item)
        elif kind == "rule":
            rule = GuidanceRule.from_dict(data)
            self._rules[rule.rule_id] = rule
        elif kind == "release":
            release = ReleaseRevision.from_dict(data)
            self._releases[release.release_id] = release
        elif kind == "adoption":
            adoption = Adoption.from_dict(data)
            self._adoptions[(adoption.county, adoption.release_id)] = adoption
        elif kind == "exposure":
            record = ExposureRecord.from_dict(data)
            self._exposures[record.exposure_key] = record
        elif kind == "message":
            message = Message.from_dict(data)
            self._messages[message.message_id] = message
        elif kind == "obligation":
            obligation = CorrectionObligation.from_dict(data)
            self._obligations[obligation.obligation_id] = obligation
        elif kind == "facts":
            self._region_facts.setdefault(str(data["region"]), {}).update(data.get("facts", {}))
        elif kind == "decision":
            self._decisions.append(DecisionRecord.from_dict(data))
        else:
            raise ServiceError(f"日志中存在未知记录类别: {kind!r}")

    def _store(self, kind: str, data: Mapping[str, Any]) -> None:
        self._append(kind, data)
        self._apply(kind, data)

    def _emit(self, event_type: str, aggregate_type: str, aggregate_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        key = f"{aggregate_type}:{aggregate_id}"
        version = self._aggregate_versions.get(key, 0) + 1
        envelope = {
            "event_id": f"evt-{len(self._events) + 1:06d}",
            "event_type": event_type,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "occurred_at": self.clock.now().isoformat(),
            "version": version,
            "payload": dict(payload),
        }
        if self.schema is not None:
            issues = validate_event(envelope, self.schema)
            _require(not issues, ServiceError, f"领域事件未通过契约校验: {[i.code for i in issues]}")
        self._store("event", envelope)
        return envelope

    # ------------------------------------------------------------------
    # 内容登记：各类别分别版本化
    # ------------------------------------------------------------------

    def register_content(self, kind: str, item_id: str, **fields: Any) -> ContentItem:
        _require(kind in CONTENT_KINDS, ServiceError, f"未知内容类别: {kind!r}")
        missing = [name for name in CONTENT_REQUIRED_FIELDS[kind] if name not in fields]
        _require(not missing, ServiceError, f"内容缺少必填属性: {missing}")
        if kind == LIFE_ADVICE:
            _require(fields["category"] in ADVICE_CATEGORIES, ServiceError, f"生活建议类别未登记: {fields['category']!r}")
        if kind == STOP_CONDITION:
            fields.setdefault("match", "any")
            _require(fields["match"] in ("any", "all"), ServiceError, "停止条件匹配方式只能是 any 或 all")
        if kind == CLIMATE_PHASE:
            window = fields["window"]
            parse_instant(window["start"])
            if window.get("end"):
                parse_instant(window["end"])
        history = self._contents[kind].setdefault(item_id, [])
        item = ContentItem(kind=kind, item_id=item_id, version=len(history) + 1, fields=dict(fields))
        self._store("content", item.to_dict())
        return item

    def content(self, kind: str, item_id: str, version: int | None = None) -> ContentItem:
        history = self._contents.get(kind, {}).get(item_id) or []
        _require(bool(history), UnknownAggregateError, f"内容不存在: {kind}/{item_id}")
        if version is None:
            return history[-1]
        for item in history:
            if item.version == version:
                return item
        raise UnknownAggregateError(f"内容版本不存在: {kind}/{item_id}@v{version}")

    # ------------------------------------------------------------------
    # 签发工作流：作者提出、临床审阅、发布签发相互独立
    # ------------------------------------------------------------------

    def propose_rule(
        self,
        author: str,
        target_group: str,
        trigger_set: list[dict[str, Any]],
        climate_phase_id: str,
        scenario_id: str,
        advice_ids: list[str],
        expert_source_id: str,
        stop_condition_id: str | None = None,
        referral_level_id: str | None = None,
        rule_id: str | None = None,
    ) -> GuidanceRule:
        _require(bool(author.strip()), ServiceError, "作者不能为空")
        _require(bool(trigger_set), ServiceError, "触发条件不能为空")
        _require(bool(advice_ids), ServiceError, "至少引用一条生活建议")
        # 停止自我处置条件与转介级别必须成对出现，避免有停诊条件却无去向。
        _require(
            (stop_condition_id is None) == (referral_level_id is None),
            ServiceError,
            "停止自我处置条件与转介级别必须成对引用",
        )
        self.content(RISK_GROUP, target_group)
        self.content(CLIMATE_PHASE, climate_phase_id)
        self.content(EXPOSURE_SCENARIO, scenario_id)
        self.content(EXPERT_SOURCE, expert_source_id)
        for advice_id in advice_ids:
            self.content(LIFE_ADVICE, advice_id)
        if stop_condition_id is not None:
            stop = self.content(STOP_CONDITION, stop_condition_id)
            self.content(SYMPTOM_SET, str(stop.fields["symptom_set_id"]))
            self.content("referral_level", str(referral_level_id))

        if rule_id is None:
            rule_id = f"rule-{len(self._rules) + 1:04d}"
            version = 1
        else:
            existing = self._rules.get(rule_id)
            _require(existing is not None, UnknownAggregateError, f"规则不存在: {rule_id}")
            _require(existing.author == author, WorkflowError, "只能由原作者修订规则")
            version = existing.version + 1
        rule = GuidanceRule(
            rule_id=rule_id,
            version=version,
            author=author,
            target_group=target_group,
            trigger_set=[dict(t) for t in trigger_set],
            climate_phase_id=climate_phase_id,
            scenario_id=scenario_id,
            advice_ids=list(advice_ids),
            expert_source_id=expert_source_id,
            stop_condition_id=stop_condition_id,
            referral_level_id=referral_level_id,
        )
        self._store("rule", rule.to_dict())
        self._emit(
            "RULE_PROPOSED",
            "guidance_rule",
            rule_id,
            {
                "author": author,
                "rule_version": version,
                "target_group": target_group,
                "trigger_set": [dict(t) for t in trigger_set],
            },
        )
        return rule

    def clinical_review(self, rule_id: str, reviewer: str, approve: bool, note: str = "") -> GuidanceRule:
        rule = self._rule(rule_id)
        _require(rule.status == RULE_PROPOSED, WorkflowError, f"规则当前状态不允许审阅: {rule.status}")
        _require(reviewer != rule.author, IndependenceError, "临床审阅不得由规则作者本人执行")
        rule.reviewer = reviewer
        rule.review_note = note
        rule.status = RULE_APPROVED if approve else RULE_REJECTED
        self._store("rule", rule.to_dict())
        self._emit(
            "CLINICAL_REVIEWED",
            "guidance_rule",
            rule_id,
            {"reviewer": reviewer, "approve": bool(approve), "note": note, "rule_version": rule.version},
        )
        return rule

    def sign_release(self, rule_id: str, publisher: str, effective_start: str, effective_end: str | None = None) -> ReleaseRevision:
        rule = self._rule(rule_id)
        _require(rule.status == RULE_APPROVED, WorkflowError, "规则须先通过临床审阅才能签发")
        _require(publisher != rule.author, IndependenceError, "发布签发不得由规则作者执行")
        _require(publisher != rule.reviewer, IndependenceError, "发布签发不得由临床审阅人执行")
        window = TimeWindow(start=parse_instant(effective_start), end=parse_instant(effective_end) if effective_end else None)
        _require(window.end is None or window.start < window.end, ServiceError, "适用时间窗口无效")
        snapshot = self._snapshot(rule)
        ordinal = 1 + sum(1 for r in self._releases.values() if r.rule_id == rule_id)
        release = ReleaseRevision(
            release_id=f"{rule_id}-r{ordinal}",
            rule_id=rule_id,
            rule_version=rule.version,
            signed_by=publisher,
            signed_at=self.clock.now(),
            effective_window=window,
            content_snapshot=snapshot,
        )
        self._store("release", release.to_dict())
        self._emit(
            "RELEASE_SIGNED",
            "release_revision",
            release.release_id,
            {
                "rule_id": rule_id,
                "rule_version": rule.version,
                "signed_by": publisher,
                "effective_window": window.to_dict(),
            },
        )
        return release

    def _snapshot(self, rule: GuidanceRule) -> dict[str, Any]:
        snapshot: dict[str, Any] = {
            "target_group": rule.target_group,
            "trigger_set": [dict(t) for t in rule.trigger_set],
            CLIMATE_PHASE: self.content(CLIMATE_PHASE, rule.climate_phase_id).to_dict(),
            RISK_GROUP: self.content(RISK_GROUP, rule.target_group).to_dict(),
            EXPOSURE_SCENARIO: self.content(EXPOSURE_SCENARIO, rule.scenario_id).to_dict(),
            EXPERT_SOURCE: self.content(EXPERT_SOURCE, rule.expert_source_id).to_dict(),
            "life_advices": [self.content(LIFE_ADVICE, aid).to_dict() for aid in rule.advice_ids],
        }
        if rule.stop_condition_id is not None:
            stop = self.content(STOP_CONDITION, rule.stop_condition_id)
            snapshot[STOP_CONDITION] = stop.to_dict()
            snapshot[SYMPTOM_SET] = self.content(SYMPTOM_SET, str(stop.fields["symptom_set_id"])).to_dict()
            snapshot["referral_level"] = self.content("referral_level", str(rule.referral_level_id)).to_dict()
        return snapshot

    def adopt_release(self, county: str, region: str, release_id: str) -> Adoption:
        """县区采用规则：采用即冻结该签发版本的规则版本号。"""
        release = self._release(release_id)
        adoption = Adoption(
            county=county,
            region=region,
            release_id=release_id,
            rule_version=release.rule_version,
            adopted_at=self.clock.now(),
        )
        self._store("adoption", adoption.to_dict())
        return adoption

    # ------------------------------------------------------------------
    # 事实与暴露回执
    # ------------------------------------------------------------------

    def record_context(self, region: str, facts: Mapping[str, Any]) -> None:
        self._store("facts", {"region": region, "facts": dict(facts)})
        self._emit("CONTEXT_RECORDED", "regional_context", region, {"region": region, "facts": dict(facts)})

    def ingest_receipt(
        self,
        receipt_id: str,
        exposure_key: str,
        channel: str,
        region: str,
        facts: Mapping[str, Any],
    ) -> ExposureRecord:
        """合并同一暴露在不同渠道的回执；内容冲突时停止自动推送。"""
        record = self._exposures.get(exposure_key)
        if record is None:
            record = ExposureRecord(exposure_key=exposure_key, region=region)
            self._exposures[exposure_key] = record
        _require(receipt_id not in record.receipt_ids, WorkflowError, f"回执重复: {receipt_id}")
        record.receipt_ids.append(receipt_id)
        if channel not in record.channels:
            record.channels.append(channel)
        if record.region != region:
            record.conflicts.setdefault("region", [])
            if region not in record.conflicts["region"]:
                record.conflicts["region"].append(region)
        for key, value in facts.items():
            if key not in record.facts:
                record.facts[key] = value
            elif record.facts[key] != value:
                seen = record.conflicts.setdefault(key, [record.facts[key]])
                if value not in seen:
                    seen.append(value)
        record.status = EXPOSURE_CONFLICTED if record.conflicts else EXPOSURE_MERGED
        self._store("exposure", record.to_dict())
        if record.status == EXPOSURE_CONFLICTED:
            self._record_decision(
                DecisionRecord(
                    at=self.clock.now(),
                    region=record.region,
                    audience_group="*",
                    channel="auto_push",
                    outcome="held",
                    reason="exposure_conflicted",
                    detail=f"暴露 {exposure_key} 内容冲突，停止自动推送: {sorted(record.conflicts)}",
                )
            )
        else:
            self._auto_push(record)
        return record

    def resolve_conflict(self, exposure_key: str, resolved_facts: Mapping[str, Any], resolver: str) -> ExposureRecord:
        """人工裁定冲突事实后恢复自动推送。"""
        record = self._exposure(exposure_key)
        _require(record.status == EXPOSURE_CONFLICTED, ConflictStateError, "该暴露不存在未解决的冲突")
        missing = set(record.conflicts) - set(resolved_facts) - {"region"}
        _require(not missing, ConflictStateError, f"冲突字段尚未全部裁定: {sorted(missing)}")
        for key, value in resolved_facts.items():
            record.facts[key] = value
        record.conflicts = {}
        record.status = EXPOSURE_MERGED
        self._store("exposure", record.to_dict())
        self._record_decision(
            DecisionRecord(
                at=self.clock.now(),
                region=record.region,
                audience_group="*",
                channel="auto_push",
                outcome="emitted",
                reason="conflict_resolved",
                detail=f"暴露 {exposure_key} 冲突由 {resolver} 裁定，恢复自动推送",
            )
        )
        self._auto_push(record)
        return record

    def _auto_push(self, record: ExposureRecord) -> None:
        now = self.clock.now()
        for adoption in list(self._adoptions.values()):
            if adoption.region != record.region:
                continue
            release = self._releases[adoption.release_id]
            target = str(release.content_snapshot.get("target_group", ""))
            if release.status != "active":
                self._record_decision(
                    DecisionRecord(
                        at=now,
                        region=adoption.region,
                        audience_group=target,
                        channel="auto_push",
                        outcome="suppressed",
                        reason="release_not_active",
                        detail=f"签发版本 {release.release_id} 状态为 {release.status}，不再自动推送",
                        rule_id=release.rule_id,
                        release_id=release.release_id,
                    )
                )
                continue
            prompts, decisions = evaluate_release(release, record.facts, target, now, "auto_push")
            for decision in decisions:
                self._record_decision(decision)
            for prompt in prompts:
                self._queue_message(adoption, release, prompt, now)

    def _queue_message(self, adoption: Adoption, release: ReleaseRevision, prompt: Prompt, now) -> Optional[Message]:
        for existing in self._messages.values():
            if (
                existing.release_id == release.release_id
                and existing.county == adoption.county
                and existing.kind == prompt.kind
                and existing.text == prompt.text
                and existing.status in (MESSAGE_QUEUED, MESSAGE_SENT, MESSAGE_DELIVERED)
            ):
                self._record_decision(
                    DecisionRecord(
                        at=now,
                        region=adoption.region,
                        audience_group=existing.audience_group,
                        channel="auto_push",
                        outcome="suppressed",
                        reason="duplicate_message",
                        detail=f"与消息 {existing.message_id} 重复，不再排队",
                        rule_id=release.rule_id,
                        release_id=release.release_id,
                    )
                )
                return None
        message = Message(
            message_id=f"msg-{len(self._messages) + 1:04d}",
            release_id=release.release_id,
            county=adoption.county,
            region=adoption.region,
            audience_group=str(release.content_snapshot.get("target_group", "")),
            kind=prompt.kind,
            text=prompt.text,
            basis=prompt.basis,
            status=MESSAGE_QUEUED,
            created_at=now,
        )
        self._store("message", message.to_dict())
        return message

    # ------------------------------------------------------------------
    # 消息生命周期与纠正义务
    # ------------------------------------------------------------------

    def dispatch(self) -> list[Message]:
        """把排队中的消息发出；发出后即不可撤回。"""
        now = self.clock.now()
        sent: list[Message] = []
        for message in self._messages.values():
            if message.status == MESSAGE_QUEUED:
                message.status = MESSAGE_SENT
                message.sent_at = now
                self._store("message", message.to_dict())
                sent.append(message)
        return sent

    def confirm_delivery(self, message_id: str) -> Message:
        message = self._message(message_id)
        _require(message.status == MESSAGE_SENT, WorkflowError, f"消息状态不允许确认送达: {message.status}")
        message.status = MESSAGE_DELIVERED
        message.delivered_at = self.clock.now()
        self._store("message", message.to_dict())
        return message

    def raise_correction(
        self,
        affected_releases: Iterable[str],
        reason: str,
        due_at: str,
        note: str = "",
    ) -> CorrectionObligation:
        """来源更正：仅撤回尚未发送的内容；已送达消息保留原文并生成纠正义务。"""
        now = self.clock.now()
        due = parse_instant(due_at)
        release_ids = list(affected_releases)
        _require(bool(release_ids), ServiceError, "更正必须指明受影响的签发版本")
        kept: list[Message] = []
        for release_id in release_ids:
            release = self._release(release_id)
            release.status = "corrected"
            self._store("release", release.to_dict())
            for message in self._messages.values():
                if message.release_id != release_id:
                    continue
                if message.status == MESSAGE_QUEUED:
                    message.status = MESSAGE_RETRACTED
                    self._store("message", message.to_dict())
                    self._record_decision(
                        DecisionRecord(
                            at=now,
                            region=message.region,
                            audience_group=message.audience_group,
                            channel="auto_push",
                            outcome="suppressed",
                            reason="retracted_by_correction",
                            detail=f"消息 {message.message_id} 尚未发送，已被更正撤回",
                            rule_id=release.rule_id,
                            release_id=release_id,
                        )
                    )
                elif message.status in (MESSAGE_SENT, MESSAGE_DELIVERED):
                    kept.append(message)
        scope = {
            "counties": sorted({m.county for m in kept}),
            "audience_groups": sorted({m.audience_group for m in kept}),
            "message_ids": sorted(m.message_id for m in kept),
            "note": note,
        }
        obligation = CorrectionObligation(
            obligation_id=f"cor-{len(self._obligations) + 1:04d}",
            reason=reason,
            affected_releases=release_ids,
            scope=scope,
            raised_at=now,
            due_at=due,
            status=OBLIGATION_OPEN if due >= now else OBLIGATION_OVERDUE,
        )
        self._store("obligation", obligation.to_dict())
        self._emit(
            "CORRECTION_RAISED",
            "correction_obligation",
            obligation.obligation_id,
            {
                "affected_releases": release_ids,
                "due_at": due.isoformat(),
                "reason": reason,
                "scope": scope,
            },
        )
        return obligation

    def fulfill_correction(self, obligation_id: str, note: str = "") -> CorrectionObligation:
        obligation = self._obligation(obligation_id)
        _require(obligation.status in (OBLIGATION_OPEN, OBLIGATION_OVERDUE), WorkflowError, "纠正义务已关闭")
        obligation.status = OBLIGATION_FULFILLED
        obligation.fulfilled_at = self.clock.now()
        if note:
            obligation.scope = {**obligation.scope, "fulfill_note": note}
        self._store("obligation", obligation.to_dict())
        return obligation

    def refresh_obligations(self) -> list[CorrectionObligation]:
        """以可控时钟推进纠正期限：超过期限仍未履行的标记为逾期。"""
        now = self.clock.now()
        changed: list[CorrectionObligation] = []
        for obligation in self._obligations.values():
            if obligation.status == OBLIGATION_OPEN and now > obligation.due_at:
                obligation.status = OBLIGATION_OVERDUE
                self._store("obligation", obligation.to_dict())
                changed.append(obligation)
        return changed

    # ------------------------------------------------------------------
    # 查询 API：按地区、人群与当下事实返回必要且克制的行动提示
    # ------------------------------------------------------------------

    def current_facts(self, region: str) -> dict[str, Any]:
        facts = dict(self._region_facts.get(region, {}))
        for record in self._exposures.values():
            if record.region == region and record.status == EXPOSURE_MERGED:
                facts.update(record.facts)
        return facts

    def current_prompts(
        self,
        region: str,
        audience_group: str,
        county: str | None = None,
        facts: Mapping[str, Any] | None = None,
    ) -> list[Prompt]:
        self.refresh_obligations()
        now = self.clock.now()
        merged_facts = dict(facts) if facts is not None else self.current_facts(region)
        releases = self._candidate_releases(region, county)
        prompts: list[Prompt] = []
        for release in releases:
            if release.status != "active":
                self._record_decision(
                    DecisionRecord(
                        at=now,
                        region=region,
                        audience_group=audience_group,
                        channel="query",
                        outcome="suppressed",
                        reason="release_not_active",
                        detail=f"签发版本 {release.release_id} 状态为 {release.status}",
                        rule_id=release.rule_id,
                        release_id=release.release_id,
                    )
                )
                continue
            evaluated, decisions = evaluate_release(release, merged_facts, audience_group, now, "query")
            prompts.extend(evaluated)
            for decision in decisions:
                self._record_decision(decision)
        for record in self._exposures.values():
            if record.region == region and record.status == EXPOSURE_CONFLICTED:
                self._record_decision(
                    DecisionRecord(
                        at=now,
                        region=region,
                        audience_group=audience_group,
                        channel="query",
                        outcome="held",
                        reason="exposure_conflicted",
                        detail=f"暴露 {record.exposure_key} 内容冲突，其事实未参与评估",
                    )
                )
        return dedupe_prompts(prompts)

    def _candidate_releases(self, region: str, county: str | None) -> list[ReleaseRevision]:
        if county is not None:
            releases = []
            for adoption in self._adoptions.values():
                if adoption.county == county and adoption.region == region:
                    releases.append(self._releases[adoption.release_id])
            return releases
        return [r for r in self._releases.values() if r.region == region]

    # ------------------------------------------------------------------
    # 审计访问
    # ------------------------------------------------------------------

    @property
    def messages(self) -> list[Message]:
        return list(self._messages.values())

    @property
    def obligations(self) -> list[CorrectionObligation]:
        return list(self._obligations.values())

    @property
    def exposures(self) -> list[ExposureRecord]:
        return list(self._exposures.values())

    @property
    def decisions(self) -> list[DecisionRecord]:
        return list(self._decisions)

    @property
    def events(self) -> list[dict[str, Any]]:
        return list(self._events)

    def message(self, message_id: str) -> Message:
        return self._message(message_id)

    def release(self, release_id: str) -> ReleaseRevision:
        return self._release(release_id)

    def _record_decision(self, decision: DecisionRecord) -> None:
        self._store("decision", decision.to_dict())

    def _rule(self, rule_id: str) -> GuidanceRule:
        rule = self._rules.get(rule_id)
        _require(rule is not None, UnknownAggregateError, f"规则不存在: {rule_id}")
        return rule

    def _release(self, release_id: str) -> ReleaseRevision:
        release = self._releases.get(release_id)
        _require(release is not None, UnknownAggregateError, f"签发版本不存在: {release_id}")
        return release

    def _message(self, message_id: str) -> Message:
        message = self._messages.get(message_id)
        _require(message is not None, UnknownAggregateError, f"消息不存在: {message_id}")
        return message

    def _exposure(self, exposure_key: str) -> ExposureRecord:
        record = self._exposures.get(exposure_key)
        _require(record is not None, UnknownAggregateError, f"暴露不存在: {exposure_key}")
        return record

    def _obligation(self, obligation_id: str) -> CorrectionObligation:
        obligation = self._obligations.get(obligation_id)
        _require(obligation is not None, UnknownAggregateError, f"纠正义务不存在: {obligation_id}")
        return obligation
