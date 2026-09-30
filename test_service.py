"""端到端测试：HTTP 接口、授权、趋势、更正链、溯源与逾期最小可见。"""

import json
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

from app import make_handler
from app import load_domain
from storage import Storage


class Client:
    def __init__(self, base, user_id=None):
        self.base = base
        self.user_id = user_id

    def _call(self, method, path, body=None):
        # 对路径中的非 ASCII 字符（如中文指标名）做百分号编码
        if "?" in path:
            base_path, query = path.split("?", 1)
            url = self.base + urllib.parse.quote(base_path) + "?" + urllib.parse.quote(query, safe="=&,")
        else:
            url = self.base + urllib.parse.quote(path)
        data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if self.user_id:
            req.add_header("X-User-Id", self.user_id)
        try:
            with urllib.request.urlopen(req) as resp:
                payload = resp.read()
                return resp.status, json.loads(payload) if payload else {}
        except urllib.error.HTTPError as e:
            payload = e.read()
            try:
                return e.code, json.loads(payload)
            except json.JSONDecodeError:
                return e.code, {"raw": payload.decode()}

    def get(self, path):
        return self._call("GET", path)

    def post(self, path, body=None):
        return self._call("POST", path, body or {})

    def delete(self, path):
        return self._call("DELETE", path)


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = Storage(":memory:")
        cls.db.initialize()
        cls.domain = load_domain()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(__import__("app").Api(cls.db, cls.domain)))
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        self.anon = Client(self.base)

    def make_doctor(self, name="张医生", role="儿童口腔医生"):
        status, user = self.anon.post("/users", {"name": name, "role": role})
        self.assertEqual(status, 201, user)
        return Client(self.base, user["id"]), user

    def make_patient(self, doctor, name="患儿甲", birth="2017-03-01"):
        status, patient = doctor.post(
            "/patients", {"name": name, "birth_date": birth}
        )
        self.assertEqual(status, 201, patient)
        return patient

    def new_visit(self, doctor, pid, date_, stage, note=None):
        status, visit = doctor.post(
            f"/patients/{pid}/visits",
            {"visit_date": date_, "stage": stage, "note": note},
        )
        self.assertEqual(status, 201, visit)
        return visit

    def add_exam(self, doctor, vid, metrics, note=None):
        status, exam = doctor.post(
            f"/visits/{vid}/exams", {"metrics": metrics, "note": note}
        )
        self.assertEqual(status, 201, exam)
        return exam


class ContinuousFollowupTest(ServerTest):
    def test_full_longitudinal_workflow(self):
        doctor, _ = self.make_doctor()
        patient = self.make_patient(doctor)
        pid = patient["id"]

        # 第一次就诊：中度拥挤、少量活动龋
        v1 = self.new_visit(doctor, pid, "2026-01-10", "混合牙列期")
        e1 = self.add_exam(
            doctor,
            v1["id"],
            {
                "前牙覆盖": 4.0,
                "反𬌗范围": "无",
                "牙列拥挤": "中度",
                "吮指频率": "偶尔",
                "口呼吸体征": "无",
                "鼻通气受阻": "无",
                "dmft": 1,
                "活动龋牙面数": 1,
                "菌斑指数": 1,
                "刷牙独立性": "需监督协助",
            },
        )
        status, _ = doctor.post(
            f"/visits/{v1['id']}/home-care",
            {"items": {"每日刷牙两次": {"执行": "多数天数"}, "含氟牙膏": {"执行": "每日"}}},
        )
        self.assertEqual(status, 201)

        # 评估预览：无检查的风险不应凭空触发
        status, preview = doctor.post(f"/visits/{v1['id']}/evaluate", {})
        self.assertEqual(status, 200)
        self.assertTrue(preview["treatment_conclusion_allowed"])
        self.assertIn("1.0.0", preview["rule_version"])

        # 落库：咬合中度→观察（触发）、龋→补充检查（触发），形成两个独立计划
        status, persisted = doctor.post(f"/visits/{v1['id']}/evaluate?persist=true", {})
        self.assertEqual(status, 200, persisted)
        risks = {p["risk_factor"] for p in persisted["persisted"]}
        self.assertIn("严重咬合异常", risks)
        self.assertIn("龋病进展", risks)
        # 未见风险不建问题
        self.assertNotIn("口呼吸疑点", risks)
        self.assertNotIn("持续吮指", risks)
        # 两个计划 id 不同（相互独立）
        plans = {p["plan_id"] for p in persisted["persisted"]}
        self.assertEqual(len(plans), len(risks))

        # 第二次就诊：覆盖加重、龋进展
        v2 = self.new_visit(doctor, pid, "2026-05-10", "混合牙列期", note="家长反映门牙更凸")
        self.add_exam(
            doctor,
            v2["id"],
            {
                "前牙覆盖": 7.0,
                "反𬌗范围": "无",
                "牙列拥挤": "中度",
                "吮指频率": "偶尔",
                "口呼吸体征": "无",
                "鼻通气受阻": "无",
                "dmft": 3,
                "活动龋牙面数": 4,
                "菌斑指数": 2,
                "刷牙独立性": "需监督协助",
            },
        )
        # 趋势是比较出来的：旧记录仍在
        status, trends = doctor.get(f"/patients/{pid}/trends?metrics=前牙覆盖,活动龋牙面数")
        self.assertEqual(status, 200)
        self.assertEqual(trends["trends"]["前牙覆盖"]["trend"], "加重")
        self.assertEqual(trends["trends"]["活动龋牙面数"]["trend"], "加重")
        self.assertEqual(len(trends["trends"]["前牙覆盖"]["points"]), 2)

        # 第二位医生意见与家长选择都保留
        status, opinion = doctor.post(
            f"/visits/{v2['id']}/second-opinions",
            {
                "doctor_name": "李正畸",
                "specialty": "正畸专科",
                "recommendation": "专科会诊",
                "opinion": "覆盖增大但尚处混合牙列期，建议观察替牙后再评估",
                "risk_factors": ["严重咬合异常"],
            },
        )
        self.assertEqual(status, 201, opinion)
        status, decision = doctor.post(
            f"/visits/{v2['id']}/parent-decisions",
            {"choice": "选择继续观察", "rationale": "家长希望先观察替牙"},
        )
        self.assertEqual(status, 201, decision)

        status, timeline = doctor.get(f"/patients/{pid}/timeline")
        self.assertEqual(status, 200)
        self.assertEqual(len(timeline["timeline"]), 2)
        second_visit = timeline["timeline"][1]
        self.assertEqual(second_visit["second_opinions"][0]["doctor_name"], "李正畸")
        self.assertEqual(second_visit["parent_decisions"][0]["choice"], "选择继续观察")

        # 严重咬合 + 龋病问题应已自动建立"可能相关"之外保持独立；
        # 这里两者并不相关，链接为空
        status, problems = doctor.get(f"/patients/{pid}/problems")
        self.assertEqual(status, 200)
        self.assertEqual(len(problems["links"]), 0)

    def test_independent_but_linkable_plans_for_habits_and_occlusion(self):
        doctor, _ = self.make_doctor("王医生")
        patient = self.make_patient(doctor, "患儿乙")
        pid = patient["id"]
        v = self.new_visit(doctor, pid, "2026-02-01", "混合牙列期")
        self.add_exam(
            doctor,
            v["id"],
            {
                "前牙覆盖": 4.0,
                "反𬌗范围": "无",
                "牙列拥挤": "轻度",
                "吮指频率": "睡眠中持续",
                "吮指持续月数": 18,
                "口呼吸体征": "无",
                "鼻通气受阻": "无",
                "dmft": 0,
                "活动龋牙面数": 0,
                "菌斑指数": 0,
                "刷牙独立性": "基本独立",
            },
        )
        status, persisted = doctor.post(f"/visits/{v['id']}/evaluate?persist=true", {})
        self.assertEqual(status, 200, persisted)
        by = {p["risk_factor"]: p for p in persisted["persisted"]}
        self.assertIn("持续吮指", by)
        self.assertIn("严重咬合异常", by)
        self.assertNotEqual(by["持续吮指"]["plan_id"], by["严重咬合异常"]["plan_id"])

        status, problems = doctor.get(f"/patients/{pid}/problems")
        self.assertEqual(status, 200)
        # 自动登记的"可能相关"链接（吮指 ↔ 咬合）
        self.assertEqual(len(problems["links"]), 2)  # 双向各一条
        relations = {lk["relation"] for lk in problems["links"]}
        self.assertEqual(relations, {"可能相关"})


class CorrectionChainTest(ServerTest):
    def test_correction_keeps_chain_and_trace_flags_evidence(self):
        doctor, _ = self.make_doctor("赵医生")
        patient = self.make_patient(doctor, "患儿丙")
        pid = patient["id"]
        v = self.new_visit(doctor, pid, "2026-03-01", "恒牙列早期")
        exam = self.add_exam(
            doctor,
            v["id"],
            {
                "前牙覆盖": 2.0,
                "反𬌗范围": "无",
                "牙列拥挤": "无",
                "吮指频率": "已戒除",
                "口呼吸体征": "无",
                "鼻通气受阻": "无",
                "dmft": 0,
                "活动龋牙面数": 0,
                "菌斑指数": 0,
                "刷牙独立性": "基本独立",
            },
        )
        # 先制造一个需要转龋病专科的建议
        exam2 = self.add_exam(
            doctor,
            v["id"],
            {"dmft": 4, "活动龋牙面数": 4},
            note="补录龋病检查",
        )
        status, persisted = doctor.post(
            f"/visits/{v['id']}/evaluate?persist=true", {"risk_factors": ["龋病进展"]}
        )
        self.assertEqual(status, 200, persisted)
        advice_id = persisted["persisted"][0]["advice_id"]

        # 更正龋读数：旧记录不删除，新版本指向旧版本
        status, new_exam = doctor.post(
            f"/records/exam/{exam2['id']}/correct",
            {
                "reason": "读数修正",
                "fields": {"metrics": {"活动龋牙面数": 0, "dmft": 0}},
            },
        )
        self.assertEqual(status, 201, new_exam)
        self.assertTrue(new_exam["is_current"])
        self.assertEqual(new_exam["supersedes_id"], exam2["id"])

        status, chain = doctor.get(f"/records/exam/{new_exam['id']}/chain")
        self.assertEqual(status, 200)
        ids = [c["id"] for c in chain["chain"]]
        self.assertEqual(ids, [exam2["id"], new_exam["id"]])
        self.assertFalse(chain["chain"][0]["is_current"])
        self.assertEqual(chain["chain"][0]["correction_reason"], "读数修正")
        # 更正必须留痕：修订人被记录
        self.assertIsNotNone(chain["chain"][0]["corrected_by"])

        # 不能再修订历史版本
        status, err = doctor.post(
            f"/records/exam/{exam2['id']}/correct",
            {"reason": "录入错误", "fields": {"metrics": {"dmft": 9}}},
        )
        self.assertEqual(status, 409)

        # 从建议溯源：引用的检查已标记为"已被更正"并指向新版本
        status, trace = doctor.get(f"/advice/{advice_id}/trace")
        self.assertEqual(status, 200, trace)
        self.assertEqual(trace["rule"]["version"], "1.0.0")
        self.assertEqual(len(trace["rule"]["checksum"]), 16)
        exam_refs = [e for e in trace["evidence"] if e["record_type"] == "检查记录"]
        self.assertTrue(exam_refs)
        corrected = [e for e in trace["evidence"] if e.get("current_state") == "已被更正"]
        self.assertTrue(corrected)
        self.assertEqual(corrected[0]["replacement_record_id"], new_exam["id"])


class ConsentTest(ServerTest):
    def _setup_patient_with_risks(self):
        doctor, user = self.make_doctor("主治医生")
        patient = self.make_patient(doctor, "患儿丁")
        pid = patient["id"]
        v = self.new_visit(doctor, pid, "2026-01-01", "混合牙列期")
        self.add_exam(
            doctor,
            v["id"],
            {
                "前牙覆盖": 8.0,
                "反𬌗范围": "无",
                "牙列拥挤": "重度",
                "吮指频率": "已戒除",
                "口呼吸体征": "无",
                "鼻通气受阻": "无",
                "dmft": 4,
                "活动龋牙面数": 4,
                "菌斑指数": 2,
                "刷牙独立性": "家长全程刷",
            },
        )
        status, img = doctor.post(
            f"/visits/{v['id']}/imaging",
            {"image_type": "全景片", "summary": "未见明显异常"},
        )
        self.assertEqual(status, 201, img)
        doctor.post(f"/visits/{v['id']}/evaluate?persist=true", {})
        return doctor, user, pid

    def test_scoped_visibility(self):
        doctor, owner, pid = self._setup_patient_with_risks()
        _, caries_doc = self.make_doctor("龋病医生", "儿童牙科")
        status, granted = doctor.post(
            f"/patients/{pid}/consents",
            {
                "grantee_id": caries_doc["id"],
                "scopes": ["龋病管理"],
                "expires_at": "9999-12-31T23:59:59+00:00",
                "granted_by": owner["id"],
            },
        )
        self.assertEqual(status, 201, granted)
        cdoc = Client(self.base, caries_doc["id"])

        # 龋病建议可见（龋病管理范围同时可见清洁能力不足）
        status, advice = cdoc.get(f"/patients/{pid}/advice")
        self.assertEqual(status, 200)
        self.assertTrue(advice["advice"])
        self.assertTrue(
            {a["risk_factor"] for a in advice["advice"]}
            <= {"龋病进展", "清洁能力不足"}
        )
        self.assertIn("龋病进展", {a["risk_factor"] for a in advice["advice"]})

        # 时间线里正畸指标被过滤，龋指标保留
        status, timeline = cdoc.get(f"/patients/{pid}/timeline")
        self.assertEqual(status, 200)
        metrics = timeline["timeline"][0]["exams"][0]["metrics"]
        self.assertIn("活动龋牙面数", metrics)
        self.assertNotIn("前牙覆盖", metrics)
        # 无影像查阅范围，影像条目不返回
        self.assertEqual(timeline["timeline"][0]["imaging"], [])

        # 无随访写权限
        status, err = cdoc.post(
            f"/patients/{pid}/visits",
            {"visit_date": "2026-06-01", "stage": "混合牙列期"},
        )
        self.assertEqual(status, 403)

        # 正畸建议的溯源对不可见者返回 404（不暴露存在性）
        status, all_advice = doctor.get(f"/patients/{pid}/advice")
        ortho = next(a for a in all_advice["advice"] if a["risk_factor"] == "严重咬合异常")
        status, err = cdoc.get(f"/advice/{ortho['id']}/trace")
        self.assertEqual(status, 404)

    def test_expired_consent_blocks_all_access(self):
        doctor, owner, pid = self._setup_patient_with_risks()
        _, temp_doc = self.make_doctor("短期医生")
        # 授权已于 2026-06-01 到期（当前日期 2026-09-30）
        status, _ = doctor.post(
            f"/patients/{pid}/consents",
            {
                "grantee_id": temp_doc["id"],
                "scopes": ["儿童口腔随访", "影像查阅"],
                "expires_at": "2026-06-01T00:00:00+00:00",
                "granted_by": owner["id"],
            },
        )
        self.assertEqual(status, 201)
        temp = Client(self.base, temp_doc["id"])
        status, err = temp.get(f"/patients/{pid}/timeline")
        self.assertEqual(status, 403)
        self.assertIn("授权", err["error"])

    def test_revocation(self):
        doctor, owner, pid = self._setup_patient_with_risks()
        _, other = self.make_doctor("被吊销医生")
        status, granted = doctor.post(
            f"/patients/{pid}/consents",
            {
                "grantee_id": other["id"],
                "scopes": ["儿童口腔随访"],
                "expires_at": "9999-12-31T23:59:59+00:00",
                "granted_by": owner["id"],
            },
        )
        consent_id = granted["granted"][0]["id"]
        other_client = Client(self.base, other["id"])
        self.assertEqual(other_client.get(f"/patients/{pid}/timeline")[0], 200)
        status, _ = doctor.delete(f"/consents/{consent_id}")
        self.assertEqual(status, 200)
        self.assertEqual(other_client.get(f"/patients/{pid}/timeline")[0], 403)

    def test_no_consent_at_all(self):
        doctor, _, pid = self._setup_patient_with_risks()
        _, outsider = self.make_doctor("无关医生")
        client = Client(self.base, outsider["id"])
        self.assertEqual(client.get(f"/patients/{pid}/timeline")[0], 403)
        self.assertEqual(client.get(f"/patients/{pid}/follow-ups")[0], 403)


class OverdueTest(ServerTest):
    def test_overdue_list_minimal_and_scoped(self):
        doctor, owner, pid = self._patient()
        _, outsider = self.make_doctor("无关逾期医生")
        outsider_client = Client(self.base, outsider["id"])

        # 主治医生看到逾期项，字段最小化（无姓名、无病情细节）
        status, overdue = doctor.get("/follow-ups/overdue?today=2026-05-01")
        self.assertEqual(status, 200)
        mine = [o for o in overdue["overdue"] if o["patient_id"] == pid]
        self.assertEqual(len(mine), 1)
        item = mine[0]
        self.assertEqual(
            set(item.keys()),
            {"follow_up_id", "patient_id", "due_date", "risk_factor", "overdue_days"},
        )
        self.assertGreater(item["overdue_days"], 0)

        # 无授权的医生完全看不到
        status, theirs = outsider_client.get("/follow-ups/overdue?today=2026-05-01")
        self.assertEqual(status, 200)
        self.assertFalse(any(o["patient_id"] == pid for o in theirs["overdue"]))

        # 完成复诊后不再逾期
        v2 = self.new_visit(doctor, pid, "2026-05-02", "混合牙列期")
        status, _ = doctor.post(
            f"/follow-ups/{item['follow_up_id']}/complete", {"visit_id": v2["id"]}
        )
        self.assertEqual(status, 200)
        status, overdue2 = doctor.get("/follow-ups/overdue?today=2026-05-03")
        self.assertFalse(any(o["patient_id"] == pid for o in overdue2["overdue"]))

    def _patient(self):
        doctor, user = self.make_doctor("随访主治")
        patient = self.make_patient(doctor, "患儿戊")
        pid = patient["id"]
        v = self.new_visit(doctor, pid, "2026-01-01", "混合牙列期")
        self.add_exam(
            doctor,
            v["id"],
            {
                "前牙覆盖": 2.0,
                "反𬌗范围": "无",
                "牙列拥挤": "无",
                "吮指频率": "已戒除",
                "口呼吸体征": "无",
                "鼻通气受阻": "无",
                "dmft": 0,
                "活动龋牙面数": 4,
                "菌斑指数": 0,
                "刷牙独立性": "基本独立",
            },
        )
        doctor.post(f"/visits/{v['id']}/evaluate?persist=true", {})
        return doctor, user, pid


class GuardrailTest(ServerTest):
    def test_age_only_persist_is_rejected(self):
        doctor, _ = self.make_doctor("建档医生")
        patient = self.make_patient(doctor, "患儿己")
        pid = patient["id"]
        v = self.new_visit(doctor, pid, "2026-01-01", "混合牙列期")
        # 没有任何检查就要求落库评估 → 拒绝
        status, err = doctor.post(f"/visits/{v['id']}/evaluate?persist=true", {})
        self.assertEqual(status, 400)
        self.assertIn("年龄", err["error"])
        # 但预览允许，且输出全部是补充检查/观察
        status, preview = doctor.post(f"/visits/{v['id']}/evaluate", {})
        self.assertEqual(status, 200)
        self.assertTrue(preview["age_only_evidence"])

    def test_invalid_metric_rejected(self):
        doctor, _ = self.make_doctor("词表医生")
        patient = self.make_patient(doctor, "患儿庚")
        pid = patient["id"]
        v = self.new_visit(doctor, pid, "2026-01-01", "乳牙列期")
        status, err = doctor.post(
            f"/visits/{v['id']}/exams", {"metrics": {"前牙覆盖": "很大"}}
        )
        self.assertEqual(status, 400)
        status, err = doctor.post(
            f"/visits/{v['id']}/exams", {"metrics": {"不存在的指标": 1}}
        )
        self.assertEqual(status, 400)

    def test_full_orthodontic_gate_path(self):
        """重度咬合：无影像→补检查；补齐影像→专科会诊；专科同意→开始干预。"""
        doctor, _ = self.make_doctor("闸门医生")
        patient = self.make_patient(doctor, "患儿辛")
        pid = patient["id"]
        v = self.new_visit(doctor, pid, "2026-01-01", "混合牙列期")
        self.add_exam(
            doctor,
            v["id"],
            {
                "前牙覆盖": 8.0,
                "反𬌗范围": "无",
                "牙列拥挤": "重度",
                "吮指频率": "已戒除",
                "口呼吸体征": "无",
                "鼻通气受阻": "无",
                "dmft": 0,
                "活动龋牙面数": 0,
                "菌斑指数": 0,
                "刷牙独立性": "基本独立",
            },
        )

        def occ_advice(persist):
            q = "?persist=true" if persist else ""
            status, r = doctor.post(f"/visits/{v['id']}/evaluate{q}", {})
            self.assertEqual(status, 200, r)
            return next(x for x in r["results"] if x["risk_factor"] == "严重咬合异常"), r

        occ, _ = occ_advice(False)
        self.assertEqual(occ["recommendation_type"], "补充检查")

        for t in ("全景片", "头颅侧位片"):
            status, _ = doctor.post(
                f"/visits/{v['id']}/imaging", {"image_type": t, "summary": "供评估"}
            )
            self.assertEqual(status, 201)
        occ, _ = occ_advice(False)
        self.assertEqual(occ["recommendation_type"], "专科会诊")
        self.assertTrue(occ["gate_blocked"])

        status, _ = doctor.post(
            f"/visits/{v['id']}/second-opinions",
            {
                "doctor_name": "正畸专家",
                "specialty": "正畸专科",
                "recommendation": "开始干预",
                "opinion": "指征明确，同意开始一期矫治",
                "risk_factors": ["严重咬合异常"],
            },
        )
        self.assertEqual(status, 201)
        occ, eval_ = occ_advice(True)
        self.assertEqual(occ["recommendation_type"], "开始干预")
        self.assertTrue(eval_["treatment_conclusion_allowed"])

    def test_repeated_evaluation_keeps_one_pending_followup(self):
        doctor, _ = self.make_doctor("重复评估医生")
        patient = self.make_patient(doctor, "患儿壬")
        pid = patient["id"]
        v = self.new_visit(doctor, pid, "2026-01-01", "混合牙列期")
        self.add_exam(
            doctor,
            v["id"],
            {
                "前牙覆盖": 4.0,
                "反𬌗范围": "无",
                "牙列拥挤": "中度",
                "吮指频率": "已戒除",
                "口呼吸体征": "无",
                "鼻通气受阻": "无",
                "dmft": 0,
                "活动龋牙面数": 0,
                "菌斑指数": 0,
                "刷牙独立性": "基本独立",
            },
        )
        s, r1 = doctor.post(f"/visits/{v['id']}/evaluate?persist=true", {})
        self.assertEqual(s, 200, r1)
        s, r2 = doctor.post(f"/visits/{v['id']}/evaluate?persist=true", {})
        self.assertEqual(s, 200, r2)
        # 两次评估都保留了建议（只追加）
        status, advice = doctor.get(f"/patients/{pid}/advice")
        self.assertEqual(status, 200)
        occ_advice = [a for a in advice["advice"] if a["risk_factor"] == "严重咬合异常"]
        self.assertEqual(len(occ_advice), 2)
        # 但只有一条待复诊随访（旧的被取消）
        status, fu = doctor.get(f"/patients/{pid}/follow-ups?today=2026-01-01")
        self.assertEqual(status, 200)
        occ_fu = [f for f in fu["follow_ups"] if f["status"] == "待复诊"]
        self.assertEqual(len(occ_fu), 1)
        self.assertEqual(occ_fu[0]["advice_id"], r2["persisted"][0]["advice_id"])


if __name__ == "__main__":
    unittest.main()
