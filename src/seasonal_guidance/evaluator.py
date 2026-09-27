"""评估 API：按地区、人群、当下事实返回必要且克制的行动提示。

只使用县区冻结的签发版本；生效窗、冲突停推、来源更正均按可控时钟与
事件投影判定。每条命中同时给出可追溯依据，供审计端解释。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Mapping

from .model import Facts, RuleVersionView, State


@dataclass(frozen=True)
class SkipReason:
    rule_id: str
    rule_version: int | None
    code: str
    detail: str


@dataclass(frozen=True)
class ActionPrompt:
    rule_id: str
    rule_version: int
    release_id: str
    matched: Mapping[str, object]
    lifestyle_advice: tuple[str, ...]
    stop_self_care: tuple[str, ...]
    referral: Mapping[str, object]
    effective_window: Mapping[str, str]
    component_versions: Mapping[str, list[object]]

    def to_dict(self) -> dict:
        return {
            "rule_id": self.rule_id,
            "rule_version": self.rule_version,
            "release_id": self.release_id,
            "matched": dict(self.matched),
            "lifestyle_advice": list(self.lifestyle_advice),
            "stop_self_care": list(self.stop_self_care),
            "referral": dict(self.referral),
            "effective_window": dict(self.effective_window),
            "component_versions": {k: list(v) for k, v in self.component_versions.items()},
        }


@dataclass(frozen=True)
class EvaluationResult:
    prompts: tuple[ActionPrompt, ...]
    skipped: tuple[SkipReason, ...]
    at: datetime

    def to_dict(self) -> dict:
        return {
            "at": self.at.isoformat(),
            "prompts": [p.to_dict() for p in self.prompts],
            "not_emitted": [
                {"rule_id": s.rule_id, "rule_version": s.rule_version,
                 "code": s.code, "detail": s.detail} for s in self.skipped],
        }


def _as_texts(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)):
        return tuple(str(v) for v in value)
    return (str(value),)


class Evaluator:
    def __init__(self, state: State, clock) -> None:
        self.state = state
        self.clock = clock

    # -- 规则级判定 ---------------------------------------------------------

    def _check_window(self, rule: RuleVersionView, now: datetime) -> SkipReason | None:
        if rule.effective_start is None or rule.effective_end is None:
            return SkipReason(rule.rule_id, rule.version, "not_signed", "规则尚未签发生效")
        if now < rule.effective_start:
            return SkipReason(rule.rule_id, rule.version, "not_yet_effective",
                              f"生效始于 {rule.effective_start.isoformat()}")
        if now >= rule.effective_end:
            return SkipReason(rule.rule_id, rule.version, "expired",
                              f"{rule.effective_end.isoformat()} 已过期")
        return None

    def _check_correction(self, rule: RuleVersionView) -> SkipReason | None:
        assert rule.release_id is not None
        for corr in self.state.corrections.values():
            if rule.release_id in corr.affected_releases:
                return SkipReason(rule.rule_id, rule.version, "corrected_source",
                                  f"来源更正 {corr.correction_id} 影响该发布版本，"
                                  "旧版不再自动推送")
        return None

    def _check_suspension(self, rule: RuleVersionView, region_code: str) -> SkipReason | None:
        for (region, ex_key), suspension in self.state.suspensions.items():
            if region != region_code or not suspension["suspended"]:
                continue
            if ex_key == "*" or ex_key in rule.triggers.exposure_keys:
                return SkipReason(rule.rule_id, rule.version, "push_suspended",
                                  f"暴露 {ex_key} 回执冲突，自动推送已停止：{suspension['reason']}")
        return None

    def _match_triggers(self, rule: RuleVersionView, facts: Facts) -> dict | None:
        t = rule.triggers
        missing: dict[str, object] = {}
        if t.climate_phases and facts.climate_phase not in t.climate_phases:
            missing["climate_phase"] = {
                "required": sorted(t.climate_phases), "actual": facts.climate_phase}
        if t.symptoms_all and not t.symptoms_all <= facts.symptoms:
            missing["symptoms_all"] = sorted(t.symptoms_all - facts.symptoms)
        if t.symptoms_any and not (t.symptoms_any & facts.symptoms):
            missing["symptoms_any"] = sorted(t.symptoms_any)
        active = self.state.active_exposures(facts.region_code)
        missing_exposures = sorted(t.exposure_keys - facts.exposures - active)
        unconfirmed = sorted(k for k in (t.exposure_keys & facts.exposures) if k not in active)
        if t.exposure_keys and missing_exposures:
            missing["exposure_keys"] = missing_exposures
        if unconfirmed:
            missing["exposure_unconfirmed"] = unconfirmed
        if missing:
            return None
        return {
            "climate_phase": facts.climate_phase,
            "symptoms": sorted(facts.symptoms),
            "exposures": sorted((facts.exposures | active) & t.exposure_keys)
            if t.exposure_keys else [],
        }

    # -- 主入口 -------------------------------------------------------------

    def evaluate(self, county_id: str, facts: Facts, *, at: datetime | None = None) -> EvaluationResult:
        now = at or self.clock.now()
        prompts: list[ActionPrompt] = []
        skipped: list[SkipReason] = []

        frozen = [(rule_id, ver) for (county, rule_id), ver in self.state.adoptions.items()
                  if county == county_id]
        if not frozen:
            skipped.append(SkipReason("-", None, "no_frozen_adoption",
                                      f"县区 {county_id} 尚未冻结采用任何规则版本"))
        for rule_id, frozen_version in sorted(frozen):
            rule = self.state.rules[rule_id][frozen_version]
            if rule.target_group != facts.group_id:
                skipped.append(SkipReason(rule_id, rule.version, "group_mismatch",
                                          f"规则面向 {rule.target_group}，当前人群 {facts.group_id}"))
                continue
            reason = self._check_window(rule, now)
            if reason is None:
                reason = self._check_correction(rule)
            if reason is None:
                reason = self._check_suspension(rule, facts.region_code)
            if reason is not None:
                skipped.append(reason)
                continue
            matched = self._match_triggers(rule, facts)
            if matched is None:
                # 给出逐条未命中的触发条件
                t = rule.triggers
                detail_parts = []
                if t.climate_phases and facts.climate_phase not in t.climate_phases:
                    detail_parts.append(f"气候阶段 {facts.climate_phase} 不在 {sorted(t.climate_phases)}")
                if t.symptoms_all and not t.symptoms_all <= facts.symptoms:
                    detail_parts.append(f"缺少症状 {sorted(t.symptoms_all - facts.symptoms)}")
                if t.symptoms_any and not (t.symptoms_any & facts.symptoms):
                    detail_parts.append(f"未出现任一症状 {sorted(t.symptoms_any)}")
                active = self.state.active_exposures(facts.region_code)
                if t.exposure_keys and not (t.exposure_keys <= (facts.exposures | active)):
                    detail_parts.append(
                        f"暴露 {sorted(t.exposure_keys - facts.exposures - active)} 未发生/未确认")
                skipped.append(SkipReason(rule_id, rule.version, "triggers_not_met",
                                          "；".join(detail_parts) or "触发器未命中"))
                continue
            prompts.append(self._build_prompt(rule, matched))
        return EvaluationResult(tuple(prompts), tuple(skipped), now)

    def _build_prompt(self, rule: RuleVersionView, matched: Mapping[str, object]) -> ActionPrompt:
        def comp(kind: str):
            return self.state.resolve_slot(rule, kind)

        advice_comp = comp("lifestyle_advice")
        stop_comp = comp("stop_self_care")
        referral_comp = comp("referral_level")
        advice: tuple[str, ...] = ()
        if advice_comp is not None:
            text = advice_comp.body.get("text", advice_comp.body)
            advice = tuple(f"[生活建议] {line}" for line in _as_texts(text))
        stop: tuple[str, ...] = ()
        if stop_comp is not None:
            stop = tuple(f"[停止自我处置] {line}"
                         for line in _as_texts(stop_comp.body.get("conditions", stop_comp.body)))
        referral = {}
        if referral_comp is not None:
            referral = {"level": referral_comp.body.get("level"),
                        "text": referral_comp.body.get("text")}
        refs = {k: [cid, ver] for k, (cid, ver) in rule.slots.items()}
        assert rule.effective_start is not None and rule.effective_end is not None
        return ActionPrompt(
            rule_id=rule.rule_id, rule_version=rule.version,
            release_id=rule.release_id or "", matched=dict(matched),
            lifestyle_advice=advice, stop_self_care=stop, referral=referral,
            effective_window={"start": rule.effective_start.isoformat(),
                              "end": rule.effective_end.isoformat()},
            component_versions=refs)
