from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from typing import Any

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.security import Principal
from app.samples.repository import ApprovalRepository, LocationRepository, SampleRepository
from app.services.audit import AuditService


class CollectionService:
    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None):
        self.connection = connection
        self.clock = clock or SystemClock()
        self.audit = AuditService(connection, self.clock)

    def register(self, principal: Principal, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("samples.write")
        existing = self.connection.execute(
            "SELECT * FROM collection_events WHERE field_code=?", (data["field_code"],)
        ).fetchone()
        if existing:
            if dict(existing)["chain_digest"] != self._chain_digest(data):
                raise ConflictError("现场编号已被不同采集信息占用")
            return {**dict(existing), "replayed": True}
        now = to_storage(self.clock.now())
        digest = self._chain_digest(data)
        cursor = self.connection.execute(
            """INSERT INTO collection_events(
                   field_code,project_code,collected_by,collected_at,source_kind,
                   source_reference,quantity,unit,preservation,chain_digest,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                data["field_code"], data["project_code"], data["collected_by"],
                data["collected_at"], data["source_kind"], data["source_reference"],
                data["quantity"], data["unit"], data["preservation"], digest, now,
            ),
        )
        event = dict(
            self.connection.execute(
                "SELECT * FROM collection_events WHERE id=?", (cursor.lastrowid,)
            ).fetchone()
        )
        self.audit.record(
            principal, "collection.register", "collection_event", str(event["id"]), after=event
        )
        return {**event, "replayed": False}

    def _chain_digest(self, data: dict[str, Any]) -> str:
        canonical = json.dumps(
            {
                key: data[key]
                for key in (
                    "field_code", "project_code", "collected_by", "collected_at",
                    "source_kind", "source_reference", "quantity", "unit", "preservation",
                )
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class TransferService:
    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None):
        self.connection = connection
        self.clock = clock or SystemClock()
        self.samples = SampleRepository(connection)
        self.locations = LocationRepository(connection)
        self.audit = AuditService(connection, self.clock)

    def move(self, principal: Principal, sample_id: int, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("samples.write")
        before = self.samples.get(sample_id)
        target = self.locations.get(data["location_id"])
        if before["lifecycle_state"] in {"loaned", "pending_destruction", "destroyed"}:
            raise ConflictError("当前状态禁止转移保管位置")
        if before["location_id"] == target["id"]:
            return {"sample": before, "replayed": True}
        now = to_storage(self.clock.now())
        cursor = self.connection.execute(
            """UPDATE samples SET location_id=?,custody_user_id=?,version=version+1,updated_at=?
               WHERE id=? AND version=?""",
            (target["id"], principal.user_id, now, sample_id, data["expected_version"]),
        )
        if cursor.rowcount != 1:
            raise ConflictError("样品位置或版本已变化")
        after = self.samples.get(sample_id)
        self.samples.append_event(
            sample_id,
            "location.transferred",
            principal.user_id,
            now,
            details={
                "from_location_id": before["location_id"],
                "to_location_id": target["id"],
                "reason": data["reason"],
            },
            correlation_id=data.get("correlation_id"),
        )
        self.audit.record(
            principal,
            "sample.transfer",
            "sample",
            str(sample_id),
            before=before,
            after=after,
            metadata={"reason": data["reason"]},
        )
        return {"sample": after, "replayed": False}


class DestructionService:
    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None):
        self.connection = connection
        self.clock = clock or SystemClock()
        self.samples = SampleRepository(connection)
        self.approvals = ApprovalRepository(connection)
        self.audit = AuditService(connection, self.clock)

    def execute(self, principal: Principal, request_id: int, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("samples.destroy")
        approval = self.approvals.get(request_id)
        if approval["action_type"] != "destruction":
            raise ValidationError("审批请求不是销毁类型")
        if approval["state"] != "approved":
            raise ConflictError("销毁请求尚未完成双人审批")
        if approval["requested_by"] in {data["witness_one"], data["witness_two"]}:
            raise ValidationError("申请人不能同时作为销毁见证人")
        if data["witness_one"] == data["witness_two"]:
            raise ValidationError("两名见证人必须不同")
        existing = self.connection.execute(
            "SELECT * FROM destruction_records WHERE request_id=?", (request_id,)
        ).fetchone()
        if existing:
            return {"record": dict(existing), "sample": self.samples.get(approval["resource_id"]), "replayed": True}
        sample = self.samples.get(approval["resource_id"])
        quantity = float(approval["payload"].get("quantity", sample["quantity"]))
        if quantity <= 0 or quantity > sample["quantity"] - sample["reserved_quantity"]:
            raise ConflictError("审批数量超过当前可销毁数量")
        now = to_storage(self.clock.now())
        remaining = sample["quantity"] - quantity
        updated = self.samples.change_quantity(sample["id"], -quantity, sample["version"], now)
        target_state = "destroyed" if remaining == 0 else "partially_consumed"
        updated = self.samples.set_state(sample["id"], target_state, updated["version"], now)
        certificate = hashlib.sha256(
            json.dumps(
                {
                    "request_id": request_id,
                    "sample_id": sample["id"],
                    "quantity": quantity,
                    "method": data["method"],
                    "witnesses": sorted([data["witness_one"], data["witness_two"]]),
                    "destroyed_at": now,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        cursor = self.connection.execute(
            """INSERT INTO destruction_records(
                   sample_id,request_id,method,witness_one,witness_two,destroyed_quantity,
                   certificate_digest,destroyed_at,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                sample["id"], request_id, data["method"], data["witness_one"],
                data["witness_two"], quantity, certificate, now, now,
            ),
        )
        self.connection.execute(
            "UPDATE approval_requests SET state='executed',version=version+1,updated_at=? WHERE id=?",
            (now, request_id),
        )
        record = dict(
            self.connection.execute(
                "SELECT * FROM destruction_records WHERE id=?", (cursor.lastrowid,)
            ).fetchone()
        )
        self.samples.append_event(
            sample["id"],
            "destroyed",
            principal.user_id,
            now,
            quantity_delta=-quantity,
            from_state=sample["lifecycle_state"],
            to_state=target_state,
            details={"request_id": request_id, "certificate_digest": certificate},
        )
        self.audit.record(
            principal,
            "sample.destroy",
            "sample",
            str(sample["id"]),
            before=sample,
            after=updated,
            metadata={"request_id": request_id, "certificate_digest": certificate},
        )
        return {"record": record, "sample": updated, "replayed": False}


class LineageService:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def graph(self, principal: Principal, sample_id: int) -> dict[str, Any]:
        principal.require("samples.read")
        sample = self.connection.execute("SELECT * FROM samples WHERE id=?", (sample_id,)).fetchone()
        if not sample:
            raise NotFoundError("样品不存在")
        root_id = sample["root_sample_id"] or sample["id"]
        rows = self.connection.execute(
            "SELECT * FROM samples WHERE root_sample_id=? OR id=? ORDER BY lineage_depth,id",
            (root_id, root_id),
        ).fetchall()
        nodes = [dict(row) for row in rows]
        edges = [
            {"parent_sample_id": node["parent_sample_id"], "child_sample_id": node["id"]}
            for node in nodes
            if node["parent_sample_id"]
        ]
        totals: dict[str, float] = {}
        for node in nodes:
            totals[node["unit"]] = totals.get(node["unit"], 0.0) + float(node["quantity"])
        return {"root_sample_id": root_id, "nodes": nodes, "edges": edges, "remaining_by_unit": totals}
