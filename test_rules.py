"""规则引擎单测：年龄阻断、各风险路径、专科闸门与版本指纹。"""

import copy
import unittest

import rules
from rules import RULE_VERSION, evaluate, rulebook_checksum


def exam_bundle(stage="混合牙列期", metrics=None, imaging=None, opinions=None, trends=None):
    bundle = {
        "stage": stage,
        "exam": {
            "record_id": "e1",
            "visit_id": "v1",
            "visit_date": "2026-01-10",
            "metrics": metrics or {},
        },
        "imaging": imaging or [],
        "home_care": None,
        "second_opinions": opinions or [],
        "metric_trends": trends or {},
    }
    return bundle


def by_risk(result):
    return {r["risk_factor"]: r for r in result["results"]}


NORMAL_OCCLUSION = {
    "前牙覆盖": 2.0,
    "反𬌗范围": "无",
    "牙列拥挤": "无",
    "吮指频率": "偶尔",
    "口呼吸体征": "无",
    "鼻通气受阻": "无",
    "dmft": 0,
    "活动龋牙面数": 0,
    "菌斑指数": 0.5,
    "刷牙独立性": "需监督协助",
}

ORTHO_IMAGING = [
    {"record_id": "i1", "image_type": "全景片", "visit_date": "2026-01-10"},
    {"record_id": "i2", "image_type": "头颅侧位片", "visit_date": "2026-01-10"},
]


class AgeOnlyTest(unittest.TestCase):
    def test_no_exam_blocks_treatment_conclusion(self):
        result = evaluate({"stage": "混合牙列期"})
        self.assertTrue(result["age_only_evidence"])
        self.assertFalse(result["treatment_conclusion_allowed"])
        for r in result["results"]:
            self.assertNotEqual(r["recommendation_type"], "开始干预")
        occ = by_risk(result)["严重咬合异常"]
        self.assertEqual(occ["recommendation_type"], "补充检查")
        self.assertTrue(occ["missing_evidence"])
        self.assertIn("禁止", occ["gate_note"])

    def test_age_alone_never_triggers_problem(self):
        # 没有检查不等于"发现风险"：triggered 必须为 False
        result = evaluate({"stage": "乳牙列期"})
        self.assertTrue(all(not r["triggered"] for r in result["results"]))


class OcclusionTest(unittest.TestCase):
    def test_normal_occlusion_observation(self):
        result = evaluate(exam_bundle(metrics=NORMAL_OCCLUSION))
        occ = by_risk(result)["严重咬合异常"]
        self.assertFalse(occ["triggered"])
        self.assertEqual(occ["recommendation_type"], "继续观察")
        self.assertTrue(result["treatment_conclusion_allowed"])

    def test_severe_without_evidence_first_asks_more_tests(self):
        m = copy.deepcopy(NORMAL_OCCLUSION)
        m.update({"前牙覆盖": 8.0})
        result = evaluate(exam_bundle(metrics=m))  # 无影像、无二诊
        occ = by_risk(result)["严重咬合异常"]
        self.assertEqual(occ["recommendation_type"], "补充检查")
        self.assertTrue(any("全景片" in x for x in occ["missing_evidence"]))
        self.assertFalse(result["treatment_conclusion_allowed"])

    def test_severe_with_imaging_but_no_specialist_refers(self):
        m = copy.deepcopy(NORMAL_OCCLUSION)
        m.update({"前牙覆盖": 8.0})
        result = evaluate(exam_bundle(metrics=m, imaging=ORTHO_IMAGING))
        occ = by_risk(result)["严重咬合异常"]
        self.assertEqual(occ["recommendation_type"], "专科会诊")
        self.assertTrue(occ["gate_blocked"])
        self.assertIn("正畸专科", occ["missing_evidence"][0])
        self.assertFalse(result["treatment_conclusion_allowed"])

    def test_specialist_non_intervention_does_not_open_gate(self):
        m = copy.deepcopy(NORMAL_OCCLUSION)
        m.update({"前牙覆盖": 8.0})
        opinion = [
            {
                "record_id": "o1",
                "specialty": "正畸专科",
                "recommendation": "继续观察",
                "visit_date": "2026-01-10",
            }
        ]
        result = evaluate(exam_bundle(metrics=m, imaging=ORTHO_IMAGING, opinions=opinion))
        occ = by_risk(result)["严重咬合异常"]
        self.assertEqual(occ["recommendation_type"], "专科会诊")
        self.assertTrue(occ["gate_blocked"])

    def test_specialist_intervention_opens_gate(self):
        m = copy.deepcopy(NORMAL_OCCLUSION)
        m.update({"前牙覆盖": 8.0})
        opinion = [
            {
                "record_id": "o1",
                "specialty": "正畸专科",
                "recommendation": "开始干预",
                "visit_date": "2026-01-10",
            }
        ]
        result = evaluate(exam_bundle(metrics=m, imaging=ORTHO_IMAGING, opinions=opinion))
        occ = by_risk(result)["严重咬合异常"]
        self.assertEqual(occ["recommendation_type"], "开始干预")
        self.assertFalse(occ["gate_blocked"])
        self.assertTrue(result["treatment_conclusion_allowed"])
        # 证据链引用了检查、两种影像和二诊意见
        kinds = {e["record_type"] for e in occ["evidence"]}
        self.assertEqual(kinds, {"检查记录", "影像摘要", "二诊意见"})

    def test_single_tooth_crossbite_early_referral(self):
        m = copy.deepcopy(NORMAL_OCCLUSION)
        m.update({"反𬌗范围": "个别前牙"})
        result = evaluate(exam_bundle(metrics=m, imaging=ORTHO_IMAGING))
        occ = by_risk(result)["严重咬合异常"]
        self.assertEqual(occ["recommendation_type"], "专科会诊")

    def test_incomplete_metrics_is_not_a_positive_finding(self):
        m = {"前牙覆盖": 8.0}  # 只有覆盖，缺反𬌗/拥挤
        result = evaluate(exam_bundle(metrics=m))
        occ = by_risk(result)["严重咬合异常"]
        self.assertEqual(occ["recommendation_type"], "补充检查")
        self.assertFalse(occ["triggered"])


class HabitAndAirwayTest(unittest.TestCase):
    def test_primary_dentition_sucking_gets_home_guidance(self):
        m = copy.deepcopy(NORMAL_OCCLUSION)
        m.update({"吮指频率": "每日多次"})
        result = evaluate(exam_bundle(stage="乳牙列期", metrics=m))
        r = by_risk(result)["持续吮指"]
        self.assertTrue(r["triggered"])
        self.assertEqual(r["recommendation_type"], "日常护理")

    def test_persistent_sleep_sucking_in_mixed_needs_ortho(self):
        m = copy.deepcopy(NORMAL_OCCLUSION)
        m.update({"吮指频率": "睡眠中持续", "吮指持续月数": 18})
        result = evaluate(exam_bundle(metrics=m))
        r = by_risk(result)["持续吮指"]
        self.assertEqual(r["recommendation_type"], "专科会诊")
        self.assertTrue(r["gate_blocked"])

    def test_daytime_mouth_breathing_goes_to_ent_not_ortho(self):
        m = copy.deepcopy(NORMAL_OCCLUSION)
        m.update({"口呼吸体征": "白天持续", "鼻通气受阻": "双侧"})
        result = evaluate(exam_bundle(metrics=m))
        r = by_risk(result)["口呼吸疑点"]
        self.assertEqual(r["recommendation_type"], "专科会诊")
        self.assertIn("耳鼻喉科", r["rationale"])
        # 口呼吸规则本身永远不会直接产出正畸干预
        self.assertNotEqual(r["recommendation_type"], "开始干预")

    def test_night_suspicion_needs_more_observation(self):
        m = copy.deepcopy(NORMAL_OCCLUSION)
        m.update({"口呼吸体征": "夜间可疑", "鼻通气受阻": "无"})
        result = evaluate(exam_bundle(metrics=m))
        self.assertEqual(
            by_risk(result)["口呼吸疑点"]["recommendation_type"], "补充检查"
        )


class CariesTest(unittest.TestCase):
    def test_multiple_active_caries_refers_pediatric_dentistry(self):
        m = copy.deepcopy(NORMAL_OCCLUSION)
        m.update({"活动龋牙面数": 4, "dmft": 3})
        result = evaluate(exam_bundle(metrics=m))
        r = by_risk(result)["龋病进展"]
        self.assertEqual(r["recommendation_type"], "专科会诊")
        self.assertIn("儿童牙科", r["rationale"])

    def test_worsening_trend_escalates_even_with_low_count(self):
        m = copy.deepcopy(NORMAL_OCCLUSION)
        m.update({"活动龋牙面数": 1})
        result = evaluate(
            exam_bundle(metrics=m, trends={"活动龋牙面数": "加重"})
        )
        self.assertEqual(
            by_risk(result)["龋病进展"]["recommendation_type"], "专科会诊"
        )

    def test_one_active_surface_asks_more_exam(self):
        m = copy.deepcopy(NORMAL_OCCLUSION)
        m.update({"活动龋牙面数": 1})
        result = evaluate(exam_bundle(metrics=m))
        self.assertEqual(
            by_risk(result)["龋病进展"]["recommendation_type"], "补充检查"
        )


class RuleVersionTest(unittest.TestCase):
    def test_version_and_checksum_stable(self):
        self.assertEqual(RULE_VERSION, "1.0.0")
        self.assertEqual(len(rulebook_checksum()), 16)
        self.assertEqual(rulebook_checksum(), rulebook_checksum())
        result = evaluate(exam_bundle(metrics=NORMAL_OCCLUSION))
        self.assertEqual(result["rule_version"], RULE_VERSION)
        self.assertEqual(result["rule_checksum"], rulebook_checksum())

    def test_checksum_changes_when_thresholds_change(self):
        before = rulebook_checksum()
        rules.RULEBOOK["严重咬合异常"]["覆盖重度_mm"] = 9
        try:
            self.assertNotEqual(before, rulebook_checksum())
        finally:
            rules.RULEBOOK["严重咬合异常"]["覆盖重度_mm"] = 6


if __name__ == "__main__":
    unittest.main()
