"""领域模型与事件溯源状态投影。

状态只能由事件重放得到；本模块不产生业务决策（角色分离、升级拦截等
不变量在 ``service.RuleDesk`` 中执行），只做忠实投影。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Mapping


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


# --- 九类分别版本化的内容组件 ---------------------------------------------

COMPONENT_KINDS = (
    "climate_phase",       # 地域气候阶段
    "risk_group",          # 风险人群
    "symptom_set",         # 症状组合
    "exposure_scenario",   # 暴露场景
    "lifestyle_advice",    # 生活建议
    "stop_self_care",      # 停止自我处置条件
    "referral_level",      # 转介级别
    "expert_source",       # 专家来源
    "applicable_window",   # 适用时间
)

GENERAL = "general"     # 普通生活建议（含中医调养）
CLINICAL = "clinical"   # 临床判读内容

# 临床槽位：只允许 clinical 分类的组件进入，general 不得自动升级为疾病判断
CLINICAL_SLOTS = frozenset({"symptom_set", "stop_self_care", "referral_level"})
# 普通生活建议槽位只承载 general 组件
GENERAL_SLOTS = frozenset({"lifestyle_advice"})

# 一条可推送规则必须具备的槽位：行动提示必须同时给出停止条件与转介级别
REQUIRED_SLOTS = frozenset(
    {"risk_group", "symptom_set", "stop_self_care", "referral_level",
     "expert_source", "applicable_window"}
)


@dataclass(frozen=True)
class Component:
    component_id: str
    kind: str
    version: int
    classification: str
    body: Mapping[str, object]
    author: str
    supersedes: int | None = None


# --- 规则与事实 ------------------------------------------------------------

@dataclass(frozen=True)
class TriggerSet:
    climate_phases: frozenset[str] = field(default_factory=frozenset)
    symptoms_all: frozenset[str] = field(default_factory=frozenset)
    symptoms_any: frozenset[str] = field(default_factory=frozenset)
    exposure_keys: frozenset[str] = field(default_factory=frozenset)

    def to_payload(self) -> dict:
        return {
            "climate_phases": sorted(self.climate_phases),
            "symptoms_all": sorted(self.symptoms_all),
            "symptoms_any": sorted(self.symptoms_any),
            "exposure_keys": sorted(self.exposure_keys),
        }

    @classmethod
    def from_payload(cls, body: Mapping[str, object]) -> "TriggerSet":
        def names(key: str) -> frozenset[str]:
            return frozenset(body.get(key, ()) or ())  # type: ignore[arg-type]
        return cls(names("climate_phases"), names("symptoms_all"),
                   names("symptoms_any"), names("exposure_keys"))


@dataclass(frozen=True)
class Facts:
    """评估当下事实：地区、人群、气候阶段、症状、暴露场景。"""

    region_code: str
    group_id: str
    climate_phase: str | None = None
    symptoms: frozenset[str] = field(default_factory=frozenset)
    exposures: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class Receipt:
    receipt_id: str
    channel: str
    facts_signature: str
    facts: Mapping[str, object]


@dataclass(frozen=True)
class RuleVersionView:
    rule_id: str
    version: int
    target_group: str
    triggers: TriggerSet
    slots: Mapping[str, tuple[str, int]]
    proposed_by: str
    proposed_at: datetime
    status: str = "proposed"                  # proposed / approved / changes_requested / signed
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    review_notes: str | None = None
    reviewed_expert_refs: tuple[tuple[str, int], ...] = ()
    review_decision: str | None = None
    signed_by: str | None = None
    signed_at: datetime | None = None
    release_id: str | None = None
    effective_start: datetime | None = None
    effective_end: datetime | None = None


@dataclass(frozen=True)
class MessageView:
    message_id: str
    county_id: str
    region_code: str
    target_group: str
    release_id: str
    rule_id: str
    rule_version: int
    component_refs: Mapping[str, tuple[str, int]]
    status: str                               # prepared / delivered / revoked
    prepared_at: datetime
    delivered_at: datetime | None = None
    revoke_reason: str | None = None


@dataclass(frozen=True)
class CorrectionView:
    correction_id: str
    affected_releases: tuple[str, ...]
    scope_note: str
    raised_by: str
    raised_at: datetime
    due_at: datetime
    revoked_messages: tuple[str, ...]
    obligations: dict[str, str]               # message_id -> open/fulfilled
    fulfilled_at: datetime | None = None


class State:
    """事件流投影。``apply`` 必须对已见事件幂等。"""

    def __init__(self) -> None:
        self.events: list[dict] = []
        self.seen_event_ids: set[str] = set()
        self.aggregate_versions: dict[tuple[str, str], int] = {}
        self.contexts: dict[str, list[dict]] = {}
        self.components: dict[str, dict[int, Component]] = {}
        self.rules: dict[str, dict[int, RuleVersionView]] = {}
        self.releases: dict[str, dict] = {}
        self.adoptions: dict[tuple[str, str], int] = {}
        self.receipts: dict[tuple[str, str], list[Receipt]] = {}
        self.merged_facts: dict[tuple[str, str], Mapping[str, object]] = {}
        self.suspensions: dict[tuple[str, str], dict] = {}
        self.messages: dict[str, MessageView] = {}
        self.corrections: dict[str, CorrectionView] = {}

    # -- 读取辅助 -----------------------------------------------------------

    def component(self, component_id: str, version: int) -> Component:
        return self.components[component_id][version]

    def resolve_slot(self, rule: RuleVersionView, kind: str) -> Component | None:
        ref = rule.slots.get(kind)
        if ref is None:
            return None
        return self.component(*ref)

    def latest_rule_version(self, rule_id: str) -> int:
        versions = self.rules.get(rule_id, {})
        return max(versions) if versions else 0

    def active_exposures(self, region_code: str) -> frozenset[str]:
        """已由多渠道一致回执确认、且未处于冲突停推的暴露键。"""
        keys = set()
        for (region, key) in self.merged_facts:
            if region == region_code and not self.suspensions.get((region, key), {}).get("suspended"):
                keys.add(key)
        return frozenset(keys)

    # -- 投影 ---------------------------------------------------------------

    def apply(self, event: Mapping[str, object]) -> bool:
        event_id = str(event["event_id"])
        if event_id in self.seen_event_ids:
            return False  # 幂等：重放/重启时跳过
        etype = event["event_type"]
        body = event["payload"]
        ts = parse_ts(str(event["occurred_at"]))
        p = body

        if etype == "CONTEXT_RECORDED":
            self.contexts.setdefault(str(p["region_code"]), []).append(dict(p))

        elif etype == "COMPONENT_VERSIONED":
            cid = str(p["component_id"])
            comp = Component(
                component_id=cid, kind=str(p["component_kind"]),
                version=int(p["component_version"]),
                classification=str(p["classification"]),
                body=dict(p.get("body", {})),
                author=str(p.get("author", "")),
                supersedes=p.get("supersedes"),
            )
            self.components.setdefault(cid, {})[comp.version] = comp

        elif etype == "RULE_PROPOSED":
            rid = str(p["rule_id"])
            slots = {k: (v[0], int(v[1])) for k, v in (p.get("slots") or {}).items()}
            view = RuleVersionView(
                rule_id=rid, version=int(p["rule_version"]),
                target_group=str(p["target_group"]),
                triggers=TriggerSet.from_payload(p.get("trigger_set") or {}),
                slots=slots, proposed_by=str(p["proposed_by"]), proposed_at=ts,
            )
            self.rules.setdefault(rid, {})[view.version] = view

        elif etype == "CLINICAL_REVIEWED":
            rid = str(p["rule_id"])
            old = self.rules[rid][int(p["rule_version"])]
            decision = str(p["decision"])
            refs = tuple((str(r[0]), int(r[1])) for r in (p.get("expert_refs") or ()))
            self.rules[rid][old.version] = RuleVersionView(
                **{**old.__dict__, "status": decision, "reviewed_by": str(p["reviewer"]),
                   "reviewed_at": ts, "review_notes": p.get("notes"),
                   "reviewed_expert_refs": refs, "review_decision": decision})

        elif etype == "RELEASE_SIGNED":
            rid, rv = str(p["rule_id"]), int(p["rule_version"])
            old = self.rules[rid][rv]
            win = p["effective_window"]
            view = RuleVersionView(
                **{**old.__dict__, "status": "signed", "signed_by": str(p["signer"]),
                   "signed_at": ts, "release_id": str(p["release_id"]),
                   "effective_start": parse_ts(str(win["start"])),
                   "effective_end": parse_ts(str(win["end"]))})
            self.rules[rid][rv] = view
            self.releases[str(p["release_id"])] = {"rule_id": rid, "rule_version": rv}

        elif etype == "COUNTY_ADOPTION_FROZEN":
            self.adoptions[(str(p["county_id"]), str(p["rule_id"]))] = int(p["rule_version"])

        elif etype == "RECEIPT_RECORDED":
            key = (str(p["region_code"]), str(p["exposure_key"]))
            self.receipts.setdefault(key, []).append(
                Receipt(str(p["receipt_id"]), str(p["channel"]),
                        str(p["facts_signature"]), dict(p.get("facts", {}))))

        elif etype == "RECEIPTS_MERGED":
            key = (str(p["region_code"]), str(p["exposure_key"]))
            if str(p["outcome"]) == "merged":
                self.merged_facts[key] = dict(p.get("facts", {}))
            # conflict：不写入合并事实，等待 PUSH_SUSPENDED

        elif etype == "PUSH_SUSPENDED":
            scope = p["scope"]
            key = (str(scope["region_code"]), str(scope["exposure_key"]))
            self.suspensions[key] = {"suspended": True, "reason": str(p.get("reason", ""))}

        elif etype == "PUSH_RESUMED":
            scope = p["scope"]
            key = (str(scope["region_code"]), str(scope["exposure_key"]))
            self.suspensions[key] = {"suspended": False, "reason": ""}

        elif etype == "MESSAGE_PREPARED":
            mid = str(p["message_id"])
            refs = {k: (v[0], int(v[1])) for k, v in (p.get("component_refs") or {}).items()}
            self.messages[mid] = MessageView(
                message_id=mid, county_id=str(p["county_id"]),
                region_code=str(p["region_code"]), target_group=str(p["target_group"]),
                release_id=str(p["release_id"]), rule_id=str(p["rule_id"]),
                rule_version=int(p["rule_version"]), component_refs=refs,
                status="prepared", prepared_at=ts)

        elif etype == "MESSAGE_DELIVERED":
            old = self.messages[str(p["message_id"])]
            self.messages[old.message_id] = MessageView(
                **{**old.__dict__, "status": "delivered",
                   "delivered_at": parse_ts(str(p["delivered_at"]))})

        elif etype == "MESSAGE_REVOKED":
            old = self.messages[str(p["message_id"])]
            self.messages[old.message_id] = MessageView(
                **{**old.__dict__, "status": "revoked", "revoke_reason": str(p.get("reason", ""))})

        elif etype == "CORRECTION_RAISED":
            cid = str(p["correction_id"])
            self.corrections[cid] = CorrectionView(
                correction_id=cid,
                affected_releases=tuple(p["affected_releases"]),
                scope_note=str(p.get("scope_note", "")), raised_by=str(p["raised_by"]),
                raised_at=ts, due_at=parse_ts(str(p["due_at"])),
                revoked_messages=tuple(p.get("revoked_messages", ())),
                obligations={m: "open" for m in p.get("obligation_messages", ())})

        elif etype == "CORRECTION_FULFILLED":
            corr = self.corrections[str(p["correction_id"])]
            done = [str(m) for m in p.get("message_ids", ())]
            obligations = dict(corr.obligations)
            for m in done:
                if m in obligations:
                    obligations[m] = "fulfilled"
            self.corrections[corr.correction_id] = CorrectionView(
                **{**corr.__dict__, "obligations": obligations,
                   "fulfilled_at": ts if all(v == "fulfilled" for v in obligations.values())
                   and obligations else corr.fulfilled_at})

        self.seen_event_ids.add(event_id)
        self.events.append(dict(event))
        agg_key = (str(event["aggregate_type"]), str(event["aggregate_id"]))
        self.aggregate_versions[agg_key] = int(event["version"])
        return True
