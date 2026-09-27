"""SQLite 存储层。

关键不变量：

* 检查只追加：每次复诊是一条新 exam（带递增序号 seq），发现/影像/家庭护理
  挂在各自的 exam 下。复诊时读取既往值计算趋势，永不 UPDATE 覆盖旧值。
* 更正链：错误记录不能删除。更正 = 插入新行 + 旧行标记 superseded_by，
  corrections 表保存旧值/新值/原因/更正人。建议发出时保存证据快照，
  事后更正不会篡改历史建议。
* 授权：consents 带 expires_at / revoked_at；所有患者数据访问都经过
  active_consent 校验，逾期即失效。逾期随访查询只在调用者被授权的
  患者集合内进行，避免泄露给无关人员。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import date, datetime
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS patients (
    patient_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    birth_date TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS consents (
    consent_id TEXT PRIMARY KEY,
    patient_id TEXT NOT NULL REFERENCES patients(patient_id),
    user_id TEXT NOT NULL,
    role TEXT NOT NULL,
    granted_by TEXT,
    granted_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_consents_user ON consents(user_id, patient_id);
CREATE INDEX IF NOT EXISTS idx_consents_patient ON consents(patient_id);

CREATE TABLE IF NOT EXISTS rule_versions (
    version TEXT PRIMARY KEY,
    snapshot_json TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    installed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS exams (
    exam_id TEXT PRIMARY KEY,
    patient_id TEXT NOT NULL REFERENCES patients(patient_id),
    seq INTEGER NOT NULL,
    stage TEXT NOT NULL,
    age_months INTEGER,
    exam_date TEXT NOT NULL,
    clinician_id TEXT,
    notes TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(patient_id, seq)
);

CREATE TABLE IF NOT EXISTS findings (
    finding_id TEXT PRIMARY KEY,
    exam_id TEXT NOT NULL REFERENCES exams(exam_id),
    metric TEXT NOT NULL,
    value TEXT,
    recorded_at TEXT NOT NULL,
    corrected INTEGER NOT NULL DEFAULT 0,
    superseded_by_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_findings_exam ON findings(exam_id);

CREATE TABLE IF NOT EXISTS images (
    image_id TEXT PRIMARY KEY,
    exam_id TEXT NOT NULL REFERENCES exams(exam_id),
    image_type TEXT NOT NULL,
    summary TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    corrected INTEGER NOT NULL DEFAULT 0,
    superseded_by_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_images_exam ON images(exam_id);

CREATE TABLE IF NOT EXISTS home_care (
    home_care_id TEXT PRIMARY KEY,
    exam_id TEXT NOT NULL REFERENCES exams(exam_id),
    topic TEXT NOT NULL,
    adherent INTEGER NOT NULL,
    detail TEXT,
    recorded_at TEXT NOT NULL,
    corrected INTEGER NOT NULL DEFAULT 0,
    superseded_by_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_home_care_exam ON home_care(exam_id);

CREATE TABLE IF NOT EXISTS corrections (
    correction_id TEXT PRIMARY KEY,
    patient_id TEXT NOT NULL REFERENCES patients(patient_id),
    exam_id TEXT NOT NULL,
    item_table TEXT NOT NULL,
    old_item_id TEXT NOT NULL,
    new_item_id TEXT NOT NULL,
    old_payload_json TEXT NOT NULL,
    new_payload_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    corrected_by TEXT NOT NULL,
    corrected_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_corrections_patient ON corrections(patient_id);

CREATE TABLE IF NOT EXISTS plans (
    plan_id TEXT PRIMARY KEY,
    patient_id TEXT NOT NULL REFERENCES patients(patient_id),
    issue TEXT NOT NULL,
    risk_factor TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_by TEXT,
    created_at TEXT NOT NULL,
    linked_plan_id TEXT REFERENCES plans(plan_id),
    closed_at TEXT,
    outcome TEXT
);
CREATE INDEX IF NOT EXISTS idx_plans_patient ON plans(patient_id);

CREATE TABLE IF NOT EXISTS followups (
    followup_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL REFERENCES plans(plan_id),
    patient_id TEXT NOT NULL REFERENCES patients(patient_id),
    due_date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'scheduled',
    created_at TEXT NOT NULL,
    completed_exam_id TEXT REFERENCES exams(exam_id),
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_followups_due ON followups(due_date, status);

CREATE TABLE IF NOT EXISTS advice (
    advice_id TEXT PRIMARY KEY,
    exam_id TEXT NOT NULL REFERENCES exams(exam_id),
    patient_id TEXT NOT NULL REFERENCES patients(patient_id),
    rule_version TEXT NOT NULL,
    results_json TEXT NOT NULL,
    evidence_snapshot_json TEXT NOT NULL,
    plan_id TEXT REFERENCES plans(plan_id),
    issued_by TEXT,
    issued_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_advice_patient ON advice(patient_id);

CREATE TABLE IF NOT EXISTS second_opinions (
    opinion_id TEXT PRIMARY KEY,
    patient_id TEXT NOT NULL REFERENCES patients(patient_id),
    exam_id TEXT REFERENCES exams(exam_id),
    doctor_id TEXT NOT NULL,
    opinion_type TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_opinions_patient ON second_opinions(patient_id);

CREATE TABLE IF NOT EXISTS family_decisions (
    decision_id TEXT PRIMARY KEY,
    patient_id TEXT NOT NULL REFERENCES patients(patient_id),
    advice_id TEXT REFERENCES advice(advice_id),
    plan_id TEXT REFERENCES plans(plan_id),
    parent_id TEXT NOT NULL,
    choice TEXT NOT NULL,
    rationale TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decisions_patient ON family_decisions(patient_id);

CREATE TABLE IF NOT EXISTS access_logs (
    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    actor_id TEXT,
    action TEXT NOT NULL,
    patient_id TEXT,
    allowed INTEGER NOT NULL,
    detail TEXT
);
"""


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _today() -> str:
    return date.today().isoformat()


ITEM_TABLES = {
    "findings": "findings",
    "images": "images",
    "home_care": "home_care",
}


class Store:
    def __init__(self, path: str = ":memory:"):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------- 规则版本 ----------
    def install_rule_version(self, snapshot: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO rule_versions VALUES (?, ?, ?, ?)",
                (snapshot["version"], json.dumps(snapshot, ensure_ascii=False),
                 snapshot["sha256"], _now()),
            )
            self._conn.commit()

    def get_rule_version(self, version: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT snapshot_json FROM rule_versions WHERE version = ?", (version,)
            ).fetchone()
        return json.loads(row["snapshot_json"]) if row else None

    def installed_rule_versions(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT version FROM rule_versions ORDER BY installed_at"
            ).fetchall()
        return [r["version"] for r in rows]

    # ---------- 患者 ----------
    def create_patient(self, name: str, birth_date: str) -> dict:
        pid = _new_id("pat")
        with self._lock:
            self._conn.execute(
                "INSERT INTO patients VALUES (?, ?, ?, ?)",
                (pid, name, birth_date, _now()),
            )
            self._conn.commit()
        return self.get_patient(pid)

    def get_patient(self, patient_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM patients WHERE patient_id = ?", (patient_id,)
            ).fetchone()
        return dict(row) if row else None

    # ---------- 授权 ----------
    def grant_consent(self, patient_id: str, user_id: str, role: str,
                      granted_by: str, expires_at: str) -> dict:
        cid = _new_id("con")
        with self._lock:
            self._conn.execute(
                "INSERT INTO consents VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
                (cid, patient_id, user_id, role, granted_by, _today(), expires_at),
            )
            self._conn.commit()
        return self.get_consent(cid)

    def get_consent(self, consent_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM consents WHERE consent_id = ?", (consent_id,)
            ).fetchone()
        return dict(row) if row else None

    def revoke_consent(self, consent_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE consents SET revoked_at = ? WHERE consent_id = ?",
                (_today(), consent_id),
            )
            self._conn.commit()

    def list_consents(self, patient_id: str, on_date: str | None = None) -> list[dict]:
        on_date = on_date or _today()
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM consents WHERE patient_id = ? ORDER BY granted_at",
                (patient_id,),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["status"] = self._consent_status(d, on_date)
            out.append(d)
        return out

    @staticmethod
    def _consent_status(row: dict, on_date: str) -> str:
        if row.get("revoked_at"):
            return "revoked"
        if row["expires_at"] < on_date:
            return "expired"
        return "active"

    def active_consent(self, patient_id: str, user_id: str,
                       on_date: str | None = None) -> dict | None:
        """返回当前有效的授权；过期或已撤销返回 None。"""
        on_date = on_date or _today()
        with self._lock:
            row = self._conn.execute(
                """SELECT * FROM consents
                   WHERE patient_id = ? AND user_id = ?
                     AND revoked_at IS NULL AND expires_at >= ?
                   ORDER BY expires_at DESC LIMIT 1""",
                (patient_id, user_id, on_date),
            ).fetchone()
        return dict(row) if row else None

    def accessible_patient_ids(self, user_id: str, on_date: str | None = None) -> set[str]:
        on_date = on_date or _today()
        with self._lock:
            rows = self._conn.execute(
                """SELECT DISTINCT patient_id FROM consents
                   WHERE user_id = ? AND revoked_at IS NULL AND expires_at >= ?""",
                (user_id, on_date),
            ).fetchall()
        return {r["patient_id"] for r in rows}

    # ---------- 检查（追加式） ----------
    def create_exam(self, patient_id: str, stage: str, age_months: int | None,
                    exam_date: str, clinician_id: str, notes: str | None = None) -> dict:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS n FROM exams WHERE patient_id = ?",
                (patient_id,),
            ).fetchone()
            seq = row["n"] + 1
            eid = _new_id("exam")
            self._conn.execute(
                "INSERT INTO exams VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (eid, patient_id, seq, stage, age_months, exam_date,
                 clinician_id, notes, _now()),
            )
            self._conn.commit()
        return self.get_exam(eid)

    def get_exam(self, exam_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM exams WHERE exam_id = ?", (exam_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_exams(self, patient_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM exams WHERE patient_id = ? ORDER BY seq", (patient_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    def add_finding(self, exam_id: str, metric: str, value: Any) -> dict:
        fid = _new_id("fnd")
        with self._lock:
            self._conn.execute(
                "INSERT INTO findings VALUES (?, ?, ?, ?, ?, 0, NULL)",
                (fid, exam_id, metric,
                 None if value is None else str(value), _now()),
            )
            self._conn.commit()
        return self.get_finding(fid)

    def get_finding(self, finding_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM findings WHERE finding_id = ?", (finding_id,)
            ).fetchone()
        return dict(row) if row else None

    def add_image(self, exam_id: str, image_type: str, summary: str) -> dict:
        iid = _new_id("img")
        with self._lock:
            self._conn.execute(
                "INSERT INTO images VALUES (?, ?, ?, ?, ?, 0, NULL)",
                (iid, exam_id, image_type, summary, _now()),
            )
            self._conn.commit()
        return self._get_image(iid)

    def _get_image(self, image_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM images WHERE image_id = ?", (image_id,)
            ).fetchone()
        return dict(row) if row else None

    def add_home_care(self, exam_id: str, topic: str, adherent: bool,
                      detail: str | None) -> dict:
        hid = _new_id("care")
        with self._lock:
            self._conn.execute(
                "INSERT INTO home_care VALUES (?, ?, ?, ?, ?, ?, 0, NULL)",
                (hid, exam_id, topic, 1 if adherent else 0, detail, _now()),
            )
            self._conn.commit()
        return self._get_home_care(hid)

    def _get_home_care(self, home_care_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM home_care WHERE home_care_id = ?", (home_care_id,)
            ).fetchone()
        return dict(row) if row else None

    def exam_detail(self, exam_id: str, include_corrected: bool = True) -> dict:
        """组装一次检查的完整内容；默认保留被更正项并标注状态。"""
        exam = self.get_exam(exam_id)
        if not exam:
            return None
        with self._lock:
            f_rows = self._conn.execute(
                "SELECT * FROM findings WHERE exam_id = ? ORDER BY recorded_at", (exam_id,)
            ).fetchall()
            i_rows = self._conn.execute(
                "SELECT * FROM images WHERE exam_id = ? ORDER BY recorded_at", (exam_id,)
            ).fetchall()
            c_rows = self._conn.execute(
                "SELECT * FROM home_care WHERE exam_id = ? ORDER BY recorded_at", (exam_id,)
            ).fetchall()

        def annotate(row, kind_label: str) -> dict:
            d = dict(row)
            d["item_kind"] = kind_label
            d["is_current"] = not d["corrected"] and d["superseded_by_id"] is None
            return d

        exam["findings"] = [annotate(r, "findings") for r in f_rows
                            if include_corrected or not r["corrected"]]
        exam["images"] = [
            self._annotate_image(r) for r in i_rows
            if include_corrected or not r["corrected"]
        ]
        exam["home_care"] = [
            self._annotate_care(r) for r in c_rows
            if include_corrected or not r["corrected"]
        ]
        return exam

    @staticmethod
    def _annotate_image(row: sqlite3.Row) -> dict:
        d = dict(row)
        d["type"] = d.pop("image_type")
        d["item_kind"] = "images"
        d["is_current"] = not d["corrected"] and d["superseded_by_id"] is None
        return d

    @staticmethod
    def _annotate_care(row: sqlite3.Row) -> dict:
        d = dict(row)
        d["adherent"] = bool(d["adherent"])
        d["item_kind"] = "home_care"
        d["is_current"] = not d["corrected"] and d["superseded_by_id"] is None
        return d

    def current_exam_items(self, exam_id: str) -> tuple[list[dict], list[dict], list[dict]]:
        """只返回当前（未被更正）条目，供规则引擎使用。"""
        detail = self.exam_detail(exam_id, include_corrected=False)
        return detail["findings"], detail["images"], detail["home_care"]

    # ---------- 更正链 ----------
    def correct_item(self, patient_id: str, item_kind: str, item_id: str,
                     updates: dict, reason: str, corrected_by: str) -> dict:
        """更正一条错误记录：旧行作废、新行生效、写入 corrections。"""
        if item_kind not in ITEM_TABLES:
            raise ValueError(f"unknown item kind: {item_kind}")
        with self._lock:
            if item_kind == "findings":
                old = self._conn.execute(
                    "SELECT * FROM findings WHERE finding_id = ?", (item_id,)
                ).fetchone()
                if not old:
                    raise LookupError(item_id)
                exam_id = old["exam_id"]
                new_id = _new_id("fnd")
                self._conn.execute(
                    "INSERT INTO findings VALUES (?, ?, ?, ?, ?, 0, NULL)",
                    (new_id, exam_id,
                     updates.get("metric", old["metric"]),
                     None if updates.get("value", old["value"]) is None
                     else str(updates.get("value", old["value"])),
                     _now()),
                )
                self._conn.execute(
                    "UPDATE findings SET corrected = 1, superseded_by_id = ? "
                    "WHERE finding_id = ?",
                    (new_id, item_id),
                )
                new_payload = self._row_payload(
                    "findings", "finding_id", new_id)
                old_payload = dict(old)
            elif item_kind == "images":
                old = self._conn.execute(
                    "SELECT * FROM images WHERE image_id = ?", (item_id,)
                ).fetchone()
                if not old:
                    raise LookupError(item_id)
                exam_id = old["exam_id"]
                new_id = _new_id("img")
                self._conn.execute(
                    "INSERT INTO images VALUES (?, ?, ?, ?, ?, 0, NULL)",
                    (new_id, exam_id,
                     updates.get("type", old["image_type"]),
                     updates.get("summary", old["summary"]), _now()),
                )
                self._conn.execute(
                    "UPDATE images SET corrected = 1, superseded_by_id = ? "
                    "WHERE image_id = ?",
                    (new_id, item_id),
                )
                new_payload = self._row_payload("images", "image_id", new_id)
                old_payload = dict(old)
            else:
                old = self._conn.execute(
                    "SELECT * FROM home_care WHERE home_care_id = ?", (item_id,)
                ).fetchone()
                if not old:
                    raise LookupError(item_id)
                exam_id = old["exam_id"]
                new_id = _new_id("care")
                adherent = updates.get("adherent", bool(old["adherent"]))
                self._conn.execute(
                    "INSERT INTO home_care VALUES (?, ?, ?, ?, ?, ?, 0, NULL)",
                    (new_id, exam_id, updates.get("topic", old["topic"]),
                     1 if adherent else 0,
                     updates.get("detail", old["detail"]), _now()),
                )
                self._conn.execute(
                    "UPDATE home_care SET corrected = 1, superseded_by_id = ? "
                    "WHERE home_care_id = ?",
                    (new_id, item_id),
                )
                new_payload = self._row_payload("home_care", "home_care_id", new_id)
                old_payload = dict(old)

            cid = _new_id("cor")
            self._conn.execute(
                "INSERT INTO corrections VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (cid, patient_id, exam_id, item_kind, item_id, new_id,
                 json.dumps(old_payload, ensure_ascii=False),
                 json.dumps(new_payload, ensure_ascii=False),
                 reason, corrected_by, _now()),
            )
            self._conn.commit()
        return {
            "correction_id": cid,
            "old_item_id": item_id,
            "new_item_id": new_id,
            "item_kind": item_kind,
            "old": old_payload,
            "new": new_payload,
            "reason": reason,
            "corrected_by": corrected_by,
        }

    def _row_payload(self, table: str, pk: str, row_id: str) -> dict:
        row = self._conn.execute(
            f"SELECT * FROM {table} WHERE {pk} = ?", (row_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_corrections(self, patient_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM corrections WHERE patient_id = ? ORDER BY corrected_at",
                (patient_id,),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["old_payload"] = json.loads(d.pop("old_payload_json"))
            d["new_payload"] = json.loads(d.pop("new_payload_json"))
            out.append(d)
        return out

    def correction_chain(self, item_kind: str, item_id: str) -> list[dict]:
        """沿 superseded_by_id 正向走到当前版本，再沿更正记录回溯。"""
        pk = {"findings": "finding_id",
              "images": "image_id",
              "home_care": "home_care_id"}[item_kind]
        chain = []
        current_id = item_id
        seen = set()
        with self._lock:
            # 先回溯到最早版本
            while True:
                row = self._conn.execute(
                    "SELECT * FROM corrections WHERE item_table = ? AND new_item_id = ?",
                    (item_kind, current_id),
                ).fetchone()
                if not row or row["old_item_id"] in seen:
                    break
                seen.add(current_id)
                current_id = row["old_item_id"]
            # 再正向遍历
            while current_id and current_id not in seen:
                seen.add(current_id)
                row = self._conn.execute(
                    f"SELECT * FROM {item_kind} WHERE {pk} = ?", (current_id,)
                ).fetchone()
                if not row:
                    break
                cor = self._conn.execute(
                    "SELECT correction_id, reason, corrected_by, corrected_at "
                    "FROM corrections WHERE item_table = ? AND old_item_id = ?",
                    (item_kind, current_id),
                ).fetchone()
                entry = dict(row)
                entry["correction"] = dict(cor) if cor else None
                chain.append(entry)
                current_id = row["superseded_by_id"]
        return chain

    # ---------- 趋势 ----------
    def metric_trend(self, patient_id: str, metric: str) -> list[dict]:
        """同一指标跨复诊的取值序列（仅当前条目），用于比较变化而非覆盖。"""
        with self._lock:
            rows = self._conn.execute(
                """SELECT e.seq, e.exam_date, e.exam_id, f.metric, f.value,
                          f.recorded_at
                   FROM findings f JOIN exams e ON e.exam_id = f.exam_id
                   WHERE e.patient_id = ? AND f.metric = ?
                     AND f.corrected = 0 AND f.superseded_by_id IS NULL
                   ORDER BY e.seq""",
                (patient_id, metric),
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------- 计划（独立但可关联） ----------
    def create_plan(self, patient_id: str, issue: str, risk_factor: str,
                    created_by: str, linked_plan_id: str | None = None) -> dict:
        pid = _new_id("plan")
        with self._lock:
            self._conn.execute(
                "INSERT INTO plans VALUES (?, ?, ?, ?, 'active', ?, ?, ?, NULL, NULL)",
                (pid, patient_id, issue, risk_factor, created_by, _now(),
                 linked_plan_id),
            )
            self._conn.commit()
        return self.get_plan(pid)

    def get_plan(self, plan_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM plans WHERE plan_id = ?", (plan_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_plans(self, patient_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM plans WHERE patient_id = ? ORDER BY created_at",
                (patient_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def link_plans(self, plan_id: str, linked_plan_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE plans SET linked_plan_id = ? WHERE plan_id = ?",
                (linked_plan_id, plan_id),
            )
            self._conn.commit()

    def close_plan(self, plan_id: str, outcome: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE plans SET status = 'completed', closed_at = ?, outcome = ? "
                "WHERE plan_id = ?",
                (_today(), outcome, plan_id),
            )
            self._conn.commit()

    # ---------- 复诊 ----------
    def schedule_followup(self, plan_id: str, patient_id: str,
                          due_date: str) -> dict:
        fid = _new_id("fu")
        with self._lock:
            self._conn.execute(
                "INSERT INTO followups VALUES (?, ?, ?, ?, 'scheduled', ?, NULL, NULL)",
                (fid, plan_id, patient_id, due_date, _now()),
            )
            self._conn.commit()
        return self.get_followup(fid)

    def get_followup(self, followup_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM followups WHERE followup_id = ?", (followup_id,)
            ).fetchone()
        return dict(row) if row else None

    def complete_followup(self, followup_id: str, exam_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE followups SET status = 'completed', completed_exam_id = ?, "
                "completed_at = ? WHERE followup_id = ?",
                (exam_id, _today(), followup_id),
            )
            self._conn.commit()

    def overdue_followups(self, patient_ids: Iterable[str],
                          as_of: str | None = None) -> list[dict]:
        """逾期未完成的复诊，限定在给定患者集合（调用者的授权范围）内。"""
        as_of = as_of or _today()
        ids = list(patient_ids)
        if not ids:
            return []
        marks = ",".join("?" for _ in ids)
        with self._lock:
            rows = self._conn.execute(
                f"""SELECT f.*, p.issue, p.risk_factor, pat.name AS patient_name
                    FROM followups f
                    JOIN plans p ON p.plan_id = f.plan_id
                    JOIN patients pat ON pat.patient_id = f.patient_id
                    WHERE f.status = 'scheduled' AND f.due_date < ?
                      AND p.status = 'active'
                      AND f.patient_id IN ({marks})
                    ORDER BY f.due_date""",
                [as_of, *ids],
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------- 建议（含证据快照与规则版本） ----------
    def issue_advice(self, exam_id: str, patient_id: str, rule_version: str,
                     results: list[dict], evidence_snapshot: dict,
                     issued_by: str, plan_id: str | None = None) -> dict:
        aid = _new_id("adv")
        with self._lock:
            self._conn.execute(
                "INSERT INTO advice VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (aid, exam_id, patient_id, rule_version,
                 json.dumps(results, ensure_ascii=False),
                 json.dumps(evidence_snapshot, ensure_ascii=False),
                 plan_id, issued_by, _now()),
            )
            self._conn.commit()
        return self.get_advice(aid)

    def get_advice(self, advice_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM advice WHERE advice_id = ?", (advice_id,)
            ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["results"] = json.loads(d.pop("results_json"))
        d["evidence_snapshot"] = json.loads(d.pop("evidence_snapshot_json"))
        return d

    def list_advice(self, patient_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT advice_id, exam_id, rule_version, plan_id, issued_by, issued_at "
                "FROM advice WHERE patient_id = ? ORDER BY issued_at",
                (patient_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def advice_trace(self, advice_id: str) -> dict | None:
        """从建议追溯到：检查、规则版本快照、当时证据快照。"""
        advice = self.get_advice(advice_id)
        if not advice:
            return None
        exam = self.get_exam(advice["exam_id"])
        rule_snapshot = self.get_rule_version(advice["rule_version"])
        return {
            "advice": advice,
            "exam": exam,
            "rule_version_snapshot": rule_snapshot,
        }

    # ---------- 第二意见 ----------
    def add_second_opinion(self, patient_id: str, doctor_id: str,
                           opinion_type: str, content: str,
                           exam_id: str | None = None) -> dict:
        oid = _new_id("opn")
        with self._lock:
            self._conn.execute(
                "INSERT INTO second_opinions VALUES (?, ?, ?, ?, ?, ?, ?)",
                (oid, patient_id, exam_id, doctor_id, opinion_type, content, _now()),
            )
            self._conn.commit()
        return self.get_second_opinion(oid)

    def get_second_opinion(self, opinion_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM second_opinions WHERE opinion_id = ?", (opinion_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_second_opinions(self, patient_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM second_opinions WHERE patient_id = ? ORDER BY created_at",
                (patient_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------- 家长选择 ----------
    def add_family_decision(self, patient_id: str, parent_id: str, choice: str,
                            rationale: str | None, advice_id: str | None = None,
                            plan_id: str | None = None) -> dict:
        did = _new_id("dec")
        with self._lock:
            self._conn.execute(
                "INSERT INTO family_decisions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (did, patient_id, advice_id, plan_id, parent_id, choice,
                 rationale, _now()),
            )
            self._conn.commit()
        return self.get_family_decision(did)

    def get_family_decision(self, decision_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM family_decisions WHERE decision_id = ?", (decision_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_family_decisions(self, patient_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM family_decisions WHERE patient_id = ? ORDER BY created_at",
                (patient_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------- 时间线 ----------
    def patient_timeline(self, patient_id: str) -> dict:
        """复诊比较与共同决策的纵向视图（所有历史保留，不做覆盖）。"""
        exams = self.list_exams(patient_id)
        plans = self.list_plans(patient_id)
        decisions = self.list_family_decisions(patient_id)
        opinions = self.list_second_opinions(patient_id)
        advice = self.list_advice(patient_id)
        corrections = self.list_corrections(patient_id)
        with self._lock:
            fu_rows = self._conn.execute(
                "SELECT * FROM followups WHERE patient_id = ? ORDER BY due_date",
                (patient_id,),
            ).fetchall()
        followups = [dict(r) for r in fu_rows]

        metrics: dict[str, list[dict]] = {}
        with self._lock:
            m_rows = self._conn.execute(
                """SELECT e.seq, e.exam_date, f.metric, f.value
                   FROM findings f JOIN exams e ON e.exam_id = f.exam_id
                   WHERE e.patient_id = ?
                     AND f.corrected = 0 AND f.superseded_by_id IS NULL
                   ORDER BY e.seq""",
                (patient_id,),
            ).fetchall()
        for r in m_rows:
            metrics.setdefault(r["metric"], []).append(
                {"seq": r["seq"], "exam_date": r["exam_date"], "value": r["value"]}
            )
        return {
            "patient": self.get_patient(patient_id),
            "exams": exams,
            "metric_trends": metrics,
            "plans": plans,
            "followups": followups,
            "advice": advice,
            "second_opinions": opinions,
            "family_decisions": decisions,
            "corrections": corrections,
        }

    # ---------- 审计 ----------
    def log_access(self, actor_id: str, action: str, allowed: bool,
                   patient_id: str | None = None, detail: str | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO access_logs(ts, actor_id, action, patient_id, allowed, detail)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (_now(), actor_id, action, patient_id, 1 if allowed else 0, detail),
            )
            self._conn.commit()

    def list_access_logs(self, limit: int = 100) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM access_logs ORDER BY log_id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]
