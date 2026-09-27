"""提示评估引擎：必要且克制。

- 只有签发并在适用时间内的版本才产生提示；
- 触发条件必须由当下事实满足，同一条事实可以支持多个提醒；
- 中医调养建议只以非诊断的生活建议出现，绝不自动升级为疾病判断；
- 停止自我处置条件命中时，同规则的普通生活建议让位，保证就医触发条件醒目。
"""

from __future__ import annotations

from typing import Any

from .models import (
    ADVICE_TCM_WELLNESS,
    PROMPT_LIFE_ADVICE,
    PROMPT_REFERRAL,
    PROMPT_STOP_SELF_CARE,
    STOP_CONDITION,
    SYMPTOM_SET,
    DecisionRecord,
    Prompt,
    ReleaseRevision,
)

#: 触发条件支持的比较符。事实缺失时一律视为不满足。
_OPS = {
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    "in": lambda a, b: a in b,
    "includes_any": lambda a, b: bool(set(a if isinstance(a, (list, tuple)) else [a]) & set(b)),
    "includes_all": lambda a, b: set(b) <= set(a if isinstance(a, (list, tuple)) else [a]),
}


def match_triggers(
    trigger_set: list[dict[str, Any]], facts: dict[str, Any]
) -> tuple[bool, str]:
    """全部触发条件满足才命中；返回 (是否命中, 未命中说明)。"""
    for trigger in trigger_set:
        fact_key = str(trigger.get("fact", ""))
        op = str(trigger.get("op", "=="))
        expected = trigger.get("value")
        actual = facts.get(fact_key)
        compare = _OPS.get(op)
        if compare is None:
            return False, f"不支持的比较符 {op!r}"
        if actual is None:
            return False, f"缺少事实 {fact_key}"
        try:
            if not compare(actual, expected):
                return False, f"事实 {fact_key}={actual!r} 不满足 {op} {expected!r}"
        except TypeError:
            return False, f"事实 {fact_key}={actual!r} 无法与 {expected!r} 比较"
    return True, ""


def _symptom_match(
    stop_fields: dict[str, Any], symptom_set_fields: dict[str, Any], facts: dict[str, Any]
) -> bool:
    reported = facts.get("symptoms") or []
    if not isinstance(reported, (list, tuple)):
        reported = [reported]
    watched = symptom_set_fields.get("symptoms") or []
    if not watched:
        return False
    if stop_fields.get("match", "any") == "all":
        return set(watched) <= set(reported)
    return bool(set(watched) & set(reported))


def _ref(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": item.get("kind"),
        "item_id": item.get("item_id"),
        "version": item.get("version"),
    }


def _basis(
    snapshot: dict[str, Any], items: list[dict[str, Any]], detail: dict[str, Any] | None = None
) -> dict[str, Any]:
    source = snapshot.get("expert_source") or {}
    source_fields = source.get("fields", {})
    return {
        "expert_source": {
            "item_id": source.get("item_id"),
            "version": source.get("version"),
            "expert": source_fields.get("expert"),
            "organization": source_fields.get("organization"),
        },
        "content": [_ref(item) for item in items],
        "detail": detail or {},
    }


def evaluate_release(
    release: ReleaseRevision,
    facts: dict[str, Any],
    audience_group: str,
    now,
    channel: str,
) -> tuple[list[Prompt], list[DecisionRecord]]:
    """评估一个签发版本，返回提示与决定记录（含未出现的原因）。"""
    snapshot = release.content_snapshot
    rule_id = release.rule_id
    region = release.region
    target_group = str(snapshot.get("target_group", ""))
    decisions: list[DecisionRecord] = []

    def record(outcome: str, reason: str, detail: str, basis: dict[str, Any] | None = None) -> None:
        decisions.append(
            DecisionRecord(
                at=now,
                region=region,
                audience_group=audience_group,
                channel=channel,
                outcome=outcome,
                reason=reason,
                detail=detail,
                rule_id=rule_id,
                release_id=release.release_id,
                basis=basis,
            )
        )

    if not release.effective_window.contains(now):
        record("suppressed", "outside_effective_window", "当前时刻不在签发版本的适用时间内")
        return [], decisions
    if audience_group != target_group:
        record("suppressed", "audience_mismatch", f"目标人群为 {target_group}，查询人群为 {audience_group}")
        return [], decisions
    matched, why = match_triggers(list(snapshot.get("trigger_set", [])), facts)
    if not matched:
        record("suppressed", "trigger_not_matched", why)
        return [], decisions

    prompts: list[Prompt] = []

    # 停止自我处置条件优先：命中即发出停止提示与转介提示。
    stop_item = snapshot.get(STOP_CONDITION)
    referral_item = snapshot.get("referral_level")
    stop_fired = False
    if stop_item and referral_item:
        symptom_set = snapshot.get(SYMPTOM_SET) or {}
        if _symptom_match(stop_item.get("fields", {}), symptom_set.get("fields", {}), facts):
            stop_fired = True
            level_fields = referral_item.get("fields", {})
            prompts.append(
                Prompt(
                    kind=PROMPT_STOP_SELF_CARE,
                    text=str(stop_item["fields"]["description"]),
                    rule_id=rule_id,
                    release_id=release.release_id,
                    non_diagnostic=False,
                    basis=_basis(snapshot, [stop_item, symptom_set] if symptom_set else [stop_item]),
                )
            )
            prompts.append(
                Prompt(
                    kind=PROMPT_REFERRAL,
                    text=str(level_fields["instruction"]),
                    rule_id=rule_id,
                    release_id=release.release_id,
                    non_diagnostic=False,
                    basis=_basis(snapshot, [referral_item], {"level": level_fields.get("level")}),
                )
            )

    seen: set[tuple[str, str]] = {(p.kind, p.text) for p in prompts}
    for advice in snapshot.get("life_advices", []):
        fields = advice.get("fields", {})
        category = fields.get("category")
        text = str(fields.get("text", ""))
        key = (PROMPT_LIFE_ADVICE, text)
        if key in seen:
            continue
        if stop_fired:
            record(
                "suppressed",
                "overridden_by_stop_condition",
                "停止自我处置条件已命中，普通生活建议让位",
            )
            continue
        seen.add(key)
        prompts.append(
            Prompt(
                kind=PROMPT_LIFE_ADVICE,
                text=text,
                rule_id=rule_id,
                release_id=release.release_id,
                # 中医调养建议只能作为非诊断生活建议，绝不升级为疾病判断。
                non_diagnostic=category == ADVICE_TCM_WELLNESS,
                basis=_basis(snapshot, [advice], {"category": category}),
            )
        )

    for prompt in prompts:
        record("emitted", "matched", prompt.kind, basis=prompt.basis)
    return prompts, decisions


def dedupe_prompts(prompts: list[Prompt]) -> list[Prompt]:
    """跨版本去重：同类别同文案只保留先出现的一条。"""
    seen: set[tuple[str, str]] = set()
    kept: list[Prompt] = []
    for prompt in prompts:
        key = (prompt.kind, prompt.text)
        if key in seen:
            continue
        seen.add(key)
        kept.append(prompt)
    return kept
