"""签发台领域服务：三权分立、版本冻结、回执合并、纠正义务。

所有事件都经可控时钟取时；写操作先在投影上校验不变量，再落事件。
"""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from typing import Mapping, Sequence

from .clock import Clock
from .model import (
    CLINICAL_SLOTS,
    COMPONENT_KINDS,
    GENERAL_SLOTS,
    REQUIRED_SLOTS,
    State,
)


class DomainError(ValueError):
    """业务不变量被违反。"""


ROLE_AUTHOR = "author"
ROLE_REVIEWER = "reviewer"
ROLE_SIGNER = "signer"


def canonical_signature(facts: Mapping[str, object]) -> str:
    """按规范化 JSON 计算事实签名，用于跨渠道同暴露比对。"""
    blob = json.dumps(facts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class RuleDesk:
    def __init__(self, clock: Clock, state: State | None = None,
                 actors: Mapping[str, Mapping[str, object]] | None = None,
                 schema: Mapping[str, object] | None = None) -> None:
        self.clock = clock
        self.state = state or State()
        # actor_id -> {"roles": set[str], "name": str}
        self.actors = {
            aid: {"roles": set(cast_roles(info.get("roles"))), "name": str(info.get("name", aid))}
            for aid, info in (actors or {}).items()
        }
        self.schema = schema
        self._new_events: list[dict] = []

    # -- 重放与输出 ---------------------------------------------------------

    def load(self, events: Sequence[Mapping[str, object]]) -> None:
        for event in events:
            self.state.apply(event)

    @property
    def new_events(self) -> list[dict]:
        return self._new_events

    def drain_events(self) -> list[dict]:
        events = self._new_events
        self._new_events = []
        return events

    # -- 内部工具 -----------------------------------------------------------

    def _require_actor(self, actor_id: str, role: str) -> dict:
        actor = self.actors.get(actor_id)
        if actor is None:
            raise DomainError(f"未登记的参与方: {actor_id}")
        if role not in actor["roles"]:
            raise DomainError(f"参与方 {actor_id} 不具备 {role} 角色")
        return actor

    def _emit(self, event_id: str | None, aggregate_type: str, aggregate_id: str,
              event_type: str, payload: Mapping[str, object]) -> dict:
        key = (aggregate_type, aggregate_id)
        version = self.state.aggregate_versions.get(key, 0) + 1
        event = {
            "event_id": event_id or f"{aggregate_id}-v{version}-{event_type.lower()}",
            "event_type": event_type,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "occurred_at": self.clock.now().isoformat(),
            "version": version,
            "payload": dict(payload),
        }
        if self.schema is not None:
            from .contracts import validate_event
            issues = validate_event(event, self.schema)
            if issues:
                raise DomainError("事件违反契约: " + "; ".join(
                    f"{i.field}/{i.code}" for i in issues))
        if self.state.apply(event):
            self._new_events.append(event)
        return event

    # -- 地域气候阶段事实 ---------------------------------------------------

    def record_context(self, region_code: str, phase: str,
                       facts: Mapping[str, object], *,
                       context_id: str | None = None,
                       observed_at: datetime | None = None) -> dict:
        cid = context_id or f"ctx-{region_code}"
        payload = {
            "region_code": region_code, "phase": phase,
            "observed_at": (observed_at or self.clock.now()).isoformat(),
            "facts": dict(facts),
        }
        return self._emit(None, "regional_context", cid, "CONTEXT_RECORDED", payload)

    # -- 组件版本化 ---------------------------------------------------------

    def version_component(self, component_id: str, kind: str, classification: str,
                          body: Mapping[str, object], *, author: str,
                          version: int | None = None, supersedes: int | None = None) -> dict:
        if kind not in COMPONENT_KINDS:
            raise DomainError(f"未知组件类型: {kind}")
        if classification not in ("general", "clinical"):
            raise DomainError("classification 仅可为 general / clinical")
        if classification == "general" and kind in CLINICAL_SLOTS:
            raise DomainError(
                f"{kind} 属于临床槽位，general 组件（含中医调养）不得进入，"
                "不得把生活建议自动升级为疾病判断")
        if classification == "clinical" and kind in GENERAL_SLOTS:
            raise DomainError("lifestyle_advice 槽位只承载 general 生活建议")
        existing = self.state.components.get(component_id, {})
        next_version = (max(existing) + 1) if existing else 1
        version = version or next_version
        if version != next_version:
            raise DomainError(f"组件 {component_id} 下版本必须连续递增，应为 {next_version}")
        if supersedes is not None and supersedes not in existing:
            raise DomainError("supersedes 指向的旧版本不存在")
        payload = {
            "component_id": component_id, "component_kind": kind,
            "component_version": version, "classification": classification,
            "body": dict(body), "author": author,
        }
        if supersedes is not None:
            payload["supersedes"] = supersedes
        return self._emit(None, "guidance_component", component_id,
                          "COMPONENT_VERSIONED", payload)

    # -- 规则：作者提出 -----------------------------------------------------

    def propose_rule(self, rule_id: str, target_group: str,
                     trigger_set: Mapping[str, object],
                     slots: Mapping[str, tuple[str, int]], *, proposed_by: str,
                     event_id: str | None = None) -> dict:
        self._require_actor(proposed_by, ROLE_AUTHOR)
        versions = self.state.rules.get(rule_id, {})
        rule_version = (max(versions) + 1) if versions else 1

        missing = REQUIRED_SLOTS - set(slots)
        if missing:
            raise DomainError(f"规则缺少必备槽位: {sorted(missing)}")
        resolved = {}
        for kind, (cid, cver) in slots.items():
            if kind not in COMPONENT_KINDS:
                raise DomainError(f"未知槽位: {kind}")
            try:
                comp = self.state.component(cid, cver)
            except KeyError:
                raise DomainError(f"槽位 {kind} 引用了不存在的组件版本 {cid}@{cver}")
            if comp.kind != kind:
                raise DomainError(f"槽位 {kind} 不能引用 {comp.kind} 组件 {cid}@{cver}")
            if kind in CLINICAL_SLOTS and comp.classification != "clinical":
                # 中医调养等生活建议不得自动升级为疾病判断
                raise DomainError(
                    f"槽位 {kind} 必须引用 clinical 组件；{cid}@{cver} 是 general，"
                    "不得升级为疾病判断")
            if kind in GENERAL_SLOTS and comp.classification != "general":
                raise DomainError(f"槽位 {kind} 只能引用 general 组件")
            resolved[kind] = comp
        if resolved["risk_group"].body.get("group_id", target_group) != target_group:
            raise DomainError("risk_group 组件与目标人群不一致")

        # 触发器中出现的症状码必须在所引用症状组合内登记
        declared = set(resolved["symptom_set"].body.get("symptoms", ()))
        used = set(trigger_set.get("symptoms_all", ())) | set(trigger_set.get("symptoms_any", ()))
        unknown_symptoms = used - declared
        if unknown_symptoms:
            raise DomainError(f"触发器引用了症状组合未登记的症状: {sorted(unknown_symptoms)}")

        payload = {
            "rule_id": rule_id, "rule_version": rule_version,
            "target_group": target_group,
            "trigger_set": {k: list(v) for k, v in trigger_set.items()},
            "slots": {k: [cid, ver] for k, (cid, ver) in slots.items()},
            "proposed_by": proposed_by,
        }
        return self._emit(event_id, "guidance_rule", rule_id, "RULE_PROPOSED", payload)

    # -- 规则：临床独立审阅 -------------------------------------------------

    def clinical_review(self, rule_id: str, rule_version: int, *,
                        reviewer: str, decision: str, notes: str | None = None,
                        expert_refs: Sequence[tuple[str, int]] = ()) -> dict:
        self._require_actor(reviewer, ROLE_REVIEWER)
        if decision not in ("approved", "changes_requested"):
            raise DomainError("审阅决定仅可为 approved / changes_requested")
        rule = self.state.rules.get(rule_id, {}).get(rule_version)
        if rule is None:
            raise DomainError("被审阅的规则版本不存在")
        if reviewer == rule.proposed_by:
            raise DomainError("临床审阅人不得是规则作者，审与著必须分离")
        for cid, cver in expert_refs:
            comp = self.state.component(cid, cver)
            if comp.kind != "expert_source":
                raise DomainError(f"专家依据必须引用 expert_source 组件: {cid}@{cver}")
        payload = {
            "rule_id": rule_id, "rule_version": rule_version,
            "reviewer": reviewer, "decision": decision,
            "expert_refs": [[cid, ver] for cid, ver in expert_refs],
        }
        if notes is not None:
            payload["notes"] = notes
        return self._emit(None, "guidance_rule", rule_id, "CLINICAL_REVIEWED", payload)

    # -- 规则：发布独立签发 -------------------------------------------------

    def sign_release(self, rule_id: str, rule_version: int, *, signer: str,
                     window_start: datetime, window_end: datetime,
                     release_id: str | None = None) -> dict:
        self._require_actor(signer, ROLE_SIGNER)
        rule = self.state.rules.get(rule_id, {}).get(rule_version)
        if rule is None:
            raise DomainError("被签发的规则版本不存在")
        if rule.status != "approved":
            raise DomainError("只有临床审阅通过的版本才能签发")
        if signer == rule.proposed_by:
            raise DomainError("签发人不得是规则作者")
        if rule.reviewed_by is not None and signer == rule.reviewed_by:
            raise DomainError("签发人不得是该版临床审阅人，签与审必须分离")
        if window_start.tzinfo is None or window_end.tzinfo is None:
            raise DomainError("生效窗口必须携带时区")
        if not window_start < window_end:
            raise DomainError("生效窗口开始必须早于结束")
        release_id = release_id or f"rel-{rule_id}-v{rule_version}"
        if release_id in self.state.releases:
            raise DomainError(f"发布标识已存在: {release_id}")
        payload = {
            "release_id": release_id, "rule_id": rule_id, "rule_version": rule_version,
            "signer": signer,
            "effective_window": {"start": window_start.isoformat(), "end": window_end.isoformat()},
        }
        return self._emit(None, "release_revision", release_id, "RELEASE_SIGNED", payload)

    # -- 县区冻结采用 -------------------------------------------------------

    def freeze_adoption(self, county_id: str, rule_id: str, rule_version: int) -> dict:
        rule = self.state.rules.get(rule_id, {}).get(rule_version)
        if rule is None or rule.status != "signed":
            raise DomainError("县区只能冻结已签发的规则版本")
        payload = {
            "county_id": county_id, "rule_id": rule_id, "rule_version": rule_version,
            "frozen_components": {k: [cid, ver] for k, (cid, ver) in rule.slots.items()},
            "frozen_at": self.clock.now().isoformat(),
        }
        return self._emit(None, "guidance_rule", f"adoption-{county_id}-{rule_id}",
                          "COUNTY_ADOPTION_FROZEN", payload)

    # -- 暴露回执：记录 + 自动合并 ------------------------------------------

    def record_receipt(self, region_code: str, exposure_key: str, channel: str,
                       facts: Mapping[str, object], *, receipt_id: str | None = None) -> list[dict]:
        rid = receipt_id or f"rcp-{region_code}-{exposure_key}-{channel}"
        signature = canonical_signature(facts)
        payload = {
            "receipt_id": rid, "region_code": region_code,
            "exposure_key": exposure_key, "channel": channel,
            "facts_signature": signature, "facts": dict(facts),
            "received_at": self.clock.now().isoformat(),
        }
        emitted = [self._emit(None, "exposure_receipt", f"{region_code}:{exposure_key}",
                              "RECEIPT_RECORDED", payload)]
        emitted.extend(self._merge_receipts(region_code, exposure_key))
        return emitted

    def _receipt_key(self, region_code: str, exposure_key: str) -> tuple[str, str]:
        return (region_code, exposure_key)

    def _merge_receipts(self, region_code: str, exposure_key: str) -> list[dict]:
        key = self._receipt_key(region_code, exposure_key)
        receipts = self.state.receipts.get(key, [])
        if len(receipts) < 2:
            return []
        signatures = {r.facts_signature for r in receipts}
        emitted: list[dict] = []
        if len(signatures) == 1:
            # 签名一致：内容相同方可合并；此前冲突已挂起的，不在此处自动恢复
            suspension = self.state.suspensions.get(key)
            if key not in self.state.merged_facts and not (suspension and suspension["suspended"]):
                facts = dict(receipts[0].facts)
                emitted.append(self._emit(
                    None, "exposure_receipt", f"{region_code}:{exposure_key}",
                    "RECEIPTS_MERGED",
                    {"region_code": region_code, "exposure_key": exposure_key,
                     "receipt_ids": sorted(r.receipt_id for r in receipts),
                     "outcome": "merged", "facts": facts,
                     "channels": sorted(r.channel for r in receipts)}))
        else:
            # 内容冲突：停止该暴露范围的自动推送，等待人工处置
            suspension = self.state.suspensions.get(key)
            if not (suspension and suspension["suspended"]):
                channels = ", ".join(sorted({r.channel for r in receipts}))
                emitted.append(self._emit(
                    None, "exposure_receipt", f"{region_code}:{exposure_key}",
                    "RECEIPTS_MERGED",
                    {"region_code": region_code, "exposure_key": exposure_key,
                     "receipt_ids": sorted(r.receipt_id for r in receipts),
                     "outcome": "conflict"}))
                emitted.append(self._emit(
                    None, "exposure_receipt", f"{region_code}:{exposure_key}",
                    "PUSH_SUSPENDED",
                    {"scope": {"region_code": region_code, "exposure_key": exposure_key},
                     "reason": f"渠道 {channels} 对同一暴露事实回报内容冲突"}))
        return emitted

    def resolve_conflict(self, region_code: str, exposure_key: str,
                         canonical_facts: Mapping[str, object], *, resolved_by: str) -> list[dict]:
        """人工处置冲突：以核定事实合并后恢复推送。"""
        key = self._receipt_key(region_code, exposure_key)
        receipts = self.state.receipts.get(key, [])
        if not receipts:
            raise DomainError("该暴露范围没有回执记录")
        emitted = [self._emit(
            None, "exposure_receipt", f"{region_code}:{exposure_key}",
            "RECEIPTS_MERGED",
            {"region_code": region_code, "exposure_key": exposure_key,
             "receipt_ids": sorted(r.receipt_id for r in receipts),
             "outcome": "merged", "facts": dict(canonical_facts),
             "resolved_by": resolved_by})]
        emitted.append(self._emit(
            None, "exposure_receipt", f"{region_code}:{exposure_key}",
            "PUSH_RESUMED",
            {"scope": {"region_code": region_code, "exposure_key": exposure_key},
             "resolved_by": resolved_by}))
        return emitted

    # -- 消息 ---------------------------------------------------------------

    def message_is_pushable(self, region_code: str, exposure_keys: frozenset[str]) -> str | None:
        """返回阻止推送的原因，没有则返回 None。"""
        for (region, ex_key), suspension in self.state.suspensions.items():
            if region != region_code or not suspension["suspended"]:
                continue
            if ex_key == "*" or ex_key in exposure_keys:
                return f"push_suspended:{ex_key}:{suspension['reason']}"
        return None

    def prepare_message(self, message_id: str, county_id: str, region_code: str,
                        rule_id: str, *, basis: Mapping[str, object]) -> dict:
        frozen_version = self.state.adoptions.get((county_id, rule_id))
        if frozen_version is None:
            raise DomainError(f"县区 {county_id} 未冻结采用规则 {rule_id}")
        rule = self.state.rules[rule_id][frozen_version]
        if rule.status != "signed" or rule.release_id is None:
            raise DomainError("冻结版本未处于签发状态")
        now = self.clock.now()
        if not (rule.effective_start <= now < rule.effective_end):
            raise DomainError("规则不在生效窗口内（生效/过期以可控时钟判定）")
        block = self.message_is_pushable(region_code, rule.triggers.exposure_keys)
        if block:
            raise DomainError(f"自动推送已停止: {block}")
        if any(rule.release_id in corr.affected_releases for corr in self.state.corrections.values()):
            raise DomainError("该发布版本存在来源更正，不得继续按旧版推送")
        payload = {
            "message_id": message_id, "county_id": county_id, "region_code": region_code,
            "target_group": rule.target_group, "release_id": rule.release_id,
            "rule_id": rule_id, "rule_version": rule.version,
            "component_refs": {k: [cid, ver] for k, (cid, ver) in rule.slots.items()},
            "basis": dict(basis), "prepared_at": now.isoformat(),
        }
        return self._emit(None, "message_dispatch", message_id, "MESSAGE_PREPARED", payload)

    def deliver_message(self, message_id: str, *, delivered_at: datetime | None = None) -> dict:
        message = self.state.messages.get(message_id)
        if message is None or message.status != "prepared":
            raise DomainError("只有待发消息可以标记送达")
        return self._emit(None, "message_dispatch", message_id, "MESSAGE_DELIVERED",
                          {"message_id": message_id,
                           "delivered_at": (delivered_at or self.clock.now()).isoformat()})

    # -- 来源更正 -----------------------------------------------------------

    def releases_referencing_component(self, component_id: str,
                                       component_version: int | None = None) -> list[str]:
        result = []
        for rule_versions in self.state.rules.values():
            for rule in rule_versions.values():
                if rule.release_id is None:
                    continue
                for cid, cver in rule.slots.values():
                    if cid == component_id and (component_version is None or cver == component_version):
                        result.append(rule.release_id)
        return sorted(set(result))

    def raise_correction(self, correction_id: str, affected_releases: Sequence[str],
                         *, raised_by: str, scope_note: str,
                         due_at: datetime | None = None) -> dict:
        """来源更正：撤回未发消息；已送达消息保留原文并立纠正义务。"""
        if not affected_releases:
            raise DomainError("更正必须指明受影响的发布版本范围")
        releases = set(affected_releases)
        unknown = releases - set(self.state.releases)
        if unknown:
            raise DomainError(f"未知发布版本: {sorted(unknown)}")
        now = self.clock.now()
        to_revoke: list[str] = []
        obligations: list[str] = []
        for mid, message in self.state.messages.items():
            if message.release_id not in releases or message.status == "revoked":
                continue
            if message.status == "prepared":
                to_revoke.append(mid)  # 尚未发送：撤回，不生成纠正义务
            elif message.status == "delivered":
                obligations.append(mid)  # 已送达：保留原文，生成范围明确的纠正义务
        # 先撤回待发消息（保留可追溯事件）
        for mid in to_revoke:
            self._emit(None, "message_dispatch", mid, "MESSAGE_REVOKED",
                       {"message_id": mid,
                        "reason": f"来源更正 {correction_id}：{scope_note}"})
        due = due_at or (now + timedelta(hours=24))
        if due.tzinfo is None:
            raise DomainError("纠正期限必须携带时区")
        payload = {
            "correction_id": correction_id,
            "affected_releases": sorted(releases),
            "scope_note": scope_note, "raised_by": raised_by,
            "due_at": due.isoformat(),
            "revoked_messages": sorted(to_revoke),
            "obligation_messages": sorted(obligations),
        }
        return self._emit(None, "correction_obligation", correction_id,
                          "CORRECTION_RAISED", payload)

    def fulfill_correction(self, correction_id: str, message_ids: Sequence[str]) -> dict:
        corr = self.state.corrections.get(correction_id)
        if corr is None:
            raise DomainError("纠正义务不存在")
        unknown = set(message_ids) - set(corr.obligations)
        if unknown:
            raise DomainError(f"不在该纠正义务范围内的消息: {sorted(unknown)}")
        return self._emit(None, "correction_obligation", correction_id,
                          "CORRECTION_FULFILLED",
                          {"correction_id": correction_id,
                           "message_ids": sorted(message_ids),
                           "fulfilled_at": self.clock.now().isoformat()})

    def overdue_corrections(self, *, at: datetime | None = None) -> list[str]:
        """按可控时钟返回已逾纠正期限但仍有未履行义务的纠正单。"""
        now = at or self.clock.now()
        return [cid for cid, c in self.state.corrections.items()
                if c.fulfilled_at is None and any(v != "fulfilled" for v in c.obligations.values())
                and c.due_at <= now]


def cast_roles(value: object) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        return {value}
    return set(value)  # type: ignore[arg-type]
