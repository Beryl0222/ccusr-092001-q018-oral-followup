"""只追加的 SQLite 存储层。

临床记录（检查、影像摘要、家庭护理）一经写入不可删除或覆盖；
修订通过新行 + ``supersedes_id`` 形成更正链，旧行保留并标记为非当前版本。
建议（advice）同样不可变，每次评估追加新建议，从而可以从建议追溯到
当时引用的检查记录与规则版本。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    role TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS patients (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    birth_date TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS consents (
    id TEXT PRIMARY KEY,
    patient_id TEXT NOT NULL REFERENCES patients(id),
    grantee_id TEXT NOT NULL REFERENCES users(id),
    scope TEXT NOT NULL,
    granted_by TEXT,
    granted_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT
);
CREATE TABLE IF NOT EXISTS visits (
    id TEXT PRIMARY KEY,
    patient_id TEXT NOT NULL REFERENCES patients(id),
    visit_date TEXT NOT NULL,
    stage TEXT NOT NULL,
    doctor_id TEXT REFERENCES users(id),
    note TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS exams (
    id TEXT PRIMARY KEY,
    patient_id TEXT NOT NULL REFERENCES patients(id),
    visit_id TEXT NOT NULL REFERENCES visits(id),
    metrics_json TEXT NOT NULL,
    note TEXT,
    doctor_id TEXT REFERENCES users(id),
    recorded_at TEXT NOT NULL,
    supersedes_id TEXT REFERENCES exams(id),
    correction_reason TEXT,
    corrected_by TEXT,
    corrected_at TEXT,
    is_current INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS imaging (
    id TEXT PRIMARY KEY,
    patient_id TEXT NOT NULL REFERENCES patients(id),
    visit_id TEXT NOT NULL REFERENCES visits(id),
    image_type TEXT NOT NULL,
    summary TEXT NOT NULL,
    findings_json TEXT NOT NULL DEFAULT '{}',
    doctor_id TEXT REFERENCES users(id),
    recorded_at TEXT NOT NULL,
    supersedes_id TEXT REFERENCES imaging(id),
    correction_reason TEXT,
    corrected_by TEXT,
    corrected_at TEXT,
    is_current INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS home_care (
    id TEXT PRIMARY KEY,
    patient_id TEXT NOT NULL REFERENCES patients(id),
    visit_id TEXT NOT NULL REFERENCES visits(id),
    items_json TEXT NOT NULL,
    reporter_id TEXT REFERENCES users(id),
    recorded_at TEXT NOT NULL,
    supersedes_id TEXT REFERENCES home_care(id),
    correction_reason TEXT,
    corrected_by TEXT,
    corrected_at TEXT,
    is_current INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS second_opinions (
    id TEXT PRIMARY KEY,
    patient_id TEXT NOT NULL REFERENCES patients(id),
    visit_id TEXT NOT NULL REFERENCES visits(id),
    doctor_name TEXT NOT NULL,
    specialty TEXT NOT NULL,
    recommendation TEXT NOT NULL,
    opinion TEXT NOT NULL,
    risk_factors_json TEXT NOT NULL DEFAULT '[]',
    recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS parent_decisions (
    id TEXT PRIMARY KEY,
    patient_id TEXT NOT NULL REFERENCES patients(id),
    visit_id TEXT NOT NULL REFERENCES visits(id),
    choice TEXT NOT NULL,
    rationale TEXT,
    recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS problems (
    id TEXT PRIMARY KEY,
    patient_id TEXT NOT NULL REFERENCES patients(id),
    risk_factor TEXT NOT NULL,
    status TEXT NOT NULL,
    opened_visit_id TEXT REFERENCES visits(id),
    opened_at TEXT NOT NULL,
    closed_visit_id TEXT REFERENCES visits(id),
    closed_at TEXT,
    note TEXT
);
CREATE TABLE IF NOT EXISTS problem_links (
    id TEXT PRIMARY KEY,
    problem_id TEXT NOT NULL REFERENCES problems(id),
    related_problem_id TEXT NOT NULL REFERENCES problems(id),
    relation TEXT NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS plans (
    id TEXT PRIMARY KEY,
    patient_id TEXT NOT NULL REFERENCES patients(id),
    problem_id TEXT NOT NULL REFERENCES problems(id),
    status TEXT NOT NULL,
    opened_visit_id TEXT REFERENCES visits(id),
    opened_at TEXT NOT NULL,
    note TEXT
);
CREATE TABLE IF NOT EXISTS advice (
    id TEXT PRIMARY KEY,
    patient_id TEXT NOT NULL REFERENCES patients(id),
    plan_id TEXT NOT NULL REFERENCES plans(id),
    visit_id TEXT NOT NULL REFERENCES visits(id),
    risk_factor TEXT NOT NULL,
    recommendation_type TEXT NOT NULL,
    rationale TEXT NOT NULL,
    missing_evidence_json TEXT NOT NULL DEFAULT '[]',
    evidence_json TEXT NOT NULL DEFAULT '[]',
    gate_note TEXT,
    rule_version TEXT NOT NULL,
    rule_checksum TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS follow_ups (
    id TEXT PRIMARY KEY,
    patient_id TEXT NOT NULL REFERENCES patients(id),
    plan_id TEXT NOT NULL REFERENCES plans(id),
    advice_id TEXT NOT NULL REFERENCES advice(id),
    due_date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT '待复诊',
    created_visit_id TEXT NOT NULL REFERENCES visits(id),
    created_at TEXT NOT NULL,
    completed_visit_id TEXT REFERENCES visits(id),
    completed_at TEXT,
    cancelled_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_visits_patient ON visits(patient_id, visit_date);
CREATE INDEX IF NOT EXISTS idx_exams_patient ON exams(patient_id, is_current);
CREATE INDEX IF NOT EXISTS idx_imaging_patient ON imaging(patient_id, is_current);
CREATE INDEX IF NOT EXISTS idx_home_care_patient ON home_care(patient_id, is_current);
CREATE INDEX IF NOT EXISTS idx_problems_patient ON problems(patient_id, status);
CREATE INDEX IF NOT EXISTS idx_plans_patient ON plans(patient_id);
CREATE INDEX IF NOT EXISTS idx_advice_patient ON advice(patient_id);
CREATE INDEX IF NOT EXISTS idx_followups_due ON follow_ups(due_date, status);
"""


def new_id() -> str:
    return uuid.uuid4().hex


def utcnow_iso(clock: Optional[Callable[[], datetime]] = None) -> str:
    now = clock() if clock else datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc).isoformat()


class StorageError(Exception):
    pass


class NotFound(StorageError):
    pass


class Conflict(StorageError):
    pass


class Storage:
    """封装全部 SQL；线程安全（每线程一个连接，写操作串行化）。"""

    CORRECTABLE = {"exam": "exams", "imaging": "imaging", "home_care": "home_care"}

    def __init__(self, path: str = ":memory:", clock: Optional[Callable[[], datetime]] = None):
        self.path = path
        self.clock = clock
        # :memory: 是"每连接一个库"，多线程 HTTP 服务下各线程会看不到彼此数据；
        # 改用共享缓存内存库，并由一个长连接保持其存活。
        if path == ":memory:":
            self._uri = f"file:memdb_{new_id()}?mode=memory&cache=shared"
        else:
            self._uri = path
        self._keeper: Optional[sqlite3.Connection] = None
        self._local = threading.local()
        self._writelock = threading.Lock()

    # -- 连接 ----------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._uri, check_same_thread=False, uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._connect()
            self._local.conn = conn
        return conn

    def initialize(self) -> None:
        with self._writelock:
            self._keeper = self._connect()
            self._keeper.executescript(SCHEMA)
            self._keeper.commit()

    def close(self) -> None:
        if self._keeper is not None:
            self._keeper.close()
            self._keeper = None

    def now(self) -> str:
        return utcnow_iso(self.clock)

    # -- 基础主体 -------------------------------------------------------------

    def create_user(self, name: str, role: str) -> dict[str, Any]:
        uid = new_id()
        ts = self.now()
        with self._writelock:
            self._conn().execute(
                "INSERT INTO users(id, name, role, created_at) VALUES(?,?,?,?)",
                (uid, name, role, ts),
            )
            self._conn().commit()
        return {"id": uid, "name": name, "role": role, "created_at": ts}

    def get_user(self, uid: str) -> Optional[dict[str, Any]]:
        row = self._conn().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        return dict(row) if row else None

    def create_patient(self, name: str, birth_date: str) -> dict[str, Any]:
        pid = new_id()
        ts = self.now()
        with self._writelock:
            self._conn().execute(
                "INSERT INTO patients(id, name, birth_date, created_at) VALUES(?,?,?,?)",
                (pid, name, birth_date, ts),
            )
            self._conn().commit()
        return {"id": pid, "name": name, "birth_date": birth_date, "created_at": ts}

    def get_patient(self, pid: str) -> dict[str, Any]:
        row = self._conn().execute("SELECT * FROM patients WHERE id=?", (pid,)).fetchone()
        if not row:
            raise NotFound(f"患者不存在: {pid}")
        return dict(row)

    # -- 授权 ----------------------------------------------------------------

    def grant_consent(
        self,
        patient_id: str,
        grantee_id: str,
        scopes: list[str],
        expires_at: str,
        granted_by: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        self.get_patient(patient_id)
        if not self.get_user(grantee_id):
            raise NotFound(f"被授权用户不存在: {grantee_id}")
        ts = self.now()
        out = []
        with self._writelock:
            for scope in scopes:
                cid = new_id()
                self._conn().execute(
                    "INSERT INTO consents(id, patient_id, grantee_id, scope, granted_by,"
                    " granted_at, expires_at) VALUES(?,?,?,?,?,?,?)",
                    (cid, patient_id, grantee_id, scope, granted_by, ts, expires_at),
                )
                out.append(
                    {
                        "id": cid,
                        "patient_id": patient_id,
                        "grantee_id": grantee_id,
                        "scope": scope,
                        "granted_at": ts,
                        "expires_at": expires_at,
                        "revoked_at": None,
                    }
                )
            self._conn().commit()
        return out

    def revoke_consent(self, consent_id: str) -> None:
        with self._writelock:
            cur = self._conn().execute(
                "UPDATE consents SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
                (self.now(), consent_id),
            )
            self._conn().commit()
        if cur.rowcount == 0:
            raise NotFound(f"授权不存在或已吊销: {consent_id}")

    def active_scopes(
        self, patient_id: str, user_id: str, at: Optional[str] = None
    ) -> list[str]:
        """返回调用者当前对该患者的有效授权范围；到期或吊销的不算。"""
        moment = at or self.now()
        rows = self._conn().execute(
            "SELECT scope FROM consents WHERE patient_id=? AND grantee_id=?"
            " AND revoked_at IS NULL AND expires_at >= ?",
            (patient_id, user_id, moment),
        ).fetchall()
        return sorted({r["scope"] for r in rows})

    # -- 就诊 ----------------------------------------------------------------

    def create_visit(
        self, patient_id: str, visit_date: str, stage: str, doctor_id: str, note: Optional[str] = None
    ) -> dict[str, Any]:
        vid = new_id()
        ts = self.now()
        with self._writelock:
            self._conn().execute(
                "INSERT INTO visits(id, patient_id, visit_date, stage, doctor_id, note, created_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (vid, patient_id, visit_date, stage, doctor_id, note, ts),
            )
            self._conn().commit()
        return self.get_visit(vid)

    def get_visit(self, vid: str) -> dict[str, Any]:
        row = self._conn().execute("SELECT * FROM visits WHERE id=?", (vid,)).fetchone()
        if not row:
            raise NotFound(f"就诊不存在: {vid}")
        return dict(row)

    def list_visits(self, patient_id: str) -> list[dict[str, Any]]:
        rows = self._conn().execute(
            "SELECT * FROM visits WHERE patient_id=? ORDER BY visit_date, created_at",
            (patient_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # -- 只追加记录 -----------------------------------------------------------

    def add_exam(
        self,
        patient_id: str,
        visit_id: str,
        metrics: dict[str, Any],
        doctor_id: str,
        note: Optional[str] = None,
    ) -> dict[str, Any]:
        return self._insert_versioned(
            "exams",
            {
                "patient_id": patient_id,
                "visit_id": visit_id,
                "metrics_json": json.dumps(metrics, ensure_ascii=False),
                "note": note,
                "doctor_id": doctor_id,
            },
            blob_key="metrics_json",
        )

    def add_imaging(
        self,
        patient_id: str,
        visit_id: str,
        image_type: str,
        summary: str,
        doctor_id: str,
        findings: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        return self._insert_versioned(
            "imaging",
            {
                "patient_id": patient_id,
                "visit_id": visit_id,
                "image_type": image_type,
                "summary": summary,
                "findings_json": json.dumps(findings or {}, ensure_ascii=False),
                "doctor_id": doctor_id,
            },
            blob_key=None,
        )

    def add_home_care(
        self,
        patient_id: str,
        visit_id: str,
        items: dict[str, Any],
        reporter_id: str,
    ) -> dict[str, Any]:
        return self._insert_versioned(
            "home_care",
            {
                "patient_id": patient_id,
                "visit_id": visit_id,
                "items_json": json.dumps(items, ensure_ascii=False),
                "reporter_id": reporter_id,
            },
            blob_key="items_json",
        )

    def _insert_versioned(
        self, table: str, fields: dict[str, Any], blob_key: Optional[str]
    ) -> dict[str, Any]:
        rid = new_id()
        ts = self.now()
        cols = ["id", *fields.keys(), "recorded_at", "is_current"]
        vals = [rid, *fields.values(), ts, 1]
        placeholders = ",".join("?" for _ in cols)
        with self._writelock:
            self._conn().execute(
                f"INSERT INTO {table}({','.join(cols)}) VALUES({placeholders})", vals
            )
            self._conn().commit()
        return self._get_versioned(table, rid, blob_key)

    def _get_versioned(self, table: str, rid: str, blob_key: Optional[str]) -> dict[str, Any]:
        row = self._conn().execute(f"SELECT * FROM {table} WHERE id=?", (rid,)).fetchone()
        if not row:
            raise NotFound(f"记录不存在: {rid}")
        return self._decode_versioned(table, dict(row))

    @staticmethod
    def _decode_versioned(table: str, row: dict[str, Any]) -> dict[str, Any]:
        if table == "exams":
            row["metrics"] = json.loads(row.pop("metrics_json"))
        elif table == "imaging":
            row["findings"] = json.loads(row.pop("findings_json") or "{}")
        elif table == "home_care":
            row["items"] = json.loads(row.pop("items_json"))
        row["is_current"] = bool(row["is_current"])
        return row

    def correct_record(
        self,
        kind: str,
        record_id: str,
        corrected_by: str,
        reason: str,
        new_fields: dict[str, Any],
    ) -> dict[str, Any]:
        """修订一条当前记录：旧行保留并标记，写入指向旧行的新版本。"""
        table = self.CORRECTABLE.get(kind)
        if not table:
            raise NotFound(f"不可修订的记录类型: {kind}")
        with self._writelock:
            conn = self._conn()
            old = conn.execute(f"SELECT * FROM {table} WHERE id=?", (record_id,)).fetchone()
            if not old:
                raise NotFound(f"记录不存在: {record_id}")
            if not old["is_current"]:
                raise Conflict("只能修订当前版本，历史版本不可再改")
            ts = self.now()
            new_id_ = new_id()
            cols = [
                c
                for c in old.keys()
                if c
                not in (
                    "id",
                    "recorded_at",
                    "supersedes_id",
                    "correction_reason",
                    "corrected_by",
                    "corrected_at",
                    "is_current",
                )
            ]
            # 复制旧行内容，再用修订字段覆盖；新行指向旧行，旧行保留
            insert_cols = {c: old[c] for c in cols}
            insert_cols.update(new_fields)
            colnames = ",".join(cols)
            ordered = [new_id_, *[insert_cols[c] for c in cols], ts, record_id]
            conn.execute(
                f"INSERT INTO {table}(id,{colnames},recorded_at,supersedes_id,is_current)"
                f" VALUES({','.join(['?'] * (len(cols) + 3))},1)",
                ordered,
            )
            conn.execute(
                f"UPDATE {table} SET is_current=0, correction_reason=?,"
                " corrected_by=?, corrected_at=? WHERE id=?",
                (reason, corrected_by, ts, record_id),
            )
            conn.commit()
        blob_key = {
            "exams": "metrics_json",
            "imaging": None,
            "home_care": "items_json",
        }[table]
        return self._get_versioned(table, new_id_, blob_key)

    def correction_chain(self, kind: str, record_id: str) -> list[dict[str, Any]]:
        """从给定记录沿 supersedes 指针回溯到最早版本，按时间正序返回。"""
        table = self.CORRECTABLE[kind]
        chain = []
        current = record_id
        seen = set()
        while current and current not in seen:
            seen.add(current)
            row = self._conn().execute(
                f"SELECT * FROM {table} WHERE id=?", (current,)
            ).fetchone()
            if not row:
                break
            chain.append(self._decode_versioned(table, dict(row)))
            current = row["supersedes_id"]
        return list(reversed(chain))

    def current_version_id(self, kind: str, record_id: str) -> Optional[str]:
        table = self.CORRECTABLE[kind]
        row = self._conn().execute(
            f"SELECT id, is_current, supersedes_id FROM {table} WHERE id=?", (record_id,)
        ).fetchone()
        if not row:
            return None
        seen = set()
        cur = row
        while not cur["is_current"] and cur["id"] not in seen:
            seen.add(cur["id"])
            nxt = self._conn().execute(
                f"SELECT id, is_current, supersedes_id FROM {table} WHERE supersedes_id=?",
                (cur["id"],),
            ).fetchone()
            if not nxt:
                return None
            cur = nxt
        return cur["id"] if cur["is_current"] else None

    def list_current(self, kind: str, patient_id: str) -> list[dict[str, Any]]:
        table = self.CORRECTABLE[kind]
        rows = self._conn().execute(
            f"SELECT * FROM {table} WHERE patient_id=? AND is_current=1 ORDER BY recorded_at",
            (patient_id,),
        ).fetchall()
        out = []
        for r in rows:
            out.append(self._decode_versioned(table, dict(r)))
        return out

    def get_record(self, kind: str, record_id: str) -> Optional[dict[str, Any]]:
        table = self.CORRECTABLE.get(kind)
        if not table:
            return None
        row = self._conn().execute(f"SELECT * FROM {table} WHERE id=?", (record_id,)).fetchone()
        return self._decode_versioned(table, dict(row)) if row else None

    # -- 二诊意见与家长选择（只追加，永不覆盖） ----------------------------------

    def add_second_opinion(
        self,
        patient_id: str,
        visit_id: str,
        doctor_name: str,
        specialty: str,
        recommendation: str,
        opinion: str,
        risk_factors: list[str],
    ) -> dict[str, Any]:
        rid = new_id()
        ts = self.now()
        with self._writelock:
            self._conn().execute(
                "INSERT INTO second_opinions(id, patient_id, visit_id, doctor_name,"
                " specialty, recommendation, opinion, risk_factors_json, recorded_at)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    rid,
                    patient_id,
                    visit_id,
                    doctor_name,
                    specialty,
                    recommendation,
                    opinion,
                    json.dumps(risk_factors, ensure_ascii=False),
                    ts,
                ),
            )
            self._conn().commit()
        row = self._conn().execute(
            "SELECT * FROM second_opinions WHERE id=?", (rid,)
        ).fetchone()
        d = dict(row)
        d["risk_factors"] = json.loads(d.pop("risk_factors_json"))
        return d

    def list_second_opinions(self, patient_id: str) -> list[dict[str, Any]]:
        rows = self._conn().execute(
            "SELECT * FROM second_opinions WHERE patient_id=? ORDER BY recorded_at",
            (patient_id,),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["risk_factors"] = json.loads(d.pop("risk_factors_json"))
            out.append(d)
        return out

    def add_parent_decision(
        self,
        patient_id: str,
        visit_id: str,
        choice: str,
        rationale: Optional[str] = None,
    ) -> dict[str, Any]:
        rid = new_id()
        ts = self.now()
        with self._writelock:
            self._conn().execute(
                "INSERT INTO parent_decisions(id, patient_id, visit_id, choice,"
                " rationale, recorded_at) VALUES(?,?,?,?,?,?)",
                (rid, patient_id, visit_id, choice, rationale, ts),
            )
            self._conn().commit()
        return dict(
            self._conn().execute(
                "SELECT * FROM parent_decisions WHERE id=?", (rid,)
            ).fetchone()
        )

    def list_parent_decisions(self, patient_id: str) -> list[dict[str, Any]]:
        rows = self._conn().execute(
            "SELECT * FROM parent_decisions WHERE patient_id=? ORDER BY recorded_at",
            (patient_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # -- 问题与计划 -----------------------------------------------------------

    def ensure_open_problem(
        self,
        patient_id: str,
        risk_factor: str,
        visit_id: str,
        note: Optional[str] = None,
    ) -> dict[str, Any]:
        existing = self._conn().execute(
            "SELECT * FROM problems WHERE patient_id=? AND risk_factor=? AND status='活跃'",
            (patient_id, risk_factor),
        ).fetchone()
        if existing:
            return dict(existing)
        pid = new_id()
        ts = self.now()
        with self._writelock:
            self._conn().execute(
                "INSERT INTO problems(id, patient_id, risk_factor, status, opened_visit_id,"
                " opened_at, note) VALUES(?,?,?,?,?,?,?)",
                (pid, patient_id, risk_factor, "活跃", visit_id, ts, note),
            )
            self._conn().commit()
        return dict(
            self._conn().execute("SELECT * FROM problems WHERE id=?", (pid,)).fetchone()
        )

    def close_problem(
        self, problem_id: str, visit_id: str, note: Optional[str] = None
    ) -> dict[str, Any]:
        ts = self.now()
        with self._writelock:
            cur = self._conn().execute(
                "UPDATE problems SET status='已完成', closed_visit_id=?, closed_at=?,"
                " note=COALESCE(?, note) WHERE id=? AND status!='已完成'",
                (visit_id, ts, note, problem_id),
            )
            self._conn().commit()
        if cur.rowcount == 0:
            raise NotFound(f"问题不存在或已关闭: {problem_id}")
        return dict(
            self._conn().execute("SELECT * FROM problems WHERE id=?", (problem_id,)).fetchone()
        )

    def list_problems(self, patient_id: str) -> list[dict[str, Any]]:
        rows = self._conn().execute(
            "SELECT * FROM problems WHERE patient_id=? ORDER BY opened_at", (patient_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def link_problems(
        self, problem_id: str, related_problem_id: str, relation: str, note: Optional[str] = None
    ) -> dict[str, Any]:
        if problem_id == related_problem_id:
            raise Conflict("问题不能与自身关联")
        lid = new_id()
        ts = self.now()
        with self._writelock:
            self._conn().execute(
                "INSERT INTO problem_links(id, problem_id, related_problem_id, relation,"
                " note, created_at) VALUES(?,?,?,?,?,?)",
                (lid, problem_id, related_problem_id, relation, note, ts),
            )
            self._conn().commit()
        return {
            "id": lid,
            "problem_id": problem_id,
            "related_problem_id": related_problem_id,
            "relation": relation,
            "note": note,
            "created_at": ts,
        }

    def ensure_problem_link(
        self, problem_id: str, related_problem_id: str, relation: str
    ) -> Optional[dict[str, Any]]:
        """登记关联；同一对问题、同一关系已存在时不重复插入（幂等）。"""
        existing = self._conn().execute(
            "SELECT * FROM problem_links WHERE problem_id=? AND related_problem_id=?"
            " AND relation=?",
            (problem_id, related_problem_id, relation),
        ).fetchone()
        if existing:
            return None
        return self.link_problems(problem_id, related_problem_id, relation)

    def problem_links(self, patient_id: str) -> list[dict[str, Any]]:
        rows = self._conn().execute(
            "SELECT pl.* FROM problem_links pl JOIN problems p ON p.id=pl.problem_id"
            " WHERE p.patient_id=?",
            (patient_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def ensure_plan(
        self,
        patient_id: str,
        problem_id: str,
        visit_id: str,
        status: str = "活跃",
        note: Optional[str] = None,
    ) -> dict[str, Any]:
        existing = self._conn().execute(
            "SELECT * FROM plans WHERE problem_id=? AND status IN ('活跃','待会诊')",
            (problem_id,),
        ).fetchone()
        if existing:
            return dict(existing)
        plid = new_id()
        ts = self.now()
        with self._writelock:
            self._conn().execute(
                "INSERT INTO plans(id, patient_id, problem_id, status, opened_visit_id,"
                " opened_at, note) VALUES(?,?,?,?,?,?,?)",
                (plid, patient_id, problem_id, status, visit_id, ts, note),
            )
            self._conn().commit()
        return dict(
            self._conn().execute("SELECT * FROM plans WHERE id=?", (plid,)).fetchone()
        )

    def update_plan_status(self, plan_id: str, status: str) -> dict[str, Any]:
        with self._writelock:
            cur = self._conn().execute(
                "UPDATE plans SET status=? WHERE id=?", (status, plan_id)
            )
            self._conn().commit()
        if cur.rowcount == 0:
            raise NotFound(f"计划不存在: {plan_id}")
        return dict(
            self._conn().execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
        )

    def list_plans(self, patient_id: str) -> list[dict[str, Any]]:
        rows = self._conn().execute(
            "SELECT * FROM plans WHERE patient_id=? ORDER BY opened_at", (patient_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    # -- 建议与随访 -----------------------------------------------------------

    def add_advice(
        self,
        patient_id: str,
        plan_id: str,
        visit_id: str,
        risk_factor: str,
        recommendation_type: str,
        rationale: str,
        missing_evidence: list[str],
        evidence: list[dict[str, Any]],
        rule_version: str,
        rule_checksum: str,
        gate_note: Optional[str] = None,
    ) -> dict[str, Any]:
        aid = new_id()
        ts = self.now()
        with self._writelock:
            self._conn().execute(
                "INSERT INTO advice(id, patient_id, plan_id, visit_id, risk_factor,"
                " recommendation_type, rationale, missing_evidence_json, evidence_json,"
                " gate_note, rule_version, rule_checksum, created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    aid,
                    patient_id,
                    plan_id,
                    visit_id,
                    risk_factor,
                    recommendation_type,
                    rationale,
                    json.dumps(missing_evidence, ensure_ascii=False),
                    json.dumps(evidence, ensure_ascii=False),
                    gate_note,
                    rule_version,
                    rule_checksum,
                    ts,
                ),
            )
            self._conn().commit()
        return self.get_advice(aid)

    def get_advice(self, aid: str) -> dict[str, Any]:
        row = self._conn().execute("SELECT * FROM advice WHERE id=?", (aid,)).fetchone()
        if not row:
            raise NotFound(f"建议不存在: {aid}")
        d = dict(row)
        d["missing_evidence"] = json.loads(d.pop("missing_evidence_json"))
        d["evidence"] = json.loads(d.pop("evidence_json"))
        return d

    def list_advice(self, patient_id: str) -> list[dict[str, Any]]:
        rows = self._conn().execute(
            "SELECT * FROM advice WHERE patient_id=? ORDER BY created_at", (patient_id,)
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["missing_evidence"] = json.loads(d.pop("missing_evidence_json"))
            d["evidence"] = json.loads(d.pop("evidence_json"))
            out.append(d)
        return out

    def cancel_pending_follow_ups(self, plan_id: str, visit_id: str) -> int:
        """同一就诊重新评估时，取消该计划在本次就诊早先生成的待复诊随访。

        历史建议保留（只追加），但只有最新一次评估的随访保持待复诊。
        """
        ts = self.now()
        with self._writelock:
            cur = self._conn().execute(
                "UPDATE follow_ups SET status='已取消', cancelled_at=?"
                " WHERE plan_id=? AND created_visit_id=? AND status='待复诊'",
                (ts, plan_id, visit_id),
            )
            self._conn().commit()
            return cur.rowcount

    def add_follow_up(
        self,
        patient_id: str,
        plan_id: str,
        advice_id: str,
        due_date: str,
        visit_id: str,
    ) -> dict[str, Any]:
        fid = new_id()
        ts = self.now()
        with self._writelock:
            self._conn().execute(
                "INSERT INTO follow_ups(id, patient_id, plan_id, advice_id, due_date,"
                " status, created_visit_id, created_at) VALUES(?,?,?,?,?,?,?,?)",
                (fid, patient_id, plan_id, advice_id, due_date, "待复诊", visit_id, ts),
            )
            self._conn().commit()
        return dict(
            self._conn().execute("SELECT * FROM follow_ups WHERE id=?", (fid,)).fetchone()
        )

    def complete_follow_up(self, follow_up_id: str, visit_id: str) -> dict[str, Any]:
        ts = self.now()
        with self._writelock:
            cur = self._conn().execute(
                "UPDATE follow_ups SET status='已完成', completed_visit_id=?, completed_at=?"
                " WHERE id=? AND status='待复诊'",
                (visit_id, ts, follow_up_id),
            )
            self._conn().commit()
        if cur.rowcount == 0:
            raise NotFound(f"随访不存在或已处理: {follow_up_id}")
        return dict(
            self._conn().execute(
                "SELECT * FROM follow_ups WHERE id=?", (follow_up_id,)
            ).fetchone()
        )

    def cancel_follow_up(self, follow_up_id: str) -> dict[str, Any]:
        ts = self.now()
        with self._writelock:
            cur = self._conn().execute(
                "UPDATE follow_ups SET status='已取消', cancelled_at=? WHERE id=?"
                " AND status='待复诊'",
                (ts, follow_up_id),
            )
            self._conn().commit()
        if cur.rowcount == 0:
            raise NotFound(f"随访不存在或已处理: {follow_up_id}")
        return dict(
            self._conn().execute(
                "SELECT * FROM follow_ups WHERE id=?", (follow_up_id,)
            ).fetchone()
        )

    def list_follow_ups(self, patient_id: str, today: str) -> list[dict[str, Any]]:
        rows = self._conn().execute(
            "SELECT * FROM follow_ups WHERE patient_id=? ORDER BY due_date", (patient_id,)
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["computed_status"] = (
                "已逾期"
                if d["status"] == "待复诊" and d["due_date"] < today
                else d["status"]
            )
            out.append(d)
        return out

    def get_follow_up(self, follow_up_id: str) -> Optional[dict[str, Any]]:
        row = self._conn().execute(
            "SELECT * FROM follow_ups WHERE id=?", (follow_up_id,)
        ).fetchone()
        return dict(row) if row else None

    def overdue_rows(self, today: str) -> list[dict[str, Any]]:
        rows = self._conn().execute(
            "SELECT f.*, p.risk_factor AS risk_factor FROM follow_ups f"
            " JOIN plans pl ON pl.id=f.plan_id"
            " JOIN problems p ON p.id=pl.problem_id"
            " WHERE f.status='待复诊' AND f.due_date < ?"
            " ORDER BY f.due_date",
            (today,),
        ).fetchall()
        return [dict(r) for r in rows]

    def list_patient_ids(self) -> list[str]:
        return [r["id"] for r in self._conn().execute("SELECT id FROM patients").fetchall()]

    # -- 溯源 ----------------------------------------------------------------

    def trace_advice(self, advice_id: str) -> dict[str, Any]:
        """从建议回溯：规则版本、证据快照、证据记录当前是否仍有效。"""
        advice = self.get_advice(advice_id)
        visit = self.get_visit(advice["visit_id"])
        evidence_status = []
        for ref in advice["evidence"]:
            kind = ref.get("kind")
            rid = ref.get("record_id")
            item: dict[str, Any] = {**ref}
            item.pop("kind", None)
            if kind in self.CORRECTABLE:
                rec = self.get_record(kind, rid)
                if rec is None:
                    item["current_state"] = "记录缺失"
                elif rec["is_current"]:
                    item["current_state"] = "有效"
                    item["current_record_id"] = rid
                else:
                    new_id_ = self.current_version_id(kind, rid)
                    item["current_state"] = "已被更正"
                    item["replacement_record_id"] = new_id_
                    item["correction_reason"] = rec["correction_reason"]
            else:
                item["current_state"] = "有效"
            evidence_status.append(item)
        return {
            "advice": advice,
            "visit": {"id": visit["id"], "visit_date": visit["visit_date"], "stage": visit["stage"]},
            "rule": {
                "version": advice["rule_version"],
                "checksum": advice["rule_checksum"],
            },
            "evidence": evidence_status,
        }
