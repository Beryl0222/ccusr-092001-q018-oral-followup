"""端到端测试：临床规则安全约束 + HTTP 服务的随访/共同决策/授权语义。"""

import http.client
import json
import threading
import unittest
from datetime import date, timedelta
from http.server import ThreadingHTTPServer

import rules
import service
from service import App, Handler
from store import Store

from service import SERVICE_ID, health


def d(offset_days: int) -> str:
    return (date(2026, 9, 27) + timedelta(days=offset_days)).isoformat()


class HealthTest(unittest.TestCase):
    def test_identity(self):
        self.assertEqual(health(), {"status": "ok", "service": SERVICE_ID})

    def test_domain_vocabulary_matches_rules(self):
        self.assertEqual(service.run_check(), 0)


class RulesTest(unittest.TestCase):
    def test_age_alone_never_triggers_intervention(self):
        # 任意发育阶段、任意年龄，没有临床证据时绝不输出"开始干预"。
        for stage in rules.STAGES:
            for age in (36, 60, 72, 84, 96, 120, 144):
                results = rules.evaluate([], [], stage=stage, age_months=age)
                advices = {r.advice for r in results}
                self.assertNotIn(
                    rules.ADVICE_INTERVENE, advices,
                    f"{stage}/{age}月龄仅凭年龄出现了干预结论",
                )
                for r in results:
                    self.assertTrue(r.reason)

    def test_intervention_requires_three_hard_evidences(self):
        findings = [
            {"metric": "malocclusion_class", "value": "class_III"},
            {"metric": "malocclusion_severity", "value": "severe"},
            {"metric": "jaw_growth_discrepancy", "value": "confirmed"},
        ]
        # 三项硬性证据缺影像与第二意见 → 只能转专科，且明确列出缺什么。
        results = rules.evaluate(findings, [], stage=rules.STAGE_MIXED)
        mal = next(r for r in results if r.issue == rules.ISSUE_SEVERE_MALOCCLUSION)
        self.assertEqual(mal.advice, rules.ADVICE_REFERRAL)
        self.assertIn("头影测量影像摘要", mal.missing_evidence)
        self.assertIn("正畸专科医生的第二意见", mal.missing_evidence)

        # 补了影像但缺第二意见 → 仍然是专科会诊。
        results = rules.evaluate(
            findings, [{"type": "lateral_cephalometric", "summary": "III 类骨面型"}],
            stage=rules.STAGE_MIXED,
        )
        mal = next(r for r in results if r.issue == rules.ISSUE_SEVERE_MALOCCLUSION)
        self.assertEqual(mal.advice, rules.ADVICE_REFERRAL)

        # 三项齐备才允许"开始干预"，且缺失证据为空。
        results = rules.evaluate(
            findings,
            [{"type": "lateral_cephalometric", "summary": "III 类骨面型"}],
            second_opinions=[{"opinion_type": "orthodontic_specialist",
                              "content": "建议早期矫形治疗"}],
            stage=rules.STAGE_MIXED,
        )
        mal = next(r for r in results if r.issue == rules.ISSUE_SEVERE_MALOCCLUSION)
        self.assertEqual(mal.advice, rules.ADVICE_INTERVENE)
        self.assertEqual(mal.missing_evidence, [])

    def test_suspected_mouth_breathing_lists_missing_evidence(self):
        results = rules.evaluate(
            [{"metric": "mouth_breathing", "value": "suspected"}], []
        )
        mb = next(r for r in results if r.issue == rules.ISSUE_MOUTH_BREATHING)
        self.assertEqual(mb.advice, rules.ADVICE_SUPPLEMENT)
        self.assertTrue(mb.missing_evidence)
        self.assertTrue(any("睡眠" in x for x in mb.missing_evidence))

    def test_progressive_caries_despite_care_refers_to_specialist(self):
        results = rules.evaluate(
            [{"metric": "caries_progression", "value": "worsening"},
             {"metric": "caries_active_lesions", "value": 3}],
            [],
            home_care=[{"topic": "fluoride", "adherent": True}],
        )
        caries = next(r for r in results if r.issue == rules.ISSUE_CARIES)
        self.assertEqual(caries.advice, rules.ADVICE_REFERRAL)

    def test_snapshot_is_deterministic_and_versioned(self):
        s1 = rules.rule_snapshot()
        s2 = rules.rule_snapshot()
        self.assertEqual(s1["sha256"], s2["sha256"])
        self.assertEqual(s1["version"], rules.RULE_VERSION)


class ApiSession:
    def __init__(self, host: str, port: int):
        self.host, self.port = host, port

    def request(self, method, path, body=None, user=None):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=5)
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if user:
            headers["X-User-Id"] = user
        payload = (
            json.dumps(body, ensure_ascii=False).encode("utf-8")
            if body is not None else None
        )
        conn.request(method, path, payload, headers)
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8")
        conn.close()
        return resp.status, json.loads(raw) if raw else None


class HttpEndToEndTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Handler.app = App(":memory:")
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.api = ApiSession("127.0.0.1", cls.port)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        Handler.app.store.close()

    def setUp(self):
        self.api.request  # 确保会话可用

    def _new_patient(self, name="童童"):
        status, body = self.api.request(
            "POST", "/patients",
            {"name": name, "birth_date": d(-96)}, user="staff-register",
        )
        self.assertEqual(status, 200, body)
        return body["patient_id"]

    def _grant(self, pid, user, role="clinician", expires=365):
        status, body = self.api.request(
            "POST", f"/patients/{pid}/consents",
            {"user_id": user, "role": role, "granted_by": "front-desk",
             "expires_at": d(expires)},
            user="front-desk",
        )
        self.assertEqual(status, 200, body)
        return body["consent_id"]

    def test_full_continuity_correction_and_traceability_flow(self):
        pid = self._new_patient()
        self._grant(pid, "dr-chen", "clinician")

        # 第一次检查：多个议题并存。
        status, exam1 = self.api.request(
            "POST", f"/patients/{pid}/exams",
            {"stage": "混合牙列期", "age_months": 96, "exam_date": d(-120),
             "notes": "家长主诉牙列不齐",
             "findings": [
                 {"metric": "caries_active_lesions", "value": 1},
                 {"metric": "malocclusion_class", "value": "crowding"},
                 {"metric": "malocclusion_severity", "value": "mild"},
                 {"metric": "finger_habit", "value": "persistent_night"},
                 {"metric": "finger_habit_duration_months", "value": 8},
                 {"metric": "mouth_breathing", "value": "suspected"},
                 {"metric": "plaque_index", "value": 2},
             ],
             "images": [{"type": "intraoral_photos",
                         "summary": "前牙轻度拥挤，未见反𬌗"}],
             "home_care": [{"topic": "brushing", "adherent": False,
                            "detail": "常漏刷下颌后牙"}]},
            user="dr-chen",
        )
        self.assertEqual(status, 200, exam1)
        self.assertEqual(exam1["seq"], 1)

        # 规则输出展示缺失证据，且年龄安全声明随评估返回。
        status, ev = self.api.request(
            "POST", f"/exams/{exam1['exam_id']}/evaluate", {}, user="dr-chen"
        )
        self.assertEqual(status, 200, ev)
        self.assertEqual(ev["rule_version"], rules.RULE_VERSION)
        self.assertIn("年龄", ev["age_safety_notice"])
        by_issue = {r["issue"]: r for r in ev["results"]}
        self.assertEqual(
            by_issue[rules.ISSUE_MOUTH_BREATHING]["advice"], "补充检查"
        )
        self.assertTrue(by_issue[rules.ISSUE_MOUTH_BREATHING]["missing_evidence"])
        self.assertEqual(
            by_issue[rules.ISSUE_FINGER_HABIT]["advice"], "日常护理"
        )
        self.assertEqual(ev["recommended_recall_months"], 3)

        # 发出建议（保存证据快照 + 规则版本）。
        status, issued = self.api.request(
            "POST", f"/exams/{exam1['exam_id']}/advice", {}, user="dr-chen"
        )
        self.assertEqual(status, 200, issued)
        advice_id = issued["advice"]["advice_id"]

        # 第二次复诊：新检查而非覆盖；龋病进展可比较出趋势。
        status, exam2 = self.api.request(
            "POST", f"/patients/{pid}/exams",
            {"stage": "混合牙列期", "age_months": 100, "exam_date": d(-5),
             "findings": [
                 {"metric": "caries_active_lesions", "value": 3},
                 {"metric": "caries_progression", "value": "worsening"},
             ],
             "home_care": [{"topic": "fluoride", "adherent": True,
                            "detail": "每日含氟牙膏"}]},
            user="dr-chen",
        )
        self.assertEqual(status, 200, exam2)
        self.assertEqual(exam2["seq"], 2)

        status, exams = self.api.request(
            "GET", f"/patients/{pid}/exams", user="dr-chen"
        )
        self.assertEqual([e["seq"] for e in exams["exams"]], [1, 2])

        status, trend = self.api.request(
            "GET", f"/patients/{pid}/trends/caries_active_lesions", user="dr-chen"
        )
        self.assertEqual(status, 200, trend)
        self.assertEqual([p["value"] for p in trend["points"]], ["1", "3"])

        # 进展性龋 + 已坚持含氟护理 → 转专科。
        status, ev2 = self.api.request(
            "POST", f"/exams/{exam2['exam_id']}/evaluate", {}, user="dr-chen"
        )
        caries = next(r for r in ev2["results"] if r["issue"] == rules.ISSUE_CARIES)
        self.assertEqual(caries["advice"], "专科会诊")

        # 更正错误记录：菌斑指数 2 应为 1，旧值保留并形成更正链。
        plaque_id = next(
            f["finding_id"] for f in exam1["findings"] if f["metric"] == "plaque_index"
        )
        status, correction = self.api.request(
            "POST", f"/patients/{pid}/items/findings/{plaque_id}/correction",
            {"reason": "录入时看错表格行", "updates": {"value": "1"}},
            user="dr-chen",
        )
        self.assertEqual(status, 200, correction)
        self.assertEqual(correction["old"]["value"], "2")
        self.assertEqual(correction["new"]["value"], "1")

        status, chain = self.api.request(
            "GET", f"/items/findings/{plaque_id}/chain", user="dr-chen"
        )
        self.assertEqual(status, 200, chain)
        self.assertEqual(len(chain["chain"]), 2)
        # 链头是已作废的旧值，带更正说明；链尾是当前生效的新值。
        old_entry, new_entry = chain["chain"]
        self.assertEqual(old_entry["value"], "2")
        self.assertTrue(old_entry["correction"])
        self.assertEqual(old_entry["correction"]["reason"], "录入时看错表格行")
        self.assertEqual(new_entry["value"], "1")
        self.assertIsNone(new_entry["correction"])
        self.assertIsNone(new_entry["superseded_by_id"])

        # 旧建议的证据快照不被事后更正篡改。
        status, trace = self.api.request(
            "GET", f"/advice/{advice_id}/trace", user="dr-chen"
        )
        self.assertEqual(status, 200, trace)
        self.assertEqual(
            trace["rule_version_snapshot"]["version"], rules.RULE_VERSION
        )
        snap_plaque = next(
            f for f in trace["advice"]["evidence_snapshot"]["findings"]
            if f["metric"] == "plaque_index"
        )
        self.assertEqual(snap_plaque["value"], "2")
        self.assertEqual(trace["exam"]["exam_id"], exam1["exam_id"])

        # 相互独立但可关联的计划：龋病计划与吮指计划。
        status, plan_caries = self.api.request(
            "POST", f"/patients/{pid}/plans",
            {"issue": "caries"}, user="dr-chen",
        )
        self.assertEqual(status, 200, plan_caries)
        status, plan_habit = self.api.request(
            "POST", f"/patients/{pid}/plans",
            {"issue": "finger_habit"}, user="dr-chen",
        )
        self.assertEqual(status, 200, plan_habit)
        status, linked = self.api.request(
            "POST", f"/plans/{plan_caries['plan_id']}/link",
            {"linked_plan_id": plan_habit["plan_id"]}, user="dr-chen",
        )
        self.assertEqual(status, 200, linked)
        self.assertEqual(linked["linked_plan_id"], plan_habit["plan_id"])

        # 排复诊并完成。
        status, fu = self.api.request(
            "POST", f"/plans/{plan_habit['plan_id']}/followups",
            {"due_date": d(-10)}, user="dr-chen",
        )
        self.assertEqual(status, 200, fu)
        status, done = self.api.request(
            "POST", f"/followups/{fu['followup_id']}/complete",
            {"exam_id": exam2["exam_id"]}, user="dr-chen",
        )
        self.assertEqual(status, 200, done)
        self.assertEqual(done["status"], "completed")

        # 第二位医生的正畸意见保留下来。
        self._grant(pid, "dr-ortho", "clinician")
        status, opinion = self.api.request(
            "POST", f"/patients/{pid}/opinions",
            {"doctor_id": "dr-ortho", "opinion_type": "orthodontic_specialist",
             "content": "拥挤为轻度，建议观察至尖牙萌出再评估",
             "exam_id": exam1["exam_id"]},
            user="dr-ortho",
        )
        self.assertEqual(status, 200, opinion)

        # 家长选择（共同决策）同样保留；guardian 可以登记但不能写临床记录。
        self._grant(pid, "parent-li", "guardian")
        status, decision = self.api.request(
            "POST", f"/patients/{pid}/decisions",
            {"parent_id": "parent-li", "choice": "先观察，不做矫治",
             "rationale": "希望先戒除夜間吮指并观察替换",
             "advice_id": advice_id, "plan_id": plan_habit["plan_id"]},
            user="parent-li",
        )
        self.assertEqual(status, 200, decision)
        status, blocked = self.api.request(
            "POST", f"/patients/{pid}/exams",
            {"stage": "乳牙列期", "exam_date": d(0)}, user="parent-li",
        )
        self.assertEqual(status, 403)
        self.assertEqual(blocked["error"], "role_not_permitted")

        # 时间线聚合：历史检查、趋势、计划、意见、家长选择、更正链都在。
        status, timeline = self.api.request(
            "GET", f"/patients/{pid}/timeline", user="dr-chen"
        )
        self.assertEqual(status, 200, timeline)
        self.assertEqual(len(timeline["exams"]), 2)
        self.assertEqual(len(timeline["plans"]), 2)
        self.assertEqual(len(timeline["second_opinions"]), 1)
        self.assertEqual(len(timeline["family_decisions"]), 1)
        self.assertEqual(len(timeline["corrections"]), 1)
        self.assertIn("plaque_index", timeline["metric_trends"])

    def test_expired_consent_blocks_access(self):
        pid = self._new_patient("过期患儿")
        self._grant(pid, "dr-temp", "clinician", expires=-1)
        status, body = self.api.request(
            "GET", f"/patients/{pid}", user="dr-temp"
        )
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "consent_expired")
        status, _ = self.api.request(
            "GET", f"/patients/{pid}/timeline", user="dr-temp"
        )
        self.assertEqual(status, 403)

    def test_revoked_and_missing_consent(self):
        pid = self._new_patient("撤销患儿")
        cid = self._grant(pid, "dr-soon", "clinician")
        status, _ = self.api.request(
            "POST", f"/patients/{pid}/consents/{cid}/revoke", {}, user="front-desk"
        )
        self.assertEqual(status, 200)
        status, body = self.api.request(
            "GET", f"/patients/{pid}", user="dr-soon"
        )
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "forbidden")

        # 完全无关人员看不到该患者（逾期清单也不能泄露）。
        status, body = self.api.request(
            "GET", "/followups/overdue", user="dr-soon"
        )
        self.assertEqual(status, 200)
        self.assertNotIn(pid, [f["patient_id"] for f in body["overdue"]])

    def test_authentication_required(self):
        status, body = self.api.request("GET", "/health")
        self.assertEqual(status, 200)
        pid = self._new_patient()
        status, body = self.api.request("GET", f"/patients/{pid}")
        self.assertEqual(status, 401)

    def test_overdue_only_within_caller_authorized_scope(self):
        # 患者 A：dr-a 被授权，有一条逾期随访。
        pa = self._new_patient("患者A")
        self._grant(pa, "dr-a", "clinician")
        status, plan_a = self.api.request(
            "POST", f"/patients/{pa}/plans", {"issue": "caries"}, user="dr-a"
        )
        self.assertEqual(status, 200, plan_a)
        self.api.request(
            "POST", f"/plans/{plan_a['plan_id']}/followups",
            {"due_date": d(-15)}, user="dr-a",
        )

        # 患者 B：只有 dr-b 被授权，也有逾期随访。
        pb = self._new_patient("患者B")
        self._grant(pb, "dr-b", "clinician")
        status, plan_b = self.api.request(
            "POST", f"/patients/{pb}/plans", {"issue": "mouth_breathing"},
            user="dr-b",
        )
        self.assertEqual(status, 200, plan_b)
        self.api.request(
            "POST", f"/plans/{plan_b['plan_id']}/followups",
            {"due_date": d(-20)}, user="dr-b",
        )

        status, seen_a = self.api.request(
            "GET", "/followups/overdue", user="dr-a"
        )
        self.assertEqual(status, 200, seen_a)
        ids_a = {f["patient_id"] for f in seen_a["overdue"]}
        self.assertIn(pa, ids_a)
        self.assertNotIn(pb, ids_a)  # 不得泄露给无关人员

        status, seen_b = self.api.request(
            "GET", "/followups/overdue", user="dr-b"
        )
        ids_b = {f["patient_id"] for f in seen_b["overdue"]}
        self.assertIn(pb, ids_b)
        self.assertNotIn(pa, ids_b)


class StoreUnitTest(unittest.TestCase):
    def test_overdue_excludes_completed_and_future(self):
        store = Store(":memory:")
        p = store.create_patient("甲", "2018-01-01")
        plan = store.create_plan(p["patient_id"], "caries", "龋病进展", "dr-x")
        f1 = store.schedule_followup(plan["plan_id"], p["patient_id"], d(-30))
        store.schedule_followup(plan["plan_id"], p["patient_id"], d(30))
        exam = store.create_exam(p["patient_id"], "混合牙列期", 100, d(-1), "dr-x")
        store.complete_followup(f1["followup_id"], exam["exam_id"])
        overdue = store.overdue_followups([p["patient_id"]], as_of=d(0))
        self.assertEqual(overdue, [])  # 逾期项已完成；未来项不算逾期
        store.close()


if __name__ == "__main__":
    unittest.main()
