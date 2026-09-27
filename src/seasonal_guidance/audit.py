"""审计解释：一条消息为何出现、为何没有出现，以及采用了哪位专家的哪版依据。"""

from __future__ import annotations

from typing import Any, Optional

from .models import DecisionRecord, Message, ReleaseRevision
from .service import IssuanceService


def explain_message(service: IssuanceService, message_id: str) -> dict[str, Any]:
    """解释一条消息为何出现：规则、签发版本、专家依据与内容版本。"""
    message = service.message(message_id)
    release = service.release(message.release_id)
    return {
        "message_id": message.message_id,
        "status": message.status,
        "kind": message.kind,
        "text": message.text,
        "region": message.region,
        "county": message.county,
        "audience_group": message.audience_group,
        "created_at": message.created_at.isoformat(),
        "sent_at": message.sent_at.isoformat() if message.sent_at else None,
        "delivered_at": message.delivered_at.isoformat() if message.delivered_at else None,
        "release": _release_summary(release),
        "expert_source": message.basis.get("expert_source", {}),
        "content_basis": message.basis.get("content", []),
    }


def explain_decisions(
    service: IssuanceService,
    region: Optional[str] = None,
    audience_group: Optional[str] = None,
    outcome: Optional[str] = None,
) -> list[dict[str, Any]]:
    """按条件列出决定记录，回答“为何出现 / 为何没有出现”。"""
    records = service.decisions
    if region is not None:
        records = [d for d in records if d.region == region]
    if audience_group is not None:
        records = [d for d in records if d.audience_group in (audience_group, "*")]
    if outcome is not None:
        records = [d for d in records if d.outcome == outcome]
    return [_decision_summary(service, d) for d in records]


def explain_absence(service: IssuanceService, region: str, audience_group: str) -> list[dict[str, Any]]:
    """某地区某人群没有收到提示的原因清单。"""
    suppressed = explain_decisions(service, region=region, audience_group=audience_group, outcome="suppressed")
    held = explain_decisions(service, region=region, audience_group=audience_group, outcome="held")
    return suppressed + held


def _release_summary(release: ReleaseRevision) -> dict[str, Any]:
    return {
        "release_id": release.release_id,
        "rule_id": release.rule_id,
        "rule_version": release.rule_version,
        "signed_by": release.signed_by,
        "signed_at": release.signed_at.isoformat(),
        "effective_window": release.effective_window.to_dict(),
        "status": release.status,
    }


def _decision_summary(service: IssuanceService, decision: DecisionRecord) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "at": decision.at.isoformat(),
        "region": decision.region,
        "audience_group": decision.audience_group,
        "channel": decision.channel,
        "outcome": decision.outcome,
        "reason": decision.reason,
        "detail": decision.detail,
        "rule_id": decision.rule_id,
        "release_id": decision.release_id,
    }
    if decision.release_id:
        try:
            summary["release"] = _release_summary(service.release(decision.release_id))
        except Exception:
            pass
    if decision.basis:
        summary["expert_source"] = decision.basis.get("expert_source", {})
        summary["content_basis"] = decision.basis.get("content", [])
    return summary
