"""儿童口腔长期观察与共同决策后端。

身份与访问：
  每个请求带 X-User-Id 头；对某患者的访问必须存在该用户 *当前有效* 的授权
  （未撤销且未过期）。写操作要求授权角色为 clinician；guardian 角色只能
  阅读与登记家长选择。逾期复诊清单只返回调用者被授权的患者。

启动：python3 service.py [--port 8000] [--db data/pedo.db]
自检：python3 service.py --check   （同时校验 domain.json 词表与规则版本）
"""

from __future__ import annotations

import argparse
import json
import os
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import rules
from rules import (
    ADVICE_TYPES,
    IMAGE_SUMMARY_TYPES,
    OBSERVATION_INDICATORS,
    RISK_FACTOR_BY_ISSUE,
    STAGES,
)
from store import Store

SERVICE_ID = "pediatric-oral-followup"

ROLE_CLINICIAN = "clinician"
ROLE_GUARDIAN = "guardian"
KNOWN_ROLES = {ROLE_CLINICIAN, ROLE_GUARDIAN}


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def health():
    """返回稳定的服务身份。"""
    return {"status": "ok", "service": SERVICE_ID}


class App:
    """路由与业务编排；Store 负责持久化，rules 负责临床判断。"""

    def __init__(self, db_path: str):
        self.store = Store(db_path)
        snapshot = rules.rule_snapshot()
        self.store.install_rule_version(snapshot)
        self.rule_version = snapshot["version"]
        self.rule_sha = snapshot["sha256"]

    # ----- 鉴权 -----
    def _user(self, handler: "Handler") -> str:
        uid = handler.headers.get("X-User-Id")
        if not uid:
            raise ApiError(HTTPStatus.UNAUTHORIZED, "unauthenticated",
                           "缺少 X-User-Id 头")
        return uid

    def _authorize(self, handler: "Handler", patient_id: str,
                   write: bool = False) -> dict:
        uid = self._user(handler)
        consent = self.store.active_consent(patient_id, uid)
        if not consent:
            self.store.log_access(uid, handler.command + " " + handler.path,
                                  False, patient_id, "无有效授权")
            # 不区分"从未授权"与"已过期"对外暴露太多细节；但明确告知授权到期，
            # 以便前端引导重新签署，而不是静默拒绝。
            any_row = self.store.list_consents(patient_id)
            expired = any(
                c["user_id"] == uid and c["status"] == "expired" for c in any_row
            )
            if expired:
                raise ApiError(HTTPStatus.FORBIDDEN, "consent_expired",
                               "授权已到期，访问被限制，请重新取得家长授权")
            raise ApiError(HTTPStatus.FORBIDDEN, "forbidden",
                           "无权访问该患者数据")
        if write and consent["role"] != ROLE_CLINICIAN:
            self.store.log_access(uid, handler.command + " " + handler.path,
                                  False, patient_id, f"角色 {consent['role']} 不可写")
            raise ApiError(HTTPStatus.FORBIDDEN, "role_not_permitted",
                           "当前授权角色不允许写入临床记录")
        return consent

    # ----- 路由 -----
    def handle(self, handler: "Handler", method: str, path: str):
        body = handler.read_json()

        # 运维
        if method == "GET" and path == "/health":
            return health()

        # 患者
        m = re.fullmatch(r"/patients", path)
        if method == "POST" and m:
            self._user(handler)
            return self._create_patient(body)

        m = re.fullmatch(r"/patients/([^/]+)", path)
        if method == "GET" and m:
            pid = m.group(1)
            self._authorize(handler, pid)
            return self.store.get_patient(pid)

        # 授权
        # 授权管理属于行政动作：认证即可登记/撤销授权（granted_by 留痕），
        # 不要求调用者事先持有该患者的临床授权，否则系统无法完成首次授权。
        m = re.fullmatch(r"/patients/([^/]+)/consents", path)
        if method == "POST" and m:
            pid = m.group(1)
            if not self.store.get_patient(pid):
                raise ApiError(HTTPStatus.NOT_FOUND, "not_found", "患者不存在")
            self._user(handler)
            return self._grant_consent(pid, body)
        if method == "GET" and m:
            pid = m.group(1)
            self._authorize(handler, pid)
            return self.store.list_consents(pid)

        m = re.fullmatch(r"/patients/([^/]+)/consents/([^/]+)/revoke", path)
        if method == "POST" and m:
            pid, cid = m.group(1), m.group(2)
            self._user(handler)
            consent = self.store.get_consent(cid)
            if not consent or consent["patient_id"] != pid:
                raise ApiError(HTTPStatus.NOT_FOUND, "not_found", "授权不存在")
            self.store.revoke_consent(cid)
            return {"revoked": cid}

        # 检查
        m = re.fullmatch(r"/patients/([^/]+)/exams", path)
        if method == "POST" and m:
            pid = m.group(1)
            self._authorize(handler, pid, write=True)
            return self._create_exam(pid, body, handler.headers.get("X-User-Id"))
        if method == "GET" and m:
            pid = m.group(1)
            self._authorize(handler, pid)
            return {"exams": self.store.list_exams(pid)}

        m = re.fullmatch(r"/exams/([^/]+)", path)
        if method == "GET" and m:
            eid = m.group(1)
            exam = self._exam_or_404(eid)
            self._authorize(handler, exam["patient_id"])
            return self.store.exam_detail(eid)

        # 规则评估 / 发出建议
        m = re.fullmatch(r"/exams/([^/]+)/evaluate", path)
        if method == "POST" and m:
            eid = m.group(1)
            exam = self._exam_or_404(eid)
            self._authorize(handler, exam["patient_id"])
            return self._evaluate(eid)

        m = re.fullmatch(r"/exams/([^/]+)/advice", path)
        if method == "POST" and m:
            eid = m.group(1)
            exam = self._exam_or_404(eid)
            self._authorize(handler, exam["patient_id"], write=True)
            return self._issue_advice(eid, body or {}, handler.headers.get("X-User-Id"))

        m = re.fullmatch(r"/advice/([^/]+)", path)
        if method == "GET" and m:
            aid = m.group(1)
            advice = self._advice_or_404(aid)
            self._authorize(handler, advice["patient_id"])
            return advice

        m = re.fullmatch(r"/advice/([^/]+)/trace", path)
        if method == "GET" and m:
            aid = m.group(1)
            advice = self._advice_or_404(aid)
            self._authorize(handler, advice["patient_id"])
            return self.store.advice_trace(aid)

        # 更正链
        m = re.fullmatch(r"/patients/([^/]+)/items/(findings|images|home_care)/([^/]+)/correction", path)
        if method == "POST" and m:
            pid, kind, item_id = m.group(1), m.group(2), m.group(3)
            self._authorize(handler, pid, write=True)
            return self._correct_item(pid, kind, item_id, body,
                                      handler.headers.get("X-User-Id"))

        m = re.fullmatch(r"/items/(findings|images|home_care)/([^/]+)/chain", path)
        if method == "GET" and m:
            kind, item_id = m.group(1), m.group(2)
            chain = self.store.correction_chain(kind, item_id)
            if not chain:
                raise ApiError(HTTPStatus.NOT_FOUND, "not_found", "记录不存在")
            # 链上的患者取首条记录所属检查
            exam = self.store.get_exam(self._exam_id_of_item(kind, item_id))
            self._authorize(handler, exam["patient_id"])
            return {"item_kind": kind, "chain": chain}

        m = re.fullmatch(r"/patients/([^/]+)/corrections", path)
        if method == "GET" and m:
            pid = m.group(1)
            self._authorize(handler, pid)
            return {"corrections": self.store.list_corrections(pid)}

        # 趋势
        m = re.fullmatch(r"/patients/([^/]+)/trends/([^/]+)", path)
        if method == "GET" and m:
            pid, metric = m.group(1), m.group(2)
            self._authorize(handler, pid)
            if metric not in OBSERVATION_INDICATORS:
                raise ApiError(HTTPStatus.BAD_REQUEST, "unknown_metric",
                               f"未知观察指标：{metric}")
            return {"metric": metric, "points": self.store.metric_trend(pid, metric)}

        m = re.fullmatch(r"/patients/([^/]+)/timeline", path)
        if method == "GET" and m:
            pid = m.group(1)
            self._authorize(handler, pid)
            return self.store.patient_timeline(pid)

        # 计划
        m = re.fullmatch(r"/patients/([^/]+)/plans", path)
        if method == "POST" and m:
            pid = m.group(1)
            self._authorize(handler, pid, write=True)
            return self._create_plan(pid, body, handler.headers.get("X-User-Id"))
        if method == "GET" and m:
            pid = m.group(1)
            self._authorize(handler, pid)
            return {"plans": self.store.list_plans(pid)}

        m = re.fullmatch(r"/plans/([^/]+)/link", path)
        if method == "POST" and m:
            plan_id = m.group(1)
            plan = self._plan_or_404(plan_id)
            self._authorize(handler, plan["patient_id"], write=True)
            linked = (body or {}).get("linked_plan_id")
            other = self._plan_or_404(linked)
            if other["patient_id"] != plan["patient_id"]:
                raise ApiError(HTTPStatus.BAD_REQUEST, "cross_patient_link",
                               "只能关联同一患者的计划")
            self.store.link_plans(plan_id, linked)
            return self.store.get_plan(plan_id)

        m = re.fullmatch(r"/plans/([^/]+)/close", path)
        if method == "POST" and m:
            plan_id = m.group(1)
            plan = self._plan_or_404(plan_id)
            self._authorize(handler, plan["patient_id"], write=True)
            outcome = (body or {}).get("outcome")
            if not outcome:
                raise ApiError(HTTPStatus.BAD_REQUEST, "missing_outcome",
                               "关闭计划需写明结局")
            self.store.close_plan(plan_id, outcome)
            return self.store.get_plan(plan_id)

        # 复诊
        m = re.fullmatch(r"/plans/([^/]+)/followups", path)
        if method == "POST" and m:
            plan_id = m.group(1)
            plan = self._plan_or_404(plan_id)
            self._authorize(handler, plan["patient_id"], write=True)
            due = (body or {}).get("due_date")
            if not due:
                raise ApiError(HTTPStatus.BAD_REQUEST, "missing_due_date",
                               "需要 due_date（YYYY-MM-DD）")
            return self.store.schedule_followup(plan_id, plan["patient_id"], due)

        m = re.fullmatch(r"/followups/([^/]+)/complete", path)
        if method == "POST" and m:
            fu_id = m.group(1)
            fu = self.store.get_followup(fu_id)
            if not fu:
                raise ApiError(HTTPStatus.NOT_FOUND, "not_found", "复诊不存在")
            self._authorize(handler, fu["patient_id"], write=True)
            exam_id = (body or {}).get("exam_id")
            if not self.store.get_exam(exam_id):
                raise ApiError(HTTPStatus.BAD_REQUEST, "unknown_exam",
                               "完成复诊需关联一次真实检查")
            self.store.complete_followup(fu_id, exam_id)
            return self.store.get_followup(fu_id)

        if method == "GET" and path == "/followups/overdue":
            uid = self._user(handler)
            scope = self.store.accessible_patient_ids(uid)
            return {"overdue": self.store.overdue_followups(scope)}

        # 第二意见
        m = re.fullmatch(r"/patients/([^/]+)/opinions", path)
        if method == "POST" and m:
            pid = m.group(1)
            self._authorize(handler, pid, write=True)
            return self._add_opinion(pid, body)
        if method == "GET" and m:
            pid = m.group(1)
            self._authorize(handler, pid)
            return {"second_opinions": self.store.list_second_opinions(pid)}

        # 家长选择（guardian 可写）
        m = re.fullmatch(r"/patients/([^/]+)/decisions", path)
        if method == "POST" and m:
            pid = m.group(1)
            self._authorize(handler, pid)
            return self._add_decision(pid, body)
        if method == "GET" and m:
            pid = m.group(1)
            self._authorize(handler, pid)
            return {"family_decisions": self.store.list_family_decisions(pid)}

        raise ApiError(HTTPStatus.NOT_FOUND, "not_found",
                       f"无此路由：{method} {path}")

    # ----- 业务方法 -----
    def _create_patient(self, body: dict) -> dict:
        name = (body or {}).get("name")
        birth_date = (body or {}).get("birth_date")
        if not name or not birth_date:
            raise ApiError(HTTPStatus.BAD_REQUEST, "missing_fields",
                           "需要 name 与 birth_date")
        return self.store.create_patient(name, birth_date)

    def _grant_consent(self, patient_id: str, body: dict) -> dict:
        user_id = body.get("user_id")
        role = body.get("role")
        expires_at = body.get("expires_at")
        granted_by = body.get("granted_by")
        if not user_id or role not in KNOWN_ROLES or not expires_at:
            raise ApiError(
                HTTPStatus.BAD_REQUEST, "bad_consent",
                f"需要 user_id、role（{'/'.join(sorted(KNOWN_ROLES))}）、expires_at",
            )
        return self.store.grant_consent(patient_id, user_id, role,
                                        granted_by, expires_at)

    def _create_exam(self, patient_id: str, body: dict, clinician_id: str) -> dict:
        stage = body.get("stage")
        if stage not in STAGES:
            raise ApiError(HTTPStatus.BAD_REQUEST, "bad_stage",
                           f"发育阶段必须是：{'、'.join(STAGES)}")
        exam_date = body.get("exam_date")
        if not exam_date:
            raise ApiError(HTTPStatus.BAD_REQUEST, "missing_exam_date",
                           "需要 exam_date（YYYY-MM-DD）")
        age_months = body.get("age_months")
        exam = self.store.create_exam(
            patient_id, stage, age_months, exam_date,
            clinician_id, body.get("notes"),
        )
        for f in body.get("findings", []):
            metric = f.get("metric")
            if metric not in OBSERVATION_INDICATORS:
                raise ApiError(HTTPStatus.BAD_REQUEST, "unknown_metric",
                               f"未知观察指标：{metric}")
            self.store.add_finding(exam["exam_id"], metric, f.get("value"))
        for img in body.get("images", []):
            itype = img.get("type")
            if itype not in IMAGE_SUMMARY_TYPES:
                raise ApiError(HTTPStatus.BAD_REQUEST, "unknown_image_type",
                               f"影像类型必须是：{'、'.join(IMAGE_SUMMARY_TYPES)}")
            if not img.get("summary"):
                raise ApiError(HTTPStatus.BAD_REQUEST, "missing_summary",
                               "影像只保存摘要，summary 不能为空")
            self.store.add_image(exam["exam_id"], itype, img["summary"])
        for care in body.get("home_care", []):
            topic = care.get("topic")
            if not topic:
                raise ApiError(HTTPStatus.BAD_REQUEST, "missing_topic",
                               "家庭护理条目需要 topic")
            self.store.add_home_care(
                exam["exam_id"], topic, bool(care.get("adherent")),
                care.get("detail"),
            )
        return self.store.exam_detail(exam["exam_id"])

    def _exam_or_404(self, exam_id: str) -> dict:
        exam = self.store.get_exam(exam_id)
        if not exam:
            raise ApiError(HTTPStatus.NOT_FOUND, "not_found", "检查不存在")
        return exam

    def _advice_or_404(self, advice_id: str) -> dict:
        advice = self.store.get_advice(advice_id)
        if not advice:
            raise ApiError(HTTPStatus.NOT_FOUND, "not_found", "建议不存在")
        return advice

    def _plan_or_404(self, plan_id: str) -> dict:
        plan = self.store.get_plan(plan_id)
        if not plan:
            raise ApiError(HTTPStatus.NOT_FOUND, "not_found", "计划不存在")
        return plan

    def _exam_id_of_item(self, kind: str, item_id: str) -> str:
        if kind == "findings":
            row = self.store.get_finding(item_id)
        elif kind == "images":
            row = self.store._get_image(item_id)  # noqa: SLF001
        else:
            row = self.store._get_home_care(item_id)  # noqa: SLF001
        if not row:
            raise ApiError(HTTPStatus.NOT_FOUND, "not_found", "记录不存在")
        return row["exam_id"]

    def _collect_evidence(self, exam_id: str) -> dict:
        exam = self.store.get_exam(exam_id)
        findings, images, home_care = self.store.current_exam_items(exam_id)
        opinions = self.store.list_second_opinions(exam["patient_id"])
        previous = []
        exams = self.store.list_exams(exam["patient_id"])
        prior = [e for e in exams if e["seq"] < exam["seq"]]
        if prior:
            pf, _pi, _pc = self.store.current_exam_items(prior[-1]["exam_id"])
            previous = pf
        return {
            "exam": exam,
            "findings": findings,
            "images": images,
            "home_care": home_care,
            "second_opinions": opinions,
            "previous_findings": previous,
        }

    def _evaluate(self, exam_id: str) -> dict:
        evidence = self._collect_evidence(exam_id)
        exam = evidence["exam"]
        results = rules.evaluate(
            findings=evidence["findings"],
            images=evidence["images"],
            home_care=evidence["home_care"],
            second_opinions=evidence["second_opinions"],
            stage=exam["stage"],
            age_months=exam["age_months"],
            previous_findings=evidence["previous_findings"],
        )
        result_dicts = [r.as_dict() for r in results]
        interval = rules.recommend_recall_interval_months(
            exam["stage"], results
        )
        return {
            "rule_version": self.rule_version,
            "rule_sha256": self.rule_sha,
            "exam_id": exam_id,
            "stage": exam["stage"],
            "age_months": exam["age_months"],
            "results": result_dicts,
            "recommended_recall_months": interval,
            "age_safety_notice": (
                "年龄仅用于发育阶段定位与复诊节奏；任何'开始干预'结论都要求"
                "重度错𬌗临床记录、头影测量摘要与正畸专科第二意见三项证据齐备。"
            ),
        }

    def _issue_advice(self, exam_id: str, body: dict, issued_by: str) -> dict:
        evaluation = self._evaluate(exam_id)
        exam = self.store.get_exam(exam_id)
        evidence = self._collect_evidence(exam_id)
        # 证据快照：建议发出瞬间所依据的全部当前条目；之后的更正不会改写它。
        snapshot = {
            "exam": {k: exam[k] for k in
                     ("exam_id", "patient_id", "seq", "stage",
                      "age_months", "exam_date")},
            "findings": evidence["findings"],
            "images": evidence["images"],
            "home_care": evidence["home_care"],
            "second_opinions": evidence["second_opinions"],
        }
        plan_id = body.get("plan_id")
        if plan_id and not self.store.get_plan(plan_id):
            raise ApiError(HTTPStatus.BAD_REQUEST, "unknown_plan", "计划不存在")
        advice = self.store.issue_advice(
            exam_id, exam["patient_id"], self.rule_version,
            evaluation["results"], snapshot, issued_by, plan_id,
        )
        return {**evaluation, "advice": advice}

    def _correct_item(self, patient_id: str, kind: str, item_id: str,
                      body: dict, corrected_by: str) -> dict:
        reason = (body or {}).get("reason")
        updates = body.get("updates")
        if not reason:
            raise ApiError(HTTPStatus.BAD_REQUEST, "missing_reason",
                           "更正记录必须填写原因，形成可审计的更正链")
        if not isinstance(updates, dict) or not updates:
            raise ApiError(HTTPStatus.BAD_REQUEST, "missing_updates",
                           "需要 updates 描述更正后的内容")
        try:
            return self.store.correct_item(
                patient_id, kind, item_id, updates, reason, corrected_by
            )
        except LookupError:
            raise ApiError(HTTPStatus.NOT_FOUND, "not_found", "被更正记录不存在")

    def _create_plan(self, patient_id: str, body: dict, created_by: str) -> dict:
        issue = (body or {}).get("issue")
        if issue not in RISK_FACTOR_BY_ISSUE:
            raise ApiError(
                HTTPStatus.BAD_REQUEST, "unknown_issue",
                "议题必须是：" + "、".join(
                    f"{k}（{v}）" for k, v in RISK_FACTOR_BY_ISSUE.items()
                ),
            )
        linked = body.get("linked_plan_id")
        if linked and not self.store.get_plan(linked):
            raise ApiError(HTTPStatus.BAD_REQUEST, "unknown_plan",
                           "被关联计划不存在")
        return self.store.create_plan(
            patient_id, issue, RISK_FACTOR_BY_ISSUE[issue], created_by, linked
        )

    def _add_opinion(self, patient_id: str, body: dict) -> dict:
        doctor_id = (body or {}).get("doctor_id")
        opinion_type = body.get("opinion_type")
        content = body.get("content")
        if not doctor_id or not opinion_type or not content:
            raise ApiError(HTTPStatus.BAD_REQUEST, "missing_fields",
                           "第二意见需要 doctor_id、opinion_type、content")
        exam_id = body.get("exam_id")
        if exam_id and not self.store.get_exam(exam_id):
            raise ApiError(HTTPStatus.BAD_REQUEST, "unknown_exam", "检查不存在")
        return self.store.add_second_opinion(
            patient_id, doctor_id, opinion_type, content, exam_id
        )

    def _add_decision(self, patient_id: str, body: dict) -> dict:
        parent_id = (body or {}).get("parent_id")
        choice = body.get("choice")
        if not parent_id or not choice:
            raise ApiError(HTTPStatus.BAD_REQUEST, "missing_fields",
                           "家长选择需要 parent_id 与 choice")
        return self.store.add_family_decision(
            patient_id, parent_id, choice, body.get("rationale"),
            body.get("advice_id"), body.get("plan_id"),
        )


class Handler(BaseHTTPRequestHandler):
    app: App = None  # 由 main 注入到类属性

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ApiError(HTTPStatus.BAD_REQUEST, "bad_json", "请求体不是合法 JSON")
        if not isinstance(data, dict):
            raise ApiError(HTTPStatus.BAD_REQUEST, "bad_json", "请求体必须是 JSON 对象")
        return data

    def _dispatch(self, method: str):
        path = self.path.split("?", 1)[0]
        try:
            result = self.app.handle(self, method, path)
            self._write_json(200, result)
        except ApiError as err:
            self._write_json(err.status,
                            {"error": err.code, "message": err.message})

    def _write_json(self, status: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


def run_check() -> int:
    """校验 domain.json 词表与规则代码一致，并打印冻结的规则版本。"""
    base = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(base, "domain.json"), encoding="utf-8") as fh:
        vocab = json.load(fh)
    problems = []
    if vocab.get("发育阶段") != STAGES:
        problems.append("发育阶段词表与 rules.STAGES 不一致")
    if vocab.get("建议类型") != ADVICE_TYPES:
        problems.append("建议类型词表与 rules.ADVICE_TYPES 不一致")
    domain_risks = set(vocab.get("风险因素", []))
    code_risks = set(RISK_FACTOR_BY_ISSUE.values())
    if domain_risks != code_risks:
        problems.append(
            f"风险因素词表不一致：domain={sorted(domain_risks)} "
            f"rules={sorted(code_risks)}"
        )
    snapshot = rules.rule_snapshot()
    if problems:
        for p in problems:
            print("检查失败：" + p)
        return 1
    print("基础检查通过")
    print(f"规则版本：{snapshot['version']}  sha256={snapshot['sha256'][:16]}…")
    print(f"观察指标 {len(rules.OBSERVATION_INDICATORS)} 项，"
          f"影像类型 {len(rules.IMAGE_SUMMARY_TYPES)} 类")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="儿童口腔长期观察")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db", default=os.environ.get("PEDO_DB", "data/pedo.db"))
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        raise SystemExit(run_check())

    db_dir = os.path.dirname(os.path.abspath(args.db))
    os.makedirs(db_dir, exist_ok=True)
    Handler.app = App(args.db)
    print(f"{SERVICE_ID} 监听 :{args.port}，数据库 {args.db}，"
          f"规则版本 {Handler.app.rule_version}")
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
