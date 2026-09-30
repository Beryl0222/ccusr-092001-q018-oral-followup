"""HTTP 应用层：路由、请求校验、授权控制与证据组装。

授权模型
========
每个请求带 ``X-User-Id`` 头。访问某患者的数据需要对该患者存在**有效**
（未吊销、未到期）授权，授权按范围（scope）授予：

* ``儿童口腔随访``：全风险可见、可写临床记录；
* ``龋病管理``：仅见龋病/清洁相关风险；
* ``正畸评估``：仅见咬合/吮指/口呼吸相关风险；
* ``影像查阅``：仅见影像摘要，不见随访清单。

范围到风险的映射保存在 ``domain.json`` 的 ``范围可见风险``，
响应中的指标、建议、逾期项都按该映射过滤，做到"找得到逾期随访，
但不向无关人员泄露病情"。
"""

from __future__ import annotations

import json
import re
import urllib.parse
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

import rules as rules_mod
from service import SERVICE_ID
from storage import Conflict, NotFound, Storage

DOMAIN_PATH = Path(__file__).with_name("domain.json")


def load_domain(path: Path | str = DOMAIN_PATH) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        domain = json.load(f)
    # 基础词表完整性自检
    for key in ("发育阶段", "风险因素", "建议类型", "观察指标"):
        if key not in domain:
            raise ValueError(f"domain.json 缺少词表: {key}")
    return domain


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


# ---------------------------------------------------------------------------


class Api:
    def __init__(self, storage: Storage, domain: dict[str, Any]):
        self.db = storage
        self.domain = domain
        self.metric_defs = {m["键"]: m for m in domain["观察指标"]}
        self.risk_visible = domain["范围可见风险"]

    # -- 工具 ----------------------------------------------------------------

    def user_from_header(self, headers: Any) -> dict[str, Any]:
        uid = headers.get("x-user-id")
        if not uid:
            raise ApiError(401, "缺少 X-User-Id 头")
        user = self.db.get_user(uid)
        if not user:
            raise ApiError(401, "用户不存在")
        return user

    def active_scopes(self, patient_id: str, user_id: str) -> list[str]:
        return self.db.active_scopes(patient_id, user_id)

    def require_any_scope(self, patient_id: str, user: dict[str, Any]) -> list[str]:
        scopes = self.active_scopes(patient_id, user["id"])
        if not scopes:
            # 不区分"从未授权/已到期/已吊销"，统一回应，避免暴露授权存在性
            raise ApiError(403, "对该患者无有效授权（授权可能已到期或被吊销）")
        return scopes

    def require_scope(self, patient_id: str, user: dict[str, Any], scope: str) -> list[str]:
        scopes = self.require_any_scope(patient_id, user)
        if scope not in scopes:
            raise ApiError(403, f"该操作需要「{scope}」授权范围")
        return scopes

    def visible_risks(self, scopes: list[str]) -> set[str]:
        out: set[str] = set()
        for s in scopes:
            out.update(self.risk_visible.get(s, []))
        return out

    def can_see_imaging(self, scopes: list[str]) -> bool:
        return "影像查阅" in scopes or "儿童口腔随访" in scopes

    # -- 词表校验 -------------------------------------------------------------

    def _validate_stage(self, stage: str) -> None:
        if stage not in self.domain["发育阶段"]:
            raise ApiError(400, f"未知发育阶段: {stage}")

    def _validate_metrics(self, metrics: dict[str, Any]) -> None:
        for key, value in metrics.items():
            spec = self.metric_defs.get(key)
            if not spec:
                raise ApiError(400, f"未知观察指标: {key}")
            if spec["类型"] == "分类" and value not in spec["取值"]:
                raise ApiError(400, f"指标「{key}」取值非法: {value}")
            if spec["类型"] == "数值" and not isinstance(value, (int, float)):
                raise ApiError(400, f"指标「{key}」必须为数值")

    def _filter_metrics_by_risks(
        self, metrics: dict[str, Any], visible: set[str]
    ) -> dict[str, Any]:
        out = {}
        for key, value in metrics.items():
            spec = self.metric_defs.get(key)
            if spec and set(spec["用于风险"]) & visible:
                out[key] = value
        return out

    # -- 主体与授权 -----------------------------------------------------------

    def create_user(self, body: dict[str, Any]) -> dict[str, Any]:
        return self.db.create_user(body["name"], body.get("role", "医生"))

    def create_patient(self, body: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
        date.fromisoformat(body["birth_date"])
        patient = self.db.create_patient(body["name"], body["birth_date"])
        # 建档者自动获得全范围授权（演示用；生产中应由家长授予）
        expires = "9999-12-31T23:59:59+00:00"
        self.db.grant_consent(
            patient["id"], user["id"], ["儿童口腔随访", "影像查阅"], expires
        )
        return patient

    def grant_consent(self, patient_id: str, body: dict[str, Any]) -> dict[str, Any]:
        scopes = body.get("scopes", [])
        for s in scopes:
            if s not in self.domain["授权范围"]:
                raise ApiError(400, f"未知授权范围: {s}")
        date.fromisoformat(body["expires_at"][:10])
        rows = self.db.grant_consent(
            patient_id,
            body["grantee_id"],
            scopes,
            body["expires_at"],
            granted_by=body.get("granted_by"),
        )
        return {"granted": rows}

    # -- 就诊与记录 -----------------------------------------------------------

    def create_visit(self, patient_id: str, body: dict[str, Any], user: dict[str, Any]):
        self.require_scope(patient_id, user, "儿童口腔随访")
        self._validate_stage(body["stage"])
        date.fromisoformat(body["visit_date"])
        return self.db.create_visit(
            patient_id, body["visit_date"], body["stage"], user["id"], body.get("note")
        )

    def _visit_patient(self, visit_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        visit = self.db.get_visit(visit_id)
        return visit, self.db.get_patient(visit["patient_id"])

    def add_exam(self, visit_id: str, body: dict[str, Any], user: dict[str, Any]):
        visit, _ = self._visit_patient(visit_id)
        self.require_scope(visit["patient_id"], user, "儿童口腔随访")
        metrics = body.get("metrics", {})
        self._validate_metrics(metrics)
        return self.db.add_exam(
            visit["patient_id"], visit_id, metrics, user["id"], body.get("note")
        )

    def add_imaging(self, visit_id: str, body: dict[str, Any], user: dict[str, Any]):
        visit, _ = self._visit_patient(visit_id)
        scopes = self.require_any_scope(visit["patient_id"], user)
        if not self.can_see_imaging(scopes):
            raise ApiError(403, "记录影像摘要需要「影像查阅」授权范围")
        if body.get("image_type") not in self.domain["影像类型"]:
            raise ApiError(400, "未知影像类型")
        return self.db.add_imaging(
            visit["patient_id"],
            visit_id,
            body["image_type"],
            body["summary"],
            user["id"],
            body.get("findings"),
        )

    def add_home_care(self, visit_id: str, body: dict[str, Any], user: dict[str, Any]):
        visit, _ = self._visit_patient(visit_id)
        self.require_scope(visit["patient_id"], user, "儿童口腔随访")
        items = body.get("items", {})
        known = {x["键"] for x in self.domain["家庭护理项"]}
        for key in items:
            if key not in known:
                raise ApiError(400, f"未知家庭护理项: {key}")
        return self.db.add_home_care(visit["patient_id"], visit_id, items, user["id"])

    def add_second_opinion(self, visit_id: str, body: dict[str, Any], user: dict[str, Any]):
        visit, _ = self._visit_patient(visit_id)
        self.require_scope(visit["patient_id"], user, "儿童口腔随访")
        if body.get("specialty") not in self.domain["会诊专科"]:
            raise ApiError(400, "未知会诊专科")
        if body.get("recommendation") not in self.domain["建议类型"]:
            raise ApiError(400, "二诊结论必须取自建议类型词表")
        for r in body.get("risk_factors", []):
            if r not in self.domain["风险因素"]:
                raise ApiError(400, f"未知风险因素: {r}")
        return self.db.add_second_opinion(
            visit["patient_id"],
            visit_id,
            body["doctor_name"],
            body["specialty"],
            body["recommendation"],
            body["opinion"],
            body.get("risk_factors", []),
        )

    def add_parent_decision(self, visit_id: str, body: dict[str, Any], user: dict[str, Any]):
        visit, _ = self._visit_patient(visit_id)
        self.require_scope(visit["patient_id"], user, "儿童口腔随访")
        if body.get("choice") not in self.domain["家长选择"]:
            raise ApiError(400, "家长选择必须取自己长选择词表")
        return self.db.add_parent_decision(
            visit["patient_id"], visit_id, body["choice"], body.get("rationale")
        )

    # -- 更正链 ---------------------------------------------------------------

    _CORRECT_FIELDS = {
        "exam": {"metrics", "note"},
        "imaging": {"image_type", "summary", "findings", "note"},
        "home_care": {"items"},
    }

    def correct_record(self, kind: str, record_id: str, body: dict[str, Any], user):
        rec = self.db.get_record(kind, record_id)
        if not rec:
            raise ApiError(404, "记录不存在")
        self.require_scope(rec["patient_id"], user, "儿童口腔随访")
        reason = body.get("reason")
        if reason not in self.domain["更正原因"]:
            raise ApiError(400, "必须给出更正原因（取自更正原因词表）")
        fields = body.get("fields", {})
        allowed = self._CORRECT_FIELDS[kind]
        unknown = set(fields) - allowed
        if unknown:
            raise ApiError(400, f"该记录类型不可修订字段: {sorted(unknown)}")
        if not fields:
            raise ApiError(400, "没有修订内容")
        if kind == "exam":
            if "metrics" in fields:
                merged = {**rec["metrics"], **fields["metrics"]}
                self._validate_metrics(merged)
                fields = {**fields, "metrics": merged}
            db_fields = {
                ("metrics_json" if k == "metrics" else k): (
                    json.dumps(v, ensure_ascii=False) if k == "metrics" else v
                )
                for k, v in fields.items()
            }
        elif kind == "imaging":
            if "image_type" in fields and fields["image_type"] not in self.domain["影像类型"]:
                raise ApiError(400, "未知影像类型")
            db_fields = {
                ("findings_json" if k == "findings" else k): (
                    json.dumps(v, ensure_ascii=False) if k == "findings" else v
                )
                for k, v in fields.items()
            }
        else:
            merged = {**rec["items"], **fields["items"]}
            db_fields = {"items_json": json.dumps(merged, ensure_ascii=False)}
        return self.db.correct_record(
            kind, record_id, user["id"], reason, db_fields
        )

    def correction_chain(self, kind: str, record_id: str, user):
        rec = self.db.get_record(kind, record_id)
        if not rec:
            raise ApiError(404, "记录不存在")
        self.require_any_scope(rec["patient_id"], user)
        return {"chain": self.db.correction_chain(kind, record_id)}

    # -- 时间线 ---------------------------------------------------------------

    def timeline(self, patient_id: str, user: dict[str, Any]):
        scopes = self.require_any_scope(patient_id, user)
        visible = self.visible_risks(scopes)
        see_imaging = self.can_see_imaging(scopes)
        visits = self.db.list_visits(patient_id)
        exams = self.db.list_current("exam", patient_id)
        images = self.db.list_current("imaging", patient_id) if see_imaging else []
        cares = self.db.list_current("home_care", patient_id)
        opinions = self.db.list_second_opinions(patient_id)
        decisions = self.db.list_parent_decisions(patient_id)

        def group(rows):
            out: dict[str, list] = {}
            for r in rows:
                out.setdefault(r["visit_id"], []).append(r)
            return out

        eg, ig, cg, og, dg = (
            group(exams),
            group(images),
            group(cares),
            group(opinions),
            group(decisions),
        )
        care_risk_map = {
            x["键"]: set(x.get("针对风险", [])) for x in self.domain["家庭护理项"]
        }
        full_scope = "儿童口腔随访" in scopes
        timeline = []
        for v in visits:
            filtered_cares = []
            for c in cg.get(v["id"], []):
                if full_scope:
                    filtered_cares.append(c)
                else:
                    items = {
                        k: val
                        for k, val in c.get("items", {}).items()
                        if care_risk_map.get(k, set()) & visible
                    }
                    if items:
                        filtered_cares.append({**c, "items": items})
            entry: dict[str, Any] = {
                "visit_id": v["id"],
                "visit_date": v["visit_date"],
                "stage": v["stage"],
                "note": v["note"],
                "exams": [
                    {
                        **e,
                        "metrics": self._filter_metrics_by_risks(e["metrics"], visible),
                    }
                    for e in eg.get(v["id"], [])
                ],
                "imaging": ig.get(v["id"], []),
                "home_care": filtered_cares,
                # 二诊意见按其涉及风险过滤；家长选择仅全随访范围可见
                "second_opinions": [
                    o for o in og.get(v["id"], []) if set(o["risk_factors"]) & visible
                ],
                "parent_decisions": dg.get(v["id"], []) if full_scope else [],
            }
            timeline.append(entry)
        return {"patient_id": patient_id, "timeline": timeline}

    # -- 趋势比较 -------------------------------------------------------------

    def _ordered_exams(self, patient_id: str):
        exams = self.db.list_current("exam", patient_id)
        visit_date = {v["id"]: v["visit_date"] for v in self.db.list_visits(patient_id)}
        return sorted(exams, key=lambda e: (visit_date.get(e["visit_id"], ""), e["recorded_at"]))

    @staticmethod
    def _direction(spec: dict[str, Any], prev: Any, curr: Any) -> Optional[str]:
        if prev is None or curr is None:
            return None
        if spec["类型"] == "数值":
            if curr == prev:
                return "稳定"
            higher_risk = curr > prev
            return "加重" if higher_risk == spec.get("高值为风险", True) else "改善"
        values = spec["取值"]
        if prev == curr:
            return "稳定"
        try:
            higher_risk = values.index(curr) > values.index(prev)
        except ValueError:
            return None
        return "加重" if higher_risk else "改善"

    def trends(self, patient_id: str, user: dict[str, Any], metric_keys: list[str] | None):
        scopes = self.require_any_scope(patient_id, user)
        visible = self.visible_risks(scopes)
        keys = metric_keys or list(self.metric_defs)
        exams = self._ordered_exams(patient_id)
        visit_date = {v["id"]: v["visit_date"] for v in self.db.list_visits(patient_id)}
        series: dict[str, list[dict[str, Any]]] = {}
        for key in keys:
            spec = self.metric_defs.get(key)
            if not spec:
                raise ApiError(400, f"未知观察指标: {key}")
            if not (set(spec["用于风险"]) & visible):
                # 不可见风险相关指标直接跳过，不暴露存在性
                continue
            points = []
            for e in exams:
                if key in e["metrics"]:
                    points.append(
                        {
                            "visit_id": e["visit_id"],
                            "visit_date": visit_date.get(e["visit_id"]),
                            "record_id": e["id"],
                            "value": e["metrics"][key],
                        }
                    )
            trend = "数据不足"
            if len(points) >= 2:
                d = self._direction(spec, points[-2]["value"], points[-1]["value"])
                trend = d or "数据不足"
            series[key] = {
                "trend": trend,
                "points": points,
                "previous_record_id": points[-2]["record_id"] if len(points) >= 2 else None,
                "latest_record_id": points[-1]["record_id"] if points else None,
            }
        return {"patient_id": patient_id, "trends": series}

    # -- 评估 -----------------------------------------------------------------

    def _build_bundle(self, visit_id: str) -> dict[str, Any]:
        visit = self.db.get_visit(visit_id)
        patient_id = visit["patient_id"]
        all_exams = self.db.list_current("exam", patient_id)
        visit_exams = [e for e in all_exams if e["visit_id"] == visit_id]
        exam = max(visit_exams, key=lambda e: e["recorded_at"], default=None)

        images = [
            i
            for i in self.db.list_current("imaging", patient_id)
            if i["visit_id"] == visit_id
        ]
        cares = [
            c
            for c in self.db.list_current("home_care", patient_id)
            if c["visit_id"] == visit_id
        ]
        home_care = max(cares, key=lambda c: c["recorded_at"], default=None)

        opinions = [
            o
            for o in self.db.list_second_opinions(patient_id)
            if self.db.get_visit(o["visit_id"])["visit_date"] <= visit["visit_date"]
        ]

        metric_trends: dict[str, str] = {}
        if exam:
            ordered = self._ordered_exams(patient_id)
            prior = [
                e
                for e in ordered
                if self.db.get_visit(e["visit_id"])["visit_date"] < visit["visit_date"]
            ]
            if prior:
                last = prior[-1]
                for key, curr in exam["metrics"].items():
                    spec = self.metric_defs.get(key)
                    prev = last["metrics"].get(key)
                    if spec and prev is not None:
                        d = self._direction(spec, prev, curr)
                        if d:
                            metric_trends[key] = d

        def opinion_view(o):
            return {
                "record_id": o["id"],
                "visit_id": o["visit_id"],
                "visit_date": self.db.get_visit(o["visit_id"])["visit_date"],
                "doctor_name": o["doctor_name"],
                "specialty": o["specialty"],
                "recommendation": o["recommendation"],
            }

        return {
            "patient_id": patient_id,
            "stage": visit["stage"],
            "visit_id": visit_id,
            "visit_date": visit["visit_date"],
            "exam": (
                {
                    "record_id": exam["id"],
                    "visit_id": exam["visit_id"],
                    "visit_date": visit["visit_date"],
                    "metrics": exam["metrics"],
                }
                if exam
                else None
            ),
            "imaging": [
                {
                    "record_id": i["id"],
                    "visit_id": i["visit_id"],
                    "visit_date": visit["visit_date"],
                    "image_type": i["image_type"],
                }
                for i in images
            ],
            "home_care": (
                {
                    "record_id": home_care["id"],
                    "visit_id": home_care["visit_id"],
                    "visit_date": visit["visit_date"],
                    "items": home_care["items"],
                }
                if home_care
                else None
            ),
            "second_opinions": [opinion_view(o) for o in opinions],
            "metric_trends": metric_trends,
        }

    def evaluate(
        self, visit_id: str, body: dict[str, Any], user: dict[str, Any], persist: bool
    ):
        visit = self.db.get_visit(visit_id)
        patient_id = visit["patient_id"]
        scopes = self.require_any_scope(patient_id, user)
        if persist:
            self.require_scope(patient_id, user, "儿童口腔随访")
        visible = self.visible_risks(scopes)

        bundle = self._build_bundle(visit_id)
        evaluation = rules_mod.evaluate(bundle)

        only = set(body.get("risk_factors", [])) if body else set()
        chosen = [r for r in evaluation["results"] if r["risk_factor"] in visible]
        if only:
            unknown = only - set(self.domain["风险因素"])
            if unknown:
                raise ApiError(400, f"未知风险因素: {sorted(unknown)}")
            chosen = [r for r in chosen if r["risk_factor"] in only]

        persisted, skipped = [], []
        if persist:
            if not bundle["exam"]:
                raise ApiError(
                    400,
                    "当次就诊没有结构化检查记录：系统不能仅凭年龄/发育阶段形成矫治相关计划，"
                    "请先录入检查再评估落库（可先不落库预览评估结果）",
                )
            problem_ids: dict[str, str] = {}
            for r in chosen:
                if not r["triggered"]:
                    skipped.append(
                        {"risk_factor": r["risk_factor"], "reason": "本次评估未见该风险指征，例行观察即可"}
                    )
                    continue
                if not r["evidence"]:
                    skipped.append(
                        {"risk_factor": r["risk_factor"], "reason": "缺少该风险的任何证据，先补充检查"}
                    )
                    continue
                problem = self.db.ensure_open_problem(
                    patient_id, r["risk_factor"], visit_id
                )
                problem_ids[r["risk_factor"]] = problem["id"]
                want_status = "待会诊" if r["recommendation_type"] == "专科会诊" else "活跃"
                plan = self.db.ensure_plan(
                    patient_id, problem["id"], visit_id, status=want_status
                )
                if plan["status"] != want_status:
                    plan = self.db.update_plan_status(plan["id"], want_status)
                advice = self.db.add_advice(
                    patient_id,
                    plan["id"],
                    visit_id,
                    r["risk_factor"],
                    r["recommendation_type"],
                    r["rationale"],
                    r["missing_evidence"],
                    r["evidence"],
                    evaluation["rule_version"],
                    evaluation["rule_checksum"],
                    gate_note=r.get("gate_note"),
                )
                due = rules_mod.add_months(
                    visit["visit_date"], r["follow_up_months"] or 12
                )
                self.db.cancel_pending_follow_ups(plan["id"], visit_id)
                follow_up = self.db.add_follow_up(
                    patient_id, plan["id"], advice["id"], due, visit_id
                )
                persisted.append(
                    {
                        "risk_factor": r["risk_factor"],
                        "problem_id": problem["id"],
                        "plan_id": plan["id"],
                        "advice_id": advice["id"],
                        "follow_up_id": follow_up["id"],
                        "due_date": due,
                    }
                )
            # 风险之间相互独立但可关联：评估时幂等登记"可能相关"链接
            for r in chosen:
                pid_a = problem_ids.get(r["risk_factor"])
                if not pid_a:
                    continue
                for related in r.get("related_risks", []):
                    pid_b = problem_ids.get(related)
                    if pid_b:
                        self.db.ensure_problem_link(pid_a, pid_b, "可能相关")

        return {
            "visit_id": visit_id,
            "stage": visit["stage"],
            "rule_version": evaluation["rule_version"],
            "rule_checksum": evaluation["rule_checksum"],
            "age_only_evidence": evaluation["age_only_evidence"],
            "treatment_conclusion_allowed": evaluation["treatment_conclusion_allowed"],
            "results": chosen,
            "persisted": persisted,
            "skipped": skipped,
        }

    # -- 问题/计划/建议/随访 ---------------------------------------------------

    def list_problems(self, patient_id: str, user: dict[str, Any]):
        scopes = self.require_any_scope(patient_id, user)
        visible = self.visible_risks(scopes)
        problems = [p for p in self.db.list_problems(patient_id) if p["risk_factor"] in visible]
        plans = self.db.list_plans(patient_id)
        plan_by_problem: dict[str, list] = {}
        for pl in plans:
            plan_by_problem.setdefault(pl["problem_id"], []).append(pl)
        links = self.db.problem_links(patient_id)
        keep = {p["id"] for p in problems}
        links = [
            lk
            for lk in links
            if lk["problem_id"] in keep and lk["related_problem_id"] in keep
        ]
        return {
            "problems": [
                {**p, "plans": plan_by_problem.get(p["id"], [])} for p in problems
            ],
            "links": links,
        }

    def close_problem(self, patient_id: str, problem_id: str, body: dict[str, Any], user):
        self.require_scope(patient_id, user, "儿童口腔随访")
        problems = {p["id"]: p for p in self.db.list_problems(patient_id)}
        if problem_id not in problems:
            raise ApiError(404, "问题不存在或不属于该患者")
        visit = self.db.get_visit(body["visit_id"])
        if visit["patient_id"] != patient_id:
            raise ApiError(400, "就诊不属于该患者")
        problem = self.db.close_problem(problem_id, body["visit_id"], body.get("note"))
        closed_plans = []
        for pl in self.db.list_plans(patient_id):
            if pl["problem_id"] == problem_id and pl["status"] in ("活跃", "待会诊"):
                closed_plans.append(self.db.update_plan_status(pl["id"], "已完成")["id"])
        return {"problem": problem, "closed_plan_ids": closed_plans}

    def link_problems(self, patient_id: str, body: dict[str, Any], user: dict[str, Any]):
        self.require_scope(patient_id, user, "儿童口腔随访")
        problems = {p["id"]: p for p in self.db.list_problems(patient_id)}
        a, b = body["problem_id"], body["related_problem_id"]
        if a not in problems or b not in problems:
            raise ApiError(404, "问题不存在或不属于该患者")
        return self.db.link_problems(a, b, body.get("relation", "可能相关"), body.get("note"))

    def list_advice(self, patient_id: str, user: dict[str, Any]):
        scopes = self.require_any_scope(patient_id, user)
        visible = self.visible_risks(scopes)
        return {
            "advice": [
                a for a in self.db.list_advice(patient_id) if a["risk_factor"] in visible
            ]
        }

    def trace_advice(self, advice_id: str, user: dict[str, Any]):
        advice = self.db.get_advice(advice_id)
        scopes = self.require_any_scope(advice["patient_id"], user)
        visible = self.visible_risks(scopes)
        if advice["risk_factor"] not in visible:
            # 不可见时返回 404 而非 403，不泄露建议存在性
            raise ApiError(404, "建议不存在")
        return self.db.trace_advice(advice_id)

    def list_follow_ups(self, patient_id: str, user: dict[str, Any], today: str):
        scopes = self.require_any_scope(patient_id, user)
        visible = self.visible_risks(scopes)
        plans = {pl["id"]: pl for pl in self.db.list_plans(patient_id)}
        problems = {p["id"]: p for p in self.db.list_problems(patient_id)}
        out = []
        for f in self.db.list_follow_ups(patient_id, today):
            problem = problems.get(plans.get(f["plan_id"], {}).get("problem_id", ""))
            if not problem or problem["risk_factor"] not in visible:
                continue
            out.append({**f, "risk_factor": problem["risk_factor"]})
        return {"today": today, "follow_ups": out}

    def overdue(self, user: dict[str, Any], today: str):
        """跨患者逾期清单：只含调用者有有效授权的患者，且只返回最小字段。"""
        today_d = date.fromisoformat(today)
        items = []
        for f in self.db.overdue_rows(today):
            pid = f["patient_id"]
            scopes = self.active_scopes(pid, user["id"])
            if not scopes:
                continue
            if f["risk_factor"] not in self.visible_risks(scopes):
                continue
            # 最小暴露：不给病情细节、不附带患者姓名，仅够团队联系随访
            items.append(
                {
                    "follow_up_id": f["id"],
                    "patient_id": pid,
                    "due_date": f["due_date"],
                    "risk_factor": f["risk_factor"],
                    "overdue_days": (today_d - date.fromisoformat(f["due_date"])).days,
                }
            )
        return {"today": today, "overdue": items}

    def complete_follow_up(self, follow_up_id: str, body: dict[str, Any], user: dict[str, Any]):
        row = self.db.get_follow_up(follow_up_id)
        if not row:
            raise ApiError(404, "随访不存在")
        self.require_scope(row["patient_id"], user, "儿童口腔随访")
        visit = self.db.get_visit(body["visit_id"])
        if visit["patient_id"] != row["patient_id"]:
            raise ApiError(400, "复诊就诊与随访不属于同一患者")
        return self.db.complete_follow_up(follow_up_id, body["visit_id"])

    def cancel_follow_up(self, follow_up_id: str, user: dict[str, Any]):
        row = self.db.get_follow_up(follow_up_id)
        if not row:
            raise ApiError(404, "随访不存在")
        self.require_scope(row["patient_id"], user, "儿童口腔随访")
        return self.db.cancel_follow_up(follow_up_id)


# -- HTTP 绑定 ---------------------------------------------------------------

ROUTES = [
    ("GET", re.compile(r"^/health$"), "health"),
    ("POST", re.compile(r"^/users$"), "create_user"),
    ("POST", re.compile(r"^/patients$"), "create_patient"),
    ("POST", re.compile(r"^/patients/(?P<pid>[^/]+)/consents$"), "grant_consent"),
    ("POST", re.compile(r"^/patients/(?P<pid>[^/]+)/visits$"), "create_visit"),
    ("GET", re.compile(r"^/patients/(?P<pid>[^/]+)/timeline$"), "timeline"),
    ("GET", re.compile(r"^/patients/(?P<pid>[^/]+)/trends$"), "trends"),
    ("GET", re.compile(r"^/patients/(?P<pid>[^/]+)/problems$"), "list_problems"),
    ("POST", re.compile(r"^/patients/(?P<pid>[^/]+)/problem-links$"), "link_problems"),
    ("POST", re.compile(r"^/patients/(?P<pid>[^/]+)/problems/(?P<prob>[^/]+)/close$"), "close_problem"),
    ("GET", re.compile(r"^/patients/(?P<pid>[^/]+)/advice$"), "list_advice"),
    ("GET", re.compile(r"^/patients/(?P<pid>[^/]+)/follow-ups$"), "list_follow_ups"),
    ("POST", re.compile(r"^/visits/(?P<vid>[^/]+)/exams$"), "add_exam"),
    ("POST", re.compile(r"^/visits/(?P<vid>[^/]+)/imaging$"), "add_imaging"),
    ("POST", re.compile(r"^/visits/(?P<vid>[^/]+)/home-care$"), "add_home_care"),
    ("POST", re.compile(r"^/visits/(?P<vid>[^/]+)/second-opinions$"), "add_second_opinion"),
    ("POST", re.compile(r"^/visits/(?P<vid>[^/]+)/parent-decisions$"), "add_parent_decision"),
    ("POST", re.compile(r"^/visits/(?P<vid>[^/]+)/evaluate$"), "evaluate"),
    ("POST", re.compile(r"^/records/(?P<kind>[^/]+)/(?P<rid>[^/]+)/correct$"), "correct_record"),
    ("GET", re.compile(r"^/records/(?P<kind>[^/]+)/(?P<rid>[^/]+)/chain$"), "correction_chain"),
    ("GET", re.compile(r"^/advice/(?P<aid>[^/]+)/trace$"), "trace_advice"),
    ("GET", re.compile(r"^/follow-ups/overdue$"), "overdue"),
    ("POST", re.compile(r"^/follow-ups/(?P<fid>[^/]+)/complete$"), "complete_follow_up"),
    ("POST", re.compile(r"^/follow-ups/(?P<fid>[^/]+)/cancel$"), "cancel_follow_up"),
    ("DELETE", re.compile(r"^/consents/(?P<cid>[^/]+)$"), "revoke_consent"),
]


def make_handler(api: Api):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, payload: Any):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_a):
            return

        def _dispatch(self, method: str):
            try:
                parsed_path = urllib.parse.urlparse(self.path)
                path = urllib.parse.unquote(parsed_path.path)
                query = {}
                for k, v in urllib.parse.parse_qsl(parsed_path.query, keep_blank_values=True):
                    query.setdefault(k, []).append(v)
                match = None
                for m, pattern, name in ROUTES:
                    if m == method:
                        match = pattern.match(path)
                        if match:
                            route_name = name
                            break
                if not match:
                    self._send(404, {"error": "未找到端点"})
                    return
                body = {}
                if method in ("POST", "PUT", "PATCH"):
                    length = int(self.headers.get("Content-Length") or 0)
                    raw = self.rfile.read(length) if length else b"{}"
                    try:
                        body = json.loads(raw or b"{}")
                    except json.JSONDecodeError:
                        self._send(400, {"error": "请求体不是合法 JSON"})
                        return
                self._route(route_name, match.groupdict(), query, body)
            except ApiError as e:
                self._send(e.status, {"error": e.message})
            except NotFound as e:
                self._send(404, {"error": str(e)})
            except Conflict as e:
                self._send(409, {"error": str(e)})
            except KeyError as e:
                self._send(400, {"error": f"缺少必填字段: {e.args[0]}"})
            except (ValueError, TypeError) as e:
                self._send(400, {"error": str(e)})

        def _route(self, name: str, groups: dict[str, str], query: dict, body: dict):
            today = query.get("today", [date.today().isoformat()])[0]
            date.fromisoformat(today)

            if name == "health":
                return self._send(200, {"status": "ok", "service": SERVICE_ID})
            if name == "create_user":
                return self._send(201, api.create_user(body))
            user = api.user_from_header(self.headers)

            if name == "create_patient":
                return self._send(201, api.create_patient(body, user))
            if name == "grant_consent":
                return self._send(201, api.grant_consent(groups["pid"], body))
            if name == "revoke_consent":
                api.db.revoke_consent(groups["cid"])
                return self._send(200, {"revoked": groups["cid"]})
            if name == "create_visit":
                return self._send(201, api.create_visit(groups["pid"], body, user))
            if name == "timeline":
                return self._send(200, api.timeline(groups["pid"], user))
            if name == "trends":
                keys = query.get("metrics", [None])[0]
                metric_keys = keys.split(",") if keys else None
                return self._send(200, api.trends(groups["pid"], user, metric_keys))
            if name == "list_problems":
                return self._send(200, api.list_problems(groups["pid"], user))
            if name == "link_problems":
                return self._send(201, api.link_problems(groups["pid"], body, user))
            if name == "close_problem":
                return self._send(
                    200, api.close_problem(groups["pid"], groups["prob"], body, user)
                )
            if name == "list_advice":
                return self._send(200, api.list_advice(groups["pid"], user))
            if name == "list_follow_ups":
                return self._send(200, api.list_follow_ups(groups["pid"], user, today))
            if name == "overdue":
                return self._send(200, api.overdue(user, today))
            if name == "add_exam":
                return self._send(201, api.add_exam(groups["vid"], body, user))
            if name == "add_imaging":
                return self._send(201, api.add_imaging(groups["vid"], body, user))
            if name == "add_home_care":
                return self._send(201, api.add_home_care(groups["vid"], body, user))
            if name == "add_second_opinion":
                return self._send(201, api.add_second_opinion(groups["vid"], body, user))
            if name == "add_parent_decision":
                return self._send(201, api.add_parent_decision(groups["vid"], body, user))
            if name == "evaluate":
                persist = query.get("persist", ["false"])[0] == "true"
                return self._send(200, api.evaluate(groups["vid"], body, user, persist))
            if name == "correct_record":
                return self._send(
                    201, api.correct_record(groups["kind"], groups["rid"], body, user)
                )
            if name == "correction_chain":
                return self._send(
                    200, api.correction_chain(groups["kind"], groups["rid"], user)
                )
            if name == "trace_advice":
                return self._send(200, api.trace_advice(groups["aid"], user))
            if name == "complete_follow_up":
                return self._send(200, api.complete_follow_up(groups["fid"], body, user))
            if name == "cancel_follow_up":
                return self._send(200, api.cancel_follow_up(groups["fid"], user))
            self._send(404, {"error": "未找到端点"})

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_DELETE(self):
            self._dispatch("DELETE")

    return Handler


def create_server(addr, storage: Storage, domain: Optional[dict[str, Any]] = None):
    domain = domain or load_domain()
    api = Api(storage, domain)
    return ThreadingHTTPServer(addr, make_handler(api))
