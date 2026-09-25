from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.core.errors import ConflictError, NotFoundError, ValidationError


def row_dict(row: sqlite3.Row | None) -> dict[str, Any]:
    if row is None:
        raise NotFoundError("记录不存在")
    return dict(row)


class LocationRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def create(self, data: dict[str, Any], now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            """INSERT INTO storage_locations(code,building,room,cabinet,shelf,sensitivity,capacity_units,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                data["code"], data["building"], data["room"], data["cabinet"], data["shelf"],
                data["sensitivity"], data["capacity_units"], now, now,
            ),
        )
        return self.get(cursor.lastrowid)

    def get(self, location_id: int) -> dict[str, Any]:
        return row_dict(self.connection.execute("SELECT * FROM storage_locations WHERE id=?", (location_id,)).fetchone())

    def by_code(self, code: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM storage_locations WHERE code=?", (code,)).fetchone()
        return dict(row) if row else None

    def list(self, active_only: bool = True) -> list[dict[str, Any]]:
        sql = "SELECT * FROM storage_locations"
        if active_only:
            sql += " WHERE active=1"
        sql += " ORDER BY code"
        return [dict(row) for row in self.connection.execute(sql).fetchall()]


class BatchRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def create(self, data: dict[str, Any], actor_user_id: int, qr_payload: str, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            """INSERT INTO receipt_batches(batch_code,project_code,received_by,received_at,expected_count,status,qr_payload,created_at,updated_at)
               VALUES(?,?,?,?,?,'open',?,?,?)""",
            (data["batch_code"], data["project_code"], actor_user_id, now, data["expected_count"], qr_payload, now, now),
        )
        return self.get(cursor.lastrowid)

    def get(self, batch_id: int) -> dict[str, Any]:
        return row_dict(self.connection.execute("SELECT * FROM receipt_batches WHERE id=?", (batch_id,)).fetchone())

    def update_counts(self, batch_id: int, now: str) -> dict[str, Any]:
        accepted = self.connection.execute("SELECT COUNT(*) FROM samples WHERE batch_id=?", (batch_id,)).fetchone()[0]
        self.connection.execute(
            "UPDATE receipt_batches SET accepted_count=?,updated_at=? WHERE id=?",
            (accepted, now, batch_id),
        )
        return self.get(batch_id)


class SampleRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def get(self, sample_id: int) -> dict[str, Any]:
        return row_dict(
            self.connection.execute(
                """SELECT s.*,b.batch_code,l.code AS location_code,l.sensitivity AS location_sensitivity
                   FROM samples s JOIN receipt_batches b ON b.id=s.batch_id
                   LEFT JOIN storage_locations l ON l.id=s.location_id WHERE s.id=?""",
                (sample_id,),
            ).fetchone()
        )

    def by_code(self, sample_code: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM samples WHERE sample_code=?", (sample_code,)).fetchone()
        return dict(row) if row else None

    def list(self, *, state: str | None = None, batch_id: int | None = None, limit: int = 100) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if state:
            clauses.append("s.lifecycle_state=?")
            params.append(state)
        if batch_id:
            clauses.append("s.batch_id=?")
            params.append(batch_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(limit)
        rows = self.connection.execute(
            """SELECT s.*,b.batch_code,l.code AS location_code,l.sensitivity AS location_sensitivity
               FROM samples s JOIN receipt_batches b ON b.id=s.batch_id
               LEFT JOIN storage_locations l ON l.id=s.location_id""" + where + " ORDER BY s.id DESC LIMIT ?",
            tuple(params),
        ).fetchall()
        return [dict(row) for row in rows]

    def create(self, data: dict[str, Any], now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            """INSERT INTO samples(sample_code,batch_id,collection_event_id,parent_sample_id,root_sample_id,sample_type,
               quantity,unit,lifecycle_state,location_id,custody_user_id,lineage_depth,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                data["sample_code"], data["batch_id"], data.get("collection_event_id"), data.get("parent_sample_id"),
                data.get("root_sample_id"), data["sample_type"], data["quantity"], data["unit"], data["lifecycle_state"],
                data.get("location_id"), data.get("custody_user_id"), data.get("lineage_depth", 0), now, now,
            ),
        )
        sample_id = cursor.lastrowid
        if not data.get("root_sample_id"):
            self.connection.execute("UPDATE samples SET root_sample_id=? WHERE id=?", (sample_id, sample_id))
        return self.get(sample_id)

    def change_quantity(self, sample_id: int, delta: float, expected_version: int, now: str) -> dict[str, Any]:
        updated = self.connection.execute(
            """UPDATE samples SET quantity=quantity+?,version=version+1,updated_at=?
               WHERE id=? AND version=? AND quantity+?>=0 AND reserved_quantity<=quantity+?""",
            (delta, now, sample_id, expected_version, delta, delta),
        )
        if updated.rowcount != 1:
            raise ConflictError("样品数量或版本已变化，请刷新后重试")
        return self.get(sample_id)

    def set_state(self, sample_id: int, state: str, expected_version: int, now: str) -> dict[str, Any]:
        updated = self.connection.execute(
            "UPDATE samples SET lifecycle_state=?,version=version+1,updated_at=? WHERE id=? AND version=?",
            (state, now, sample_id, expected_version),
        )
        if updated.rowcount != 1:
            raise ConflictError("样品状态已变化，请刷新后重试")
        return self.get(sample_id)

    def children(self, sample_id: int) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute("SELECT * FROM samples WHERE parent_sample_id=? ORDER BY id", (sample_id,)).fetchall()]

    def events(self, sample_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM sample_events WHERE sample_id=? ORDER BY id", (sample_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item.pop("details_json"))
            result.append(item)
        return result

    def append_event(
        self,
        sample_id: int,
        event_type: str,
        actor_user_id: int | None,
        now: str,
        *,
        quantity_delta: float = 0,
        from_state: str | None = None,
        to_state: str | None = None,
        details: dict[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> None:
        self.connection.execute(
            """INSERT INTO sample_events(sample_id,event_type,actor_user_id,quantity_delta,from_state,to_state,details_json,correlation_id,occurred_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (sample_id, event_type, actor_user_id, quantity_delta, from_state, to_state, json.dumps(details or {}, ensure_ascii=False), correlation_id, now),
        )


class ApprovalRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def create(self, data: dict[str, Any], requested_by: int, request_code: str, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            """INSERT INTO approval_requests(request_code,action_type,resource_type,resource_id,requested_by,payload_json,state,
               required_approvals,expires_at,created_at,updated_at) VALUES(?,?,?,?,?,?,'pending',2,?,?,?)""",
            (request_code, data["action_type"], data["resource_type"], data["resource_id"], requested_by, json.dumps(data["payload"], ensure_ascii=False), data["expires_at"], now, now),
        )
        return self.get(cursor.lastrowid)

    def get(self, request_id: int) -> dict[str, Any]:
        row = row_dict(self.connection.execute("SELECT * FROM approval_requests WHERE id=?", (request_id,)).fetchone())
        row["payload"] = json.loads(row.pop("payload_json"))
        row["decisions"] = [dict(item) for item in self.connection.execute("SELECT * FROM approval_decisions WHERE request_id=? ORDER BY id", (request_id,)).fetchall()]
        return row

    def decide(self, request_id: int, approver_user_id: int, decision: str, comment: str, now: str) -> dict[str, Any]:
        request = self.get(request_id)
        if request["state"] != "pending":
            raise ConflictError("审批请求已经结束")
        if request["requested_by"] == approver_user_id:
            raise ValidationError("申请人不能审批自己的请求")
        self.connection.execute(
            "INSERT INTO approval_decisions(request_id,approver_user_id,decision,comment,decided_at) VALUES(?,?,?,?,?)",
            (request_id, approver_user_id, decision, comment, now),
        )
        decisions = self.connection.execute("SELECT decision FROM approval_decisions WHERE request_id=?", (request_id,)).fetchall()
        state = "rejected" if any(row[0] == "reject" for row in decisions) else ("approved" if len(decisions) >= request["required_approvals"] else "pending")
        self.connection.execute(
            "UPDATE approval_requests SET state=?,version=version+1,updated_at=? WHERE id=?",
            (state, now, request_id),
        )
        return self.get(request_id)


class AnomalyRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def create(self, data: dict[str, Any], detected_by: int, case_code: str, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            """INSERT INTO anomaly_cases(case_code,sample_id,batch_id,anomaly_type,severity,state,detected_by,description,created_at,updated_at)
               VALUES(?,?,?,?,?,'open',?,?,?,?)""",
            (case_code, data.get("sample_id"), data.get("batch_id"), data["anomaly_type"], data["severity"], detected_by, data["description"], now, now),
        )
        return self.get(cursor.lastrowid)

    def get(self, case_id: int) -> dict[str, Any]:
        return row_dict(self.connection.execute("SELECT * FROM anomaly_cases WHERE id=?", (case_id,)).fetchone())

    def list(self, state: str | None = None) -> list[dict[str, Any]]:
        if state:
            rows = self.connection.execute("SELECT * FROM anomaly_cases WHERE state=? ORDER BY id DESC", (state,)).fetchall()
        else:
            rows = self.connection.execute("SELECT * FROM anomaly_cases ORDER BY id DESC").fetchall()
        return [dict(row) for row in rows]
