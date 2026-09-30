"""版本化的证据规则引擎。

核心临床安全约束：

1. **年龄从不单独产生矫治结论**。年龄/发育阶段只用于选择观察节奏；
   没有当次结构化检查时，相关风险一律输出「补充检查」并列出缺失证据。
2. **正畸类「开始干预」设专科闸门**：存在严重咬合指征时，必须先有
   正畸专科二诊意见且其结论为「开始干预」，否则规则止步于
   「专科会诊」/「补充检查」，并给出闸门说明。
3. 每个风险因素独立评估、独立产出建议；规则之间只给出「可能相关」
   提示（如持续吮指、口呼吸与咬合异常），由上层决定是否关联问题。
4. 规则为纯函数，阈值集中在 ``RULEBOOK``；版本号与校验和随建议落库，
   使任何一条建议都能回溯到产生它的规则版本。
"""

from __future__ import annotations

import calendar
import hashlib
import json
from datetime import date
from typing import Any, Optional

RULE_VERSION = "1.0.0"

# 阈值与分支条件集中定义；修改临床参数即改变校验和。
RULEBOOK: dict[str, Any] = {
    "严重咬合异常": {
        "覆盖重度_mm": 6,
        "覆盖中度_mm": 4,
        "反𬌗_立即会诊": ["多数前牙", "后牙反𬌗"],
        "反𬌗_早期会诊": ["个别前牙"],
        "拥挤_重度": "重度",
        "拥挤_中度": "中度",
        "干预所需专科": "正畸专科",
        "评估所需影像": ["全景片", "头颅侧位片"],
        "随访月数_重度待会诊": 1,
        "随访月数_中度观察": 6,
        "随访月数_稳定": 12,
    },
    "持续吮指": {
        "持续频率": ["每日多次", "睡眠中持续"],
        "混合牙列持续月数_会诊": 12,
        "戒除观察月数_乳牙列": 6,
        "干预所需专科": "正畸专科",
        "随访月数_引导": 3,
        "随访月数_待会诊": 2,
    },
    "口呼吸疑点": {
        "会诊体征": "白天持续",
        "会诊鼻阻": "双侧",
        "会诊专科": "耳鼻喉科",
        "随访月数_疑点": 2,
        "随访月数_待会诊": 1,
    },
    "龋病进展": {
        "活动牙面_会诊": 3,
        "活动牙面_补充检查": 1,
        "建议影像": "咬翼片",
        "会诊专科": "儿童牙科",
        "随访月数_多发活动龋": 1,
        "随访月数_少量活动龋": 3,
    },
    "清洁能力不足": {
        "菌斑指数_高": 2,
        "菌斑指数_中": 1,
        "需要协助的刷牙水平": ["家长全程刷", "需监督协助"],
        "随访月数_指导": 3,
    },
}

# 风险之间允许建立的关联（仅提示，不合并计划）。
RELATED_RISKS = {
    "持续吮指": ["严重咬合异常"],
    "口呼吸疑点": ["严重咬合异常"],
    "严重咬合异常": ["持续吮指", "口呼吸疑点"],
}


def rulebook_checksum() -> str:
    canonical = json.dumps(
        {"version": RULE_VERSION, "rulebook": RULEBOOK},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


class RuleError(ValueError):
    pass


# -- 证据取值工具 ------------------------------------------------------------


def _metric(bundle: dict[str, Any], key: str) -> Any:
    exam = bundle.get("exam")
    if not exam:
        return None
    return exam.get("metrics", {}).get(key)


def _exam_ref(bundle: dict[str, Any], used: list[str]) -> Optional[dict[str, Any]]:
    exam = bundle.get("exam")
    if not exam:
        return None
    return {
        "kind": "exam",
        "record_type": "检查记录",
        "record_id": exam.get("record_id"),
        "visit_id": exam.get("visit_id"),
        "visit_date": exam.get("visit_date"),
        "used_metrics": used,
    }


def _imaging_types(bundle: dict[str, Any]) -> set[str]:
    return {img.get("image_type") for img in bundle.get("imaging", [])}


def _imaging_refs(bundle: dict[str, Any], types: list[str]) -> list[dict[str, Any]]:
    refs = []
    for img in bundle.get("imaging", []):
        if img.get("image_type") in types:
            refs.append(
                {
                    "kind": "imaging",
                    "record_type": "影像摘要",
                    "record_id": img.get("record_id"),
                    "visit_id": img.get("visit_id"),
                    "visit_date": img.get("visit_date"),
                    "image_type": img.get("image_type"),
                }
            )
    return refs


def _opinion(bundle: dict[str, Any], specialty: str) -> Optional[dict[str, Any]]:
    found = None
    for op in bundle.get("second_opinions", []):
        if op.get("specialty") == specialty:
            found = op  # 取最近一条（上层按时间正序传入）
    return found


def _specialist_gate(
    bundle: dict[str, Any],
    specialty: str,
    missing: list[str],
) -> tuple[bool, Optional[dict[str, Any]], Optional[str]]:
    """返回(闸门是否通过, 二诊证据, 闸门说明)。"""
    op = _opinion(bundle, specialty)
    if not op:
        missing.append(f"{specialty}二诊意见")
        return (
            False,
            None,
            f"存在需矫治评估的指征，但{specialty}尚未会诊；系统不能仅凭年龄或单次检查给出矫治结论",
        )
    ref = {
        "kind": "second_opinion",
        "record_type": "二诊意见",
        "record_id": op.get("record_id"),
        "visit_id": op.get("visit_id"),
        "visit_date": op.get("visit_date"),
        "specialty": specialty,
        "specialist_recommendation": op.get("recommendation"),
    }
    if op.get("recommendation") != "开始干预":
        return (
            False,
            ref,
            f"{specialty}意见为「{op.get('recommendation')}」，未达到开始干预条件",
        )
    return True, ref, None


def _result(
    risk: str,
    triggered: bool,
    recommendation: str,
    rationale: str,
    follow_up_months: Optional[int],
    evidence: list[dict[str, Any]],
    missing: Optional[list[str]] = None,
    gate_note: Optional[str] = None,
    gate_blocked: bool = False,
) -> dict[str, Any]:
    return {
        "risk_factor": risk,
        "triggered": triggered,
        "recommendation_type": recommendation,
        "rationale": rationale,
        "missing_evidence": missing or [],
        "gate_note": gate_note,
        "gate_blocked": gate_blocked,
        "follow_up_months": follow_up_months,
        "evidence": evidence,
        "related_risks": RELATED_RISKS.get(risk, []),
    }


# -- 各风险规则 ---------------------------------------------------------------


def _rule_occlusion(bundle: dict[str, Any]) -> dict[str, Any]:
    risk = "严重咬合异常"
    cfg = RULEBOOK[risk]
    used = ["前牙覆盖", "反𬌗范围", "牙列拥挤"]
    missing: list[str] = []
    exam = bundle.get("exam")
    if not exam:
        missing.append("当次结构化咬合检查（前牙覆盖、反𬌗范围、牙列拥挤）")
        return _result(
            risk,
            triggered=False,
            recommendation="补充检查",
            rationale=(
                f"当前为{bundle.get('stage', '发育阶段未记录')}，但缺少当次结构化咬合检查；"
                "牙列是否不齐不能仅凭年龄或家长观察判断是否需要矫治"
            ),
            follow_up_months=cfg["随访月数_中度观察"],
            evidence=[],
            missing=missing,
            gate_note="证据不足，禁止据此开始矫治",
        )

    overjet = _metric(bundle, "前牙覆盖")
    crossbite = _metric(bundle, "反𬌗范围")
    crowding = _metric(bundle, "牙列拥挤")
    absent = [name for name, v in zip(used, [overjet, crossbite, crowding]) if v is None]
    if absent:
        missing.extend(f"检查指标：{name}" for name in absent)
        return _result(
            risk,
            False,
            "补充检查",
            "咬合检查指标不完整，无法判断严重程度，需补测后再评估",
            cfg["随访月数_中度观察"],
            [_exam_ref(bundle, used)],
            missing,
            gate_note="证据不足，禁止据此开始矫治",
        )

    evidence = [_exam_ref(bundle, used)]
    severe = (
        (isinstance(overjet, (int, float)) and overjet >= cfg["覆盖重度_mm"])
        or crossbite in cfg["反𬌗_立即会诊"]
        or crowding == cfg["拥挤_重度"]
    )
    moderate = (
        (isinstance(overjet, (int, float)) and overjet >= cfg["覆盖中度_mm"])
        or crossbite in cfg["反𬌗_早期会诊"]
        or crowding == cfg["拥挤_中度"]
    )
    trend = bundle.get("metric_trends", {}).get("前牙覆盖")

    if severe:
        reasons = []
        if isinstance(overjet, (int, float)) and overjet >= cfg["覆盖重度_mm"]:
            reasons.append(f"前牙覆盖{overjet}mm（≥{cfg['覆盖重度_mm']}mm）")
        if crossbite in cfg["反𬌗_立即会诊"]:
            reasons.append(f"反𬌗范围：{crossbite}")
        if crowding == "重度":
            reasons.append("牙列重度拥挤")
        if trend == "加重":
            reasons.append("覆盖较上次检查加重")
        # 影像证据
        have_types = _imaging_types(bundle)
        for need in cfg["评估所需影像"]:
            if need not in have_types:
                missing.append(f"影像检查：{need}")
        evidence.extend(_imaging_refs(bundle, cfg["评估所需影像"]))
        if missing:
            # 先补齐影像，再请专科会诊——证据不足时绝不形成矫治结论
            return _result(
                risk,
                True,
                "补充检查",
                "发现严重咬合指征（" + "、".join(reasons) + "），但评估影像尚不完整",
                cfg["随访月数_重度待会诊"],
                evidence,
                missing,
                gate_note="补齐影像并完成专科会诊后再决定",
                gate_blocked=True,
            )
        # 专科闸门
        passed, opinion_ref, gate_note = _specialist_gate(
            bundle, cfg["干预所需专科"], missing
        )
        if opinion_ref:
            evidence.append(opinion_ref)
        if not passed:
            return _result(
                risk,
                True,
                "专科会诊",
                "发现严重咬合指征（" + "、".join(reasons) + "），需正畸专科判断矫治时机",
                cfg["随访月数_重度待会诊"],
                evidence,
                missing,
                gate_note=gate_note,
                gate_blocked=True,
            )
        return _result(
            risk,
            True,
            "开始干预",
            "严重咬合指征明确，影像齐备，且正畸专科已建议开始干预",
            cfg["随访月数_中度观察"],
            evidence,
        )

    if moderate:
        reasons = []
        if isinstance(overjet, (int, float)) and overjet >= cfg["覆盖中度_mm"]:
            reasons.append(f"前牙覆盖{overjet}mm")
        if crossbite in cfg["反𬌗_早期会诊"]:
            reasons.append("个别前牙反𬌗")
        if crowding == "中度":
            reasons.append("牙列中度拥挤")
        if crossbite in cfg["反𬌗_早期会诊"]:
            # 个别前牙反𬌗是少数值得早评估的情况，先转专科判断而非直接矫治
            passed, opinion_ref, gate_note = _specialist_gate(
                bundle, cfg["干预所需专科"], missing
            )
            if opinion_ref:
                evidence.append(opinion_ref)
            if passed:
                return _result(
                    risk,
                    True,
                    "开始干预",
                    "个别前牙反𬌗且正畸专科已建议开始干预",
                    cfg["随访月数_中度观察"],
                    evidence,
                )
            return _result(
                risk,
                True,
                "专科会诊",
                "发现" + "、".join(reasons) + "，属于可考虑早期评估的情况，转正畸专科",
                cfg["随访月数_重度待会诊"],
                evidence,
                missing,
                gate_note=gate_note,
                gate_blocked=True,
            )
        return _result(
            risk,
            True,
            "继续观察",
            "发现" + "、".join(reasons) + "，处于混合牙列变化期，按计划复查趋势",
            cfg["随访月数_中度观察"],
            evidence,
        )

    return _result(
        risk,
        False,
        "继续观察",
        f"覆盖{overjet}mm、反𬌗「{crossbite}」、拥挤「{crowding}」，未见明显异常，例行观察",
        cfg["随访月数_稳定"],
        evidence,
    )


def _rule_finger_sucking(bundle: dict[str, Any]) -> dict[str, Any]:
    risk = "持续吮指"
    cfg = RULEBOOK[risk]
    missing: list[str] = []
    if not bundle.get("exam"):
        missing.append("习惯询问：吮指频率、持续月数")
        return _result(
            risk,
            False,
            "补充检查",
            "缺少吮指习惯的结构化记录，需先了解频率与持续时间",
            cfg["随访月数_引导"],
            [],
            missing,
        )
    freq = _metric(bundle, "吮指频率")
    months = _metric(bundle, "吮指持续月数")
    evidence = [_exam_ref(bundle, ["吮指频率", "吮指持续月数"])]
    if freq is None:
        return _result(
            risk,
            False,
            "补充检查",
            "缺少吮指频率记录",
            cfg["随访月数_引导"],
            evidence,
            ["检查指标：吮指频率"],
        )
    if freq in ("已戒除", "偶尔"):
        return _result(
            risk,
            False,
            "继续观察",
            f"吮指频率为「{freq}」，暂无持续风险，维持例行观察",
            cfg["随访月数_引导"],
            evidence,
        )

    stage = bundle.get("stage")
    persistent = freq in cfg["持续频率"]
    long_mixed = (
        stage == "混合牙列期"
        and isinstance(months, (int, float))
        and months >= cfg["混合牙列持续月数_会诊"]
        and freq == "睡眠中持续"
    )
    care_ref = _home_care_ref(bundle, ["吮指戒除引导"])
    evidence.extend(care_ref)

    if long_mixed:
        passed, opinion_ref, gate_note = _specialist_gate(
            bundle, cfg["干预所需专科"], missing
        )
        if opinion_ref:
            evidence.append(opinion_ref)
        if passed:
            return _result(
                risk,
                True,
                "开始干预",
                f"混合牙列期睡眠中持续吮指已{int(months)}个月，正畸专科建议干预",
                cfg["随访月数_引导"],
                evidence,
            )
        return _result(
            risk,
            True,
            "专科会诊",
            f"混合牙列期睡眠中持续吮指已{int(months)}个月，可能影响咬合，转正畸专科评估",
            cfg["随访月数_待会诊"],
            evidence,
            missing,
            gate_note=gate_note or "先行为引导，矫治器干预须经正畸专科确认",
            gate_blocked=True,
        )

    if stage == "乳牙列期" and persistent:
        return _result(
            risk,
            True,
            "日常护理",
            f"乳牙列期吮指「{freq}」，以家庭戒除引导为主，多数可随发育自行缓解；"
            f"若超过{cfg['戒除观察月数_乳牙列']}个月仍持续再升级",
            cfg["随访月数_引导"],
            evidence,
        )

    return _result(
        risk,
        True,
        "日常护理",
        f"吮指「{freq}」，先进行家庭戒除引导并记录频率变化",
        cfg["随访月数_引导"],
        evidence,
    )


def _rule_mouth_breathing(bundle: dict[str, Any]) -> dict[str, Any]:
    risk = "口呼吸疑点"
    cfg = RULEBOOK[risk]
    missing: list[str] = []
    if not bundle.get("exam"):
        missing.append("体征询问：口呼吸体征、鼻通气受阻")
        return _result(
            risk,
            False,
            "补充检查",
            "缺少口呼吸相关体征记录，需在就诊时重复观察确认",
            cfg["随访月数_疑点"],
            [],
            missing,
        )
    sign = _metric(bundle, "口呼吸体征")
    nasal = _metric(bundle, "鼻通气受阻")
    evidence = [_exam_ref(bundle, ["口呼吸体征", "鼻通气受阻"])]
    evidence.extend(_home_care_ref(bundle, ["口呼吸观察记录"]))
    if sign is None:
        return _result(
            risk,
            False,
            "补充检查",
            "缺少口呼吸体征记录",
            cfg["随访月数_疑点"],
            evidence,
            ["检查指标：口呼吸体征"],
        )
    if sign == "无":
        return _result(
            risk,
            False,
            "继续观察",
            "无口呼吸体征，例行观察",
            cfg["随访月数_疑点"],
            evidence,
        )
    if sign == cfg["会诊体征"] or nasal == cfg["会诊鼻阻"]:
        return _result(
            risk,
            True,
            "专科会诊",
            f"口呼吸体征「{sign}」、鼻通气受阻「{nasal or '未记录'}」，"
            f"需{cfg['会诊专科']}排查上气道原因；牙颌面处置须在气道评估之后",
            cfg["随访月数_待会诊"],
            evidence,
            gate_note="本规则不会直接建议正畸干预，先排除鼻/咽病因",
        )
    return _result(
        risk,
        True,
        "补充检查",
        f"夜间可疑口呼吸（鼻通气受阻「{nasal or '未记录'}」），"
        "需家庭记录并复查，必要时耳鼻喉科评估",
        cfg["随访月数_疑点"],
        evidence,
    )


def _rule_caries(bundle: dict[str, Any]) -> dict[str, Any]:
    risk = "龋病进展"
    cfg = RULEBOOK[risk]
    missing: list[str] = []
    if not bundle.get("exam"):
        missing.append("龋病检查：dmft、活动性龋损牙面数")
        return _result(
            risk,
            False,
            "补充检查",
            "缺少当次龋病结构化检查，无法判断龋病是否进展",
            cfg["随访月数_少量活动龋"],
            [],
            missing,
        )
    dmft = _metric(bundle, "dmft")
    active = _metric(bundle, "活动龋牙面数")
    evidence = [_exam_ref(bundle, ["dmft", "活动龋牙面数"])]
    if active is None:
        return _result(
            risk,
            False,
            "补充检查",
            "缺少活动性龋损牙面数记录",
            cfg["随访月数_少量活动龋"],
            evidence,
            ["检查指标：活动龋牙面数"],
        )
    trends = bundle.get("metric_trends", {})
    worsening = trends.get("活动龋牙面数") == "加重" or trends.get("dmft") == "加重"
    have_types = _imaging_types(bundle)
    evidence.extend(_imaging_refs(bundle, [cfg["建议影像"]]))
    if cfg["建议影像"] not in have_types:
        missing.append(f"影像检查：{cfg['建议影像']}（确认邻面龋）")

    if active >= cfg["活动牙面_会诊"] or worsening:
        detail = f"活动性龋损{active}个牙面"
        if dmft is not None:
            detail += f"、dmft={dmft}"
        if worsening:
            detail += "，且较上次检查进展"
        return _result(
            risk,
            True,
            "专科会诊",
            f"{detail}，转{cfg['会诊专科']}制定充填与预防方案",
            cfg["随访月数_多发活动龋"],
            evidence,
            missing,
            gate_note="龋病治疗由儿童牙科执行，本系统不据年龄推断",
        )
    if active >= cfg["活动牙面_补充检查"]:
        return _result(
            risk,
            True,
            "补充检查",
            f"发现{active}个活动性龋损牙面，需明确范围（建议咬翼片）并安排处置",
            cfg["随访月数_少量活动龋"],
            evidence,
            missing,
        )
    return _result(
        risk,
        False,
        "日常护理",
        f"未见活动性龋损（dmft={dmft if dmft is not None else '未记录'}），维持防龋护理",
        cfg["随访月数_少量活动龋"],
        evidence,
    )


def _home_care_ref(bundle: dict[str, Any], keys: list[str]) -> list[dict[str, Any]]:
    hc = bundle.get("home_care")
    if not hc:
        return []
    items = hc.get("items", {})
    used = {k: items.get(k) for k in keys if k in items}
    if not used:
        return []
    return [
        {
            "kind": "home_care",
            "record_type": "家庭护理",
            "record_id": hc.get("record_id"),
            "visit_id": hc.get("visit_id"),
            "visit_date": hc.get("visit_date"),
            "used_items": used,
        }
    ]


def _rule_hygiene(bundle: dict[str, Any]) -> dict[str, Any]:
    risk = "清洁能力不足"
    cfg = RULEBOOK[risk]
    missing: list[str] = []
    if not bundle.get("exam"):
        missing.append("菌斑指数与刷牙独立性评估")
        return _result(
            risk,
            False,
            "补充检查",
            "缺少清洁能力评估，无法给出针对性护理指导",
            cfg["随访月数_指导"],
            [],
            missing,
        )
    plaque = _metric(bundle, "菌斑指数")
    independence = _metric(bundle, "刷牙独立性")
    evidence = [_exam_ref(bundle, ["菌斑指数", "刷牙独立性"])]
    evidence.extend(
        _home_care_ref(bundle, ["每日刷牙两次", "家长协助刷牙", "含氟牙膏", "牙线使用"])
    )
    if plaque is None:
        return _result(
            risk,
            False,
            "补充检查",
            "缺少菌斑指数记录",
            cfg["随访月数_指导"],
            evidence,
            ["检查指标：菌斑指数"],
        )

    poor_adherence = _poor_adherence(bundle)
    if plaque >= cfg["菌斑指数_高"] or (
        plaque >= cfg["菌斑指数_中"]
        and (independence in cfg["需要协助的刷牙水平"] or poor_adherence)
    ):
        details = [f"菌斑指数{plaque}"]
        if independence:
            details.append(f"刷牙「{independence}」")
        if poor_adherence:
            details.append("家庭护理执行不充分")
        return _result(
            risk,
            True,
            "日常护理",
            "、".join(details) + "，需家长协助刷牙并强化氟化物与牙线使用",
            cfg["随访月数_指导"],
            evidence,
        )
    return _result(
        risk,
        False,
        "日常护理",
        f"菌斑指数{plaque}，清洁状况可，维持现有家庭护理",
        cfg["随访月数_指导"],
        evidence,
    )


def _poor_adherence(bundle: dict[str, Any]) -> bool:
    hc = bundle.get("home_care")
    if not hc:
        return False
    for key in ("每日刷牙两次", "含氟牙膏"):
        entry = hc.get("items", {}).get(key)
        if isinstance(entry, dict) and entry.get("执行") in ("偶尔", "未执行"):
            return True
    return False


_RULES = {
    "严重咬合异常": _rule_occlusion,
    "持续吮指": _rule_finger_sucking,
    "口呼吸疑点": _rule_mouth_breathing,
    "龋病进展": _rule_caries,
    "清洁能力不足": _rule_hygiene,
}


# -- 对外入口 ----------------------------------------------------------------


def evaluate(bundle: dict[str, Any]) -> dict[str, Any]:
    """对证据包执行全部风险规则。

    ``bundle`` 由上层组装，关键字段：
      stage, exam, imaging[], home_care, second_opinions[], metric_trends{}
    """
    results = []
    for risk, fn in _RULES.items():
        r = fn(bundle)
        r["risk_factor"] = risk
        results.append(r)
    age_only = bundle.get("exam") is None
    blocked = any(r.get("gate_blocked") for r in results)
    return {
        "rule_version": RULE_VERSION,
        "rule_checksum": rulebook_checksum(),
        "age_only_evidence": age_only,
        # 仅凭年龄/发育阶段、或任一矫治类输出被专科闸门拦下时，
        # 系统明确标记尚不能产生矫治结论
        "treatment_conclusion_allowed": not age_only and not blocked,
        "results": results,
    }


def add_months(day: str | date, months: int) -> str:
    """按月推算复诊日期（目标月天数不足时收敛到月末）。"""
    if isinstance(day, str):
        d = date.fromisoformat(day)
    else:
        d = day
    month_index = d.year * 12 + (d.month - 1) + months
    year, month0 = divmod(month_index, 12)
    month = month0 + 1
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(d.day, last_day)).isoformat()
