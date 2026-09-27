"""签发台领域对象。

地域气候阶段、风险人群、症状组合、暴露场景、生活建议、停止自我处置条件、
转介级别、专家来源与适用时间分别版本化；规则把它们装配为可审阅、可签发的
整体，县区采用时冻结版本，已送达消息保留原文。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Optional

# ---- 内容类别（各自独立版本化） ----

CLIMATE_PHASE = "climate_phase"
RISK_GROUP = "risk_group"
SYMPTOM_SET = "symptom_set"
EXPOSURE_SCENARIO = "exposure_scenario"
LIFE_ADVICE = "life_advice"
STOP_CONDITION = "stop_condition"
REFERRAL_LEVEL = "referral_level"
EXPERT_SOURCE = "expert_source"

CONTENT_KINDS = (
    CLIMATE_PHASE,
    RISK_GROUP,
    SYMPTOM_SET,
    EXPOSURE_SCENARIO,
    LIFE_ADVICE,
    STOP_CONDITION,
    REFERRAL_LEVEL,
    EXPERT_SOURCE,
)

#: 各内容类别必填的属性字段。
CONTENT_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    CLIMATE_PHASE: ("region", "name", "window"),
    RISK_GROUP: ("label",),
    SYMPTOM_SET: ("symptoms",),
    EXPOSURE_SCENARIO: ("category", "description"),
    LIFE_ADVICE: ("text", "category"),
    STOP_CONDITION: ("description", "symptom_set_id"),
    REFERRAL_LEVEL: ("level", "instruction"),
    EXPERT_SOURCE: ("expert", "organization", "title"),
}

#: 生活建议类别。中医调养建议只能作为生活建议下发，不得自动升级为疾病判断。
ADVICE_GENERAL = "general"
ADVICE_TCM_WELLNESS = "tcm_wellness"
ADVICE_CATEGORIES = (ADVICE_GENERAL, ADVICE_TCM_WELLNESS)

#: 提示类别。
PROMPT_LIFE_ADVICE = "life_advice"
PROMPT_STOP_SELF_CARE = "stop_self_care"
PROMPT_REFERRAL = "referral"

#: 规则状态。
RULE_PROPOSED = "proposed"
RULE_APPROVED = "approved"
RULE_REJECTED = "rejected"

#: 消息状态。只有 queued（尚未发送）允许被更正撤回。
MESSAGE_QUEUED = "queued"
MESSAGE_SENT = "sent"
MESSAGE_DELIVERED = "delivered"
MESSAGE_RETRACTED = "retracted"

#: 暴露合并状态。
EXPOSURE_MERGED = "merged"
EXPOSURE_CONFLICTED = "conflicted"

#: 纠正义务状态。
OBLIGATION_OPEN = "open"
OBLIGATION_FULFILLED = "fulfilled"
OBLIGATION_OVERDUE = "overdue"


def parse_instant(value: Any) -> datetime:
    """解析必须携带时区的时间字符串。"""
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"时间必须携带时区: {value!r}")
    return parsed


def _iso(moment: Optional[datetime]) -> Optional[str]:
    return moment.isoformat() if moment is not None else None


@dataclass(frozen=True)
class TimeWindow:
    """适用时间窗口，end 为 None 表示不限期。"""

    start: datetime
    end: Optional[datetime] = None

    def contains(self, moment: datetime) -> bool:
        if moment < self.start:
            return False
        return self.end is None or moment < self.end

    def to_dict(self) -> dict[str, Any]:
        return {"start": self.start.isoformat(), "end": _iso(self.end)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TimeWindow":
        end = data.get("end")
        return cls(
            start=parse_instant(data["start"]),
            end=parse_instant(end) if end else None,
        )


@dataclass(frozen=True)
class ContentItem:
    """分别版本化的一份内容。fields 为各类别专属属性（JSON 可序列化）。"""

    kind: str
    item_id: str
    version: int
    fields: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "item_id": self.item_id,
            "version": self.version,
            "fields": dict(self.fields),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ContentItem":
        return cls(
            kind=str(data["kind"]),
            item_id=str(data["item_id"]),
            version=int(data["version"]),
            fields=dict(data.get("fields", {})),
        )

    def ref(self) -> dict[str, Any]:
        """审计依据中的引用形态。"""
        return {"kind": self.kind, "item_id": self.item_id, "version": self.version}


@dataclass
class GuidanceRule:
    """规则作者提出的内容，版本从 1 开始递增。"""

    rule_id: str
    version: int
    author: str
    target_group: str
    trigger_set: list[dict[str, Any]]
    climate_phase_id: str
    scenario_id: str
    advice_ids: list[str]
    expert_source_id: str
    stop_condition_id: Optional[str] = None
    referral_level_id: Optional[str] = None
    status: str = RULE_PROPOSED
    reviewer: Optional[str] = None
    review_note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "version": self.version,
            "author": self.author,
            "target_group": self.target_group,
            "trigger_set": [dict(t) for t in self.trigger_set],
            "climate_phase_id": self.climate_phase_id,
            "scenario_id": self.scenario_id,
            "advice_ids": list(self.advice_ids),
            "expert_source_id": self.expert_source_id,
            "stop_condition_id": self.stop_condition_id,
            "referral_level_id": self.referral_level_id,
            "status": self.status,
            "reviewer": self.reviewer,
            "review_note": self.review_note,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "GuidanceRule":
        return cls(
            rule_id=str(data["rule_id"]),
            version=int(data["version"]),
            author=str(data["author"]),
            target_group=str(data["target_group"]),
            trigger_set=[dict(t) for t in data.get("trigger_set", [])],
            climate_phase_id=str(data["climate_phase_id"]),
            scenario_id=str(data["scenario_id"]),
            advice_ids=list(data.get("advice_ids", [])),
            expert_source_id=str(data["expert_source_id"]),
            stop_condition_id=data.get("stop_condition_id"),
            referral_level_id=data.get("referral_level_id"),
            status=str(data.get("status", RULE_PROPOSED)),
            reviewer=data.get("reviewer"),
            review_note=str(data.get("review_note", "")),
        )


@dataclass
class ReleaseRevision:
    """签发版本：签发时冻结规则版本与全部内容快照。"""

    release_id: str
    rule_id: str
    rule_version: int
    signed_by: str
    signed_at: datetime
    effective_window: TimeWindow
    content_snapshot: dict[str, Any]
    status: str = "active"  # active | corrected

    def to_dict(self) -> dict[str, Any]:
        return {
            "release_id": self.release_id,
            "rule_id": self.rule_id,
            "rule_version": self.rule_version,
            "signed_by": self.signed_by,
            "signed_at": self.signed_at.isoformat(),
            "effective_window": self.effective_window.to_dict(),
            "content_snapshot": self.content_snapshot,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ReleaseRevision":
        return cls(
            release_id=str(data["release_id"]),
            rule_id=str(data["rule_id"]),
            rule_version=int(data["rule_version"]),
            signed_by=str(data["signed_by"]),
            signed_at=parse_instant(data["signed_at"]),
            effective_window=TimeWindow.from_dict(data["effective_window"]),
            content_snapshot=dict(data["content_snapshot"]),
            status=str(data.get("status", "active")),
        )

    @property
    def region(self) -> str:
        phase = self.content_snapshot.get(CLIMATE_PHASE) or {}
        return str(phase.get("fields", {}).get("region", ""))


@dataclass
class Adoption:
    """县区采用记录：采用即冻结 rule_version，之后的签发不影响本县。"""

    county: str
    region: str
    release_id: str
    rule_version: int
    adopted_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "county": self.county,
            "region": self.region,
            "release_id": self.release_id,
            "rule_version": self.rule_version,
            "adopted_at": self.adopted_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Adoption":
        return cls(
            county=str(data["county"]),
            region=str(data["region"]),
            release_id=str(data["release_id"]),
            rule_version=int(data["rule_version"]),
            adopted_at=parse_instant(data["adopted_at"]),
        )


@dataclass
class Message:
    """待推送或已推送的消息。已送达消息原文不可改写。"""

    message_id: str
    release_id: str
    county: str
    region: str
    audience_group: str
    kind: str
    text: str
    basis: dict[str, Any]
    status: str
    created_at: datetime
    sent_at: Optional[datetime] = None
    delivered_at: Optional[datetime] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "release_id": self.release_id,
            "county": self.county,
            "region": self.region,
            "audience_group": self.audience_group,
            "kind": self.kind,
            "text": self.text,
            "basis": self.basis,
            "status": self.status,
            "created_at": self.created_at.isoformat(),
            "sent_at": _iso(self.sent_at),
            "delivered_at": _iso(self.delivered_at),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Message":
        sent_at = data.get("sent_at")
        delivered_at = data.get("delivered_at")
        return cls(
            message_id=str(data["message_id"]),
            release_id=str(data["release_id"]),
            county=str(data["county"]),
            region=str(data["region"]),
            audience_group=str(data["audience_group"]),
            kind=str(data["kind"]),
            text=str(data["text"]),
            basis=dict(data.get("basis", {})),
            status=str(data["status"]),
            created_at=parse_instant(data["created_at"]),
            sent_at=parse_instant(sent_at) if sent_at else None,
            delivered_at=parse_instant(delivered_at) if delivered_at else None,
        )


@dataclass
class ExposureRecord:
    """同一暴露在不同渠道回执的合并结果。"""

    exposure_key: str
    region: str
    channels: list[str] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)
    status: str = EXPOSURE_MERGED
    conflicts: dict[str, list[Any]] = field(default_factory=dict)
    receipt_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "exposure_key": self.exposure_key,
            "region": self.region,
            "channels": list(self.channels),
            "facts": dict(self.facts),
            "status": self.status,
            "conflicts": {k: list(v) for k, v in self.conflicts.items()},
            "receipt_ids": list(self.receipt_ids),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ExposureRecord":
        return cls(
            exposure_key=str(data["exposure_key"]),
            region=str(data["region"]),
            channels=list(data.get("channels", [])),
            facts=dict(data.get("facts", {})),
            status=str(data.get("status", EXPOSURE_MERGED)),
            conflicts={k: list(v) for k, v in data.get("conflicts", {}).items()},
            receipt_ids=list(data.get("receipt_ids", [])),
        )


@dataclass
class CorrectionObligation:
    """来源更正产生的纠正义务，范围明确到县、消息与人群。"""

    obligation_id: str
    reason: str
    affected_releases: list[str]
    scope: dict[str, Any]
    raised_at: datetime
    due_at: datetime
    status: str = OBLIGATION_OPEN
    fulfilled_at: Optional[datetime] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "obligation_id": self.obligation_id,
            "reason": self.reason,
            "affected_releases": list(self.affected_releases),
            "scope": self.scope,
            "raised_at": self.raised_at.isoformat(),
            "due_at": self.due_at.isoformat(),
            "status": self.status,
            "fulfilled_at": _iso(self.fulfilled_at),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CorrectionObligation":
        fulfilled_at = data.get("fulfilled_at")
        return cls(
            obligation_id=str(data["obligation_id"]),
            reason=str(data["reason"]),
            affected_releases=list(data["affected_releases"]),
            scope=dict(data.get("scope", {})),
            raised_at=parse_instant(data["raised_at"]),
            due_at=parse_instant(data["due_at"]),
            status=str(data.get("status", OBLIGATION_OPEN)),
            fulfilled_at=parse_instant(fulfilled_at) if fulfilled_at else None,
        )


@dataclass(frozen=True)
class Prompt:
    """评估返回的行动提示，携带完整依据以便审计。"""

    kind: str
    text: str
    rule_id: str
    release_id: str
    non_diagnostic: bool
    basis: dict[str, Any]


@dataclass
class DecisionRecord:
    """一次评估的决定记录：为何出现、为何没有出现。"""

    at: datetime
    region: str
    audience_group: str
    channel: str  # query | auto_push
    outcome: str  # emitted | suppressed | held
    reason: str
    detail: str = ""
    rule_id: Optional[str] = None
    release_id: Optional[str] = None
    basis: Optional[dict[str, Any]] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "at": self.at.isoformat(),
            "region": self.region,
            "audience_group": self.audience_group,
            "channel": self.channel,
            "outcome": self.outcome,
            "reason": self.reason,
            "detail": self.detail,
            "rule_id": self.rule_id,
            "release_id": self.release_id,
            "basis": self.basis,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "DecisionRecord":
        return cls(
            at=parse_instant(data["at"]),
            region=str(data["region"]),
            audience_group=str(data["audience_group"]),
            channel=str(data["channel"]),
            outcome=str(data["outcome"]),
            reason=str(data["reason"]),
            detail=str(data.get("detail", "")),
            rule_id=data.get("rule_id"),
            release_id=data.get("release_id"),
            basis=data.get("basis"),
        )
