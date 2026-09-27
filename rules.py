"""版本化的临床规则引擎。

设计原则（对应团队共识）：

1. 年龄本身永远不构成"开始干预"的充分条件。年龄只能决定发育阶段相关的
   观察节奏，任何矫治结论都必须有临床表现 + 影像摘要 + 专科意见等证据。
2. 每条规则输出三样东西：建议类型、理由、*仍缺少的证据*。前端据此向家长
   解释"为什么这次是观察 / 为什么要补充检查 / 为什么转专科"，而不是只给结论。
3. 规则整体带版本号。规则发布后内容冻结，历史建议保存当时的规则版本与
   证据快照，因此任何一次旧建议都能被忠实复现。
4. 规则词表（发育阶段 / 风险因素 / 建议类型）必须与 domain.json 一致，
   `service.py --check` 会校验。

引擎对不同问题（龋病、吮指、口呼吸、咬合/颌骨）分别评估，输出相互独立的
议题（issue），上层可以为每个议题建立相互独立但可关联的计划。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

RULE_VERSION = "2026.1.0"

# 建议类型（需与 domain.json 一致）
ADVICE_DAILY_CARE = "日常护理"
ADVICE_OBSERVE = "继续观察"
ADVICE_SUPPLEMENT = "补充检查"
ADVICE_REFERRAL = "专科会诊"
ADVICE_INTERVENE = "开始干预"

ADVICE_TYPES = [
    ADVICE_DAILY_CARE,
    ADVICE_OBSERVE,
    ADVICE_SUPPLEMENT,
    ADVICE_REFERRAL,
    ADVICE_INTERVENE,
]

# 发育阶段
STAGE_PRIMARY = "乳牙列期"
STAGE_MIXED = "混合牙列期"
STAGE_EARLY_PERMANENT = "恒牙列早期"
STAGES = [STAGE_PRIMARY, STAGE_MIXED, STAGE_EARLY_PERMANENT]

# 风险因素（议题键 -> 词表名称）
ISSUE_CARIES = "caries"          # 龋病进展
ISSUE_SEVERE_MALOCCLUSION = "severe_malocclusion"  # 严重咬合异常
ISSUE_FINGER_HABIT = "finger_habit"  # 持续吮指
ISSUE_MOUTH_BREATHING = "mouth_breathing"  # 口呼吸疑点
ISSUE_POOR_HYGIENE = "poor_hygiene"  # 清洁能力不足

RISK_FACTOR_BY_ISSUE = {
    ISSUE_CARIES: "龋病进展",
    ISSUE_SEVERE_MALOCCLUSION: "严重咬合异常",
    ISSUE_FINGER_HABIT: "持续吮指",
    ISSUE_MOUTH_BREATHING: "口呼吸疑点",
    ISSUE_POOR_HYGIENE: "清洁能力不足",
}

# 检查中可记录的观察指标目录（结构化检查的合法键 + 含义）。
# 值类型仅用于文档与校验，实际值随检查原样保存。
OBSERVATION_INDICATORS = {
    # 龋病
    "caries_new_lesions": "新发龋损（牙位列表）",
    "caries_active_lesions": "活动龋损数量",
    "caries_progression": "龋病进展（worsening/stable/improved，基于与既往比较）",
    # 咬合与颌骨
    "malocclusion_class": "错𬌗分类（如 class_II/class_III/crossbite/openbite/deepbite/crowding）",
    "malocclusion_severity": "严重度（mild/moderate/severe）",
    "jaw_growth_discrepancy": "颌骨发育不调（none/suspected/confirmed）",
    # 习惯
    "finger_habit": "吮指习惯（absent/occasional/persistent_day/persistent_night）",
    "finger_habit_duration_months": "吮指持续月数",
    "mouth_breathing": "口呼吸表现（absent/suspected/confirmed）",
    # 清洁
    "plaque_index": "菌斑指数（0-3）",
    "brushing_independence": "自主刷牙能力（none/partial/independent）",
}

# 影像摘要的合法类型
IMAGE_SUMMARY_TYPES = ["panoramic", "lateral_cephalometric", "intraoral_photos", "bitewing"]

# 严重错𬌗被认为值得早治评估的形态
SEVERE_FORMS = {"crossbite", "openbite", "class_III", "severe_crowding"}

# 干预的硬性证据门槛：
# 即使规则命中"开始干预"，下列证据缺一时只能降级为补充检查/专科会诊，
# 年龄永远不在此列。
INTERVENTION_REQUIRED_EVIDENCE = [
    ("finding", "malocclusion_severity", "重度错𬌗的临床检查记录"),
    ("image", "lateral_cephalometric", "头影测量影像摘要"),
    ("specialist_opinion", None, "正畸专科医生的第二意见"),
]


@dataclass
class RuleFinding:
    """单条规则的评估结果。"""

    issue: str
    risk_factor: str
    advice: str
    reason: str
    missing_evidence: list[str] = field(default_factory=list)
    triggered_rule: str = ""
    severity: str = "info"  # info / watch / action

    def as_dict(self) -> dict:
        return {
            "issue": self.issue,
            "risk_factor": self.risk_factor,
            "advice": self.advice,
            "reason": self.reason,
            "missing_evidence": list(self.missing_evidence),
            "triggered_rule": self.triggered_rule,
            "severity": self.severity,
        }


def _finding_map(findings: list[dict]) -> dict:
    out = {}
    for f in findings or []:
        metric = f.get("metric")
        if metric:
            out[metric] = f.get("value")
    return out


def _has_image(images: list[dict], image_type: str) -> bool:
    return any(i.get("type") == image_type for i in images or [])


def _has_specialist_opinion(second_opinions: list[dict]) -> bool:
    return any(
        o.get("opinion_type") == "orthodontic_specialist"
        for o in second_opinions or []
    )


def evaluate(
    findings: list[dict],
    images: list[dict],
    home_care: list[dict] | None = None,
    second_opinions: list[dict] | None = None,
    stage: str | None = None,
    age_months: int | None = None,  # 仅用于解释与节奏，绝不单独驱动结论
    previous_findings: list[dict] | None = None,
) -> list[RuleFinding]:
    """对一次检查的证据评估各议题。

    只依据传入的证据；证据不足时输出"继续观察/补充检查"并列出缺什么。
    """
    m = _finding_map(findings)
    prev = _finding_map(previous_findings)
    results: list[RuleFinding] = []

    results.append(
        _eval_caries(m, images, home_care)
    )
    results.append(
        _eval_malocclusion(m, images, second_opinions, stage)
    )
    results.append(
        _eval_finger_habit(m, stage)
    )
    results.append(
        _eval_mouth_breathing(m)
    )
    results.append(
        _eval_hygiene(m, home_care)
    )

    # 引擎层的最终防线：任何"开始干预"结论若缺少硬性证据则强制降级。
    guarded = []
    for r in results:
        if r.advice == ADVICE_INTERVENE:
            missing = _missing_intervention_evidence(m, images, second_opinions)
            if missing:
                r.advice = ADVICE_REFERRAL
                r.severity = "action"
                r.reason += (
                    "；虽达到早治评估门槛，但干预证据不完整，"
                    "在证据补齐前只能转专科会诊，不得直接开始矫治"
                )
                r.missing_evidence = sorted(set(r.missing_evidence + missing))
        guarded.append(r)

    return guarded


def _missing_intervention_evidence(m, images, second_opinions) -> list[str]:
    missing = []
    if m.get("malocclusion_severity") != "severe":
        missing.append("重度错𬌗的临床检查记录")
    if not _has_image(images, "lateral_cephalometric"):
        missing.append("头影测量影像摘要")
    if not _has_specialist_opinion(second_opinions):
        missing.append("正畸专科医生的第二意见")
    return missing


def _eval_caries(m, images, home_care) -> RuleFinding:
    active = m.get("caries_active_lesions")
    progression = m.get("caries_progression")
    new_lesions = m.get("caries_new_lesions")
    findings = m  # noqa: F841（保留读起来一致的入口）
    r = RuleFinding(
        issue=ISSUE_CARIES,
        risk_factor=RISK_FACTOR_BY_ISSUE[ISSUE_CARIES],
        advice=ADVICE_OBSERVE,
        reason="本次检查未提供龋病活动度证据，维持常规复查。",
        triggered_rule="caries.baseline",
        severity="info",
    )
    try:
        active_n = int(active) if active is not None else 0
    except (TypeError, ValueError):
        active_n = 0

    care_adherent = _care_adherent(home_care, "fluoride")

    if progression == "worsening" or (new_lesions and active_n >= 2):
        r.triggered_rule = "caries.progressive"
        r.severity = "action"
        if not care_adherent:
            r.advice = ADVICE_SUPPLEMENT
            r.reason = (
                "龋病呈进展趋势（活动龋损 %d 个），且含氟护理执行不到位；"
                "需安排龋风险评估与涂氟等补充处置，并复查家庭护理。" % active_n
            )
            r.missing_evidence.append("家庭含氟护理的连续执行记录")
        else:
            r.advice = ADVICE_REFERRAL
            r.reason = (
                "龋病在规范家庭护理下仍进展（活动龋损 %d 个），"
                "需儿童牙科专科评估充填/镇静治疗方案。" % active_n
            )
    elif active_n >= 1:
        r.triggered_rule = "caries.active"
        r.severity = "watch"
        r.advice = ADVICE_DAILY_CARE
        r.reason = "发现 %d 个活动龋损，优先强化刷牙与含氟护理并安排治疗复查。" % active_n
        if not _has_image(images, "bitewing"):
            r.missing_evidence.append("咬翼片影像摘要（评估邻面龋）")
    return r


def _care_adherent(home_care, topic: str) -> bool:
    for c in home_care or []:
        if c.get("topic") == topic:
            return bool(c.get("adherent"))
    return False


def _eval_malocclusion(m, images, second_opinions, stage) -> RuleFinding:
    form = m.get("malocclusion_class")
    severity = m.get("malocclusion_severity")
    jaw = m.get("jaw_growth_discrepancy")
    r = RuleFinding(
        issue=ISSUE_SEVERE_MALOCCLUSION,
        risk_factor=RISK_FACTOR_BY_ISSUE[ISSUE_SEVERE_MALOCCLUSION],
        advice=ADVICE_OBSERVE,
        reason="暂无显著错𬌗表现的记录，按发育阶段继续观察牙列替换。",
        triggered_rule="malocclusion.baseline",
        severity="info",
    )

    severe_form = form in SEVERE_FORMS
    if severity == "severe" or (severe_form and jaw in ("suspected", "confirmed")):
        r.triggered_rule = "malocclusion.early_treatment_candidate"
        r.severity = "action"
        r.reason = (
            "记录到%s%s，属于需要评估是否早治的错𬌗类型；"
            "是否干预仍须结合颌骨发育趋势与专科判断，不能仅凭年龄决定。"
            % (form or "重度错𬌗", "合并颌骨发育不调疑点" if jaw == "suspected" else "")
        )
        missing = _missing_intervention_evidence(m, images, second_opinions)
        r.missing_evidence = missing
        if not missing:
            r.advice = ADVICE_INTERVENE
            r.reason += "三项硬性证据齐备，可与家长共同决策是否开始矫治。"
        elif not _has_specialist_opinion(second_opinions):
            r.advice = ADVICE_REFERRAL
        else:
            r.advice = ADVICE_SUPPLEMENT
    elif severity == "moderate" or severe_form:
        r.triggered_rule = "malocclusion.monitor"
        r.severity = "watch"
        r.advice = ADVICE_OBSERVE
        r.reason = (
            "%s目前为中度/局限表现，处于%s，缩短复诊间隔观察自行调整与"
            "颌骨发育，暂不矫治。" % (form or "错𬌗", stage or "当前发育阶段")
        )
        if not _has_image(images, "intraoral_photos"):
            r.missing_evidence.append("口内照影像摘要（便于复诊比较形态变化）")
    return r


def _eval_finger_habit(m, stage) -> RuleFinding:
    habit = m.get("finger_habit")
    months = m.get("finger_habit_duration_months")
    r = RuleFinding(
        issue=ISSUE_FINGER_HABIT,
        risk_factor=RISK_FACTOR_BY_ISSUE[ISSUE_FINGER_HABIT],
        advice=ADVICE_OBSERVE,
        reason="无持续性吮指习惯记录。",
        triggered_rule="habit.baseline",
        severity="info",
    )
    persistent = habit in ("persistent_day", "persistent_night")
    try:
        months_n = int(months) if months is not None else 0
    except (TypeError, ValueError):
        months_n = 0

    if persistent and stage in (STAGE_MIXED, STAGE_EARLY_PERMANENT) and months_n >= 6:
        r.triggered_rule = "habit.persistent_mixed_dentition"
        r.severity = "action"
        r.advice = ADVICE_DAILY_CARE
        r.reason = (
            "进入%s后持续吮指已 %d 个月，可能影响前牙排列与腭弓形态；"
            "先用行为引导（提醒、奖励记录），并记录是否伴发开𬌗，"
            "3 个月复诊评价效果，暂不使用矫治装置。" % (stage, months_n)
        )
        if "malocclusion_class" not in m:
            r.missing_evidence.append("咬合形态检查（确认是否已形成开𬌗/深覆盖）")
    elif habit == "occasional" or (persistent and stage == STAGE_PRIMARY):
        r.triggered_rule = "habit.education"
        r.severity = "watch"
        r.advice = ADVICE_DAILY_CARE
        r.reason = "乳牙列期的吮指多数可自行消退，给予家长宣教并持续记录频率。"
    return r


def _eval_mouth_breathing(m) -> RuleFinding:
    mb = m.get("mouth_breathing")
    r = RuleFinding(
        issue=ISSUE_MOUTH_BREATHING,
        risk_factor=RISK_FACTOR_BY_ISSUE[ISSUE_MOUTH_BREATHING],
        advice=ADVICE_OBSERVE,
        reason="无口呼吸相关记录。",
        triggered_rule="mouth_breathing.baseline",
        severity="info",
    )
    if mb == "confirmed":
        r.triggered_rule = "mouth_breathing.confirmed"
        r.severity = "action"
        r.advice = ADVICE_REFERRAL
        r.reason = (
            "已确认存在口呼吸模式，可能影响颌面部发育，需耳鼻喉科评估"
            "气道阻塞（腺样体/扁桃体、过敏性鼻炎）后再讨论咬合管理。"
        )
    elif mb == "suspected":
        r.triggered_rule = "mouth_breathing.suspected"
        r.severity = "watch"
        r.advice = ADVICE_SUPPLEMENT
        r.reason = "家长或医生怀疑睡眠/安静时张口呼吸，证据尚不足以确认。"
        r.missing_evidence.append("连续 2 周的家庭睡眠呼吸观察记录")
        r.missing_evidence.append("耳鼻喉气道评估")
    return r


def _eval_hygiene(m, home_care) -> RuleFinding:
    plaque = m.get("plaque_index")
    independence = m.get("brushing_independence")
    r = RuleFinding(
        issue=ISSUE_POOR_HYGIENE,
        risk_factor=RISK_FACTOR_BY_ISSUE[ISSUE_POOR_HYGIENE],
        advice=ADVICE_OBSERVE,
        reason="无清洁能力相关记录。",
        triggered_rule="hygiene.baseline",
        severity="info",
    )
    try:
        plaque_n = float(plaque) if plaque is not None else 0.0
    except (TypeError, ValueError):
        plaque_n = 0.0
    brushing_done = _care_adherent(home_care, "brushing")

    if plaque_n >= 2 or (independence == "none" and not brushing_done):
        r.triggered_rule = "hygiene.poor"
        r.severity = "watch"
        r.advice = ADVICE_DAILY_CARE
        r.reason = (
            "菌斑指数 %s 且儿童尚不能独立清洁，需家长监督/代刷，"
            "下次复诊复评菌斑与刷牙执行。" % plaque
        )
        if not brushing_done:
            r.missing_evidence.append("每日家长监督刷牙的执行记录")
    return r


def rule_snapshot() -> dict:
    """生成可持久化的规则版本快照（内容冻结后哈希校验用）。"""
    import hashlib
    import json

    payload = {
        "version": RULE_VERSION,
        "advice_types": ADVICE_TYPES,
        "stages": STAGES,
        "indicators": OBSERVATION_INDICATORS,
        "image_types": IMAGE_SUMMARY_TYPES,
        "severe_forms": sorted(SEVERE_FORMS),
        "intervention_required_evidence": [
            {"kind": k, "key": key, "label": label}
            for k, key, label in INTERVENTION_REQUIRED_EVIDENCE
        ],
    }
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    payload["sha256"] = digest
    return payload


def recommend_recall_interval_months(stage: str | None, findings: list[RuleFinding]) -> int:
    """建议复诊间隔（月）。有 watch/action 议题时缩短；仅节奏用途。"""
    severities = {f.severity for f in findings}
    if "action" in severities:
        return 3
    if "watch" in severities:
        return 4 if stage == STAGE_MIXED else 6
    return 6
