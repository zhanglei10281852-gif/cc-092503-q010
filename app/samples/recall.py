from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import timedelta
from typing import Any

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.security import Principal
from app.samples.repository import SampleRepository
from app.services.audit import AuditService

# 重新计算时允许的最大连续谱系变化轮次，防止并发分装导致无限循环
MAX_RECALCULATION_ROUNDS = 5

# 仍在库、可以被冻结的样品状态
STOCK_STATES = {"received", "available", "partially_consumed", "quarantined"}
# 视为仍在借用、需要召回的借用单状态
ACTIVE_LOAN_STATES = {"active", "partially_returned", "overdue", "disputed"}


class ContaminationService:
    """从污染事件出发计算受影响后代并落实冻结、召回、不可撤销影响登记。"""

    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None):
        self.connection = connection
        self.clock = clock or SystemClock()
        self.samples = SampleRepository(connection)
        self.audit = AuditService(connection, self.clock)

    # ------------------------------------------------------------------ 创建

    def create_event(self, principal: Principal, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("contamination.manage")
        source = self.samples.get(data["source_sample_id"])
        if source["lifecycle_state"] in {"destroyed"}:
            raise ValidationError("已销毁样品不能作为污染溯源起点")
        now = to_storage(self.clock.now())
        event_code = data.get("event_code") or f"CTM-{uuid.uuid4().hex[:12]}"
        cursor = self.connection.execute(
            """INSERT INTO contamination_events(
                   event_code,source_sample_id,source_sample_version,title,description,severity,
                   state,containment_round,reported_by,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,'containing',0,?,?,?)""",
            (
                event_code, source["id"], source["version"], data["title"],
                data.get("description", ""), data["severity"], principal.user_id, now, now,
            ),
        )
        event_id = cursor.lastrowid
        self.audit.record(
            principal, "contamination.event.create", "contamination_event", str(event_id),
            after={"event_code": event_code, "source_sample_id": source["id"], "severity": data["severity"]},
        )
        result = self._run_round(principal, event_id, now, reason="initial")
        return self.detail(principal, result["id"])

    # ---------------------------------------------------------- 谱系与版本

    def _descendants(self, source_id: int) -> list[dict[str, Any]]:
        """递归 CTE：返回污染起点及其全部分装后代（含跨代）。"""
        return [
            dict(row)
            for row in self.connection.execute(
                """WITH RECURSIVE descendants(id, depth) AS (
                       SELECT id, 0 FROM samples WHERE id=?
                       UNION ALL
                       SELECT s.id, d.depth + 1 FROM samples s
                       JOIN descendants d ON s.parent_sample_id = d.id
                   )
                   SELECT s.id,s.sample_code,s.version,s.lifecycle_state,s.quantity,s.reserved_quantity,
                          s.parent_sample_id,s.root_sample_id,s.lineage_depth,s.contamination_lock,d.depth,
                          (SELECT COALESCE(MAX(e.id),0) FROM sample_events e WHERE e.sample_id=s.id) AS last_event_id
                   FROM samples s JOIN descendants d ON d.id=s.id ORDER BY s.lineage_depth,s.id""",
                (source_id,),
            ).fetchall()
        ]

    @staticmethod
    def _topology_signature(nodes: list[dict[str, Any]]) -> str:
        """谱系拓扑签名：只与节点集合和父子关系有关，不受冻结动作自身版本递增影响。

        执行期间若有新增分装，必然出现新的子样节点（并改变 parent 指针），签名随之变化。
        """
        topology = sorted((n["id"], n["parent_sample_id"], n["lineage_depth"]) for n in nodes)
        canonical = json.dumps(topology, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    # ----------------------------------------------------------- 主处置流程

    def recalculate(self, principal: Principal, event_id: int) -> dict[str, Any]:
        """重试未完成的处置动作，并检测谱系版本变化后重新计算。"""
        principal.require("contamination.manage")
        event = self._get_event_row(event_id)
        if event["state"] in {"released", "cancelled"}:
            raise ConflictError("事件已经结束，不能重新计算")
        now = to_storage(self.clock.now())
        before_sig = event["lineage_signature"]
        result = self._run_round(principal, event_id, now, reason="recalculate")
        changed = bool(before_sig) and before_sig != result["lineage_signature"]
        report = self.detail(principal, event_id)
        report["lineage_changed"] = changed
        return report

    def _run_round(self, principal: Principal, event_id: int, now: str, *, reason: str) -> dict[str, Any]:
        event = self._get_event_row(event_id)
        generation = int(event["lineage_generation"])
        recalculations = 0
        last_change_at: str | None = None
        baseline_signature = event["lineage_signature"]
        for round_index in range(MAX_RECALCULATION_ROUNDS):
            generation += 1
            nodes = self._descendants(event["source_sample_id"])
            signature = self._topology_signature(nodes)
            # 相对上一轮（或事件创建时）记录的签名，谱系已新增分装等变化
            if baseline_signature and signature != baseline_signature:
                recalculations += 1
                last_change_at = now
            self._evaluate_and_apply(principal, event, nodes, generation, now)
            # 处置后再次取谱系：执行期间若新增分装，拓扑签名会变化，需要重新计算
            after_nodes = self._descendants(event["source_sample_id"])
            after_signature = self._topology_signature(after_nodes)
            if after_signature == signature:
                signature = after_signature
                break
            recalculations += 1
            last_change_at = now
            baseline_signature = after_signature  # 本次变化已计数，避免下一轮重复计数
            nodes = after_nodes
            signature = after_signature
        else:  # pragma: no cover - 保护性分支
            raise ConflictError("谱系在处置期间持续变化，超过最大重新计算轮次，请稍后重试")

        self.connection.execute(
            """UPDATE contamination_events SET
                   state='contained',lineage_generation=?,lineage_signature=?,
                   containment_round=containment_round+1,last_change_detected_at=COALESCE(?,last_change_detected_at),
                   recalculation_count=recalculation_count+?,updated_at=?
               WHERE id=?""",
            (generation, signature, last_change_at, recalculations, now, event_id),
        )
        self.audit.record(
            principal, "contamination.containment.run", "contamination_event", str(event_id),
            metadata={"reason": reason, "generation": generation, "recalculations": recalculations,
                      "node_count": len(nodes), "signature": signature[:12]},
        )
        return self._get_event_row(event_id)

    def _evaluate_and_apply(
        self,
        principal: Principal,
        event: dict[str, Any],
        nodes: list[dict[str, Any]],
        generation: int,
        now: str,
    ) -> None:
        event_id = event["id"]
        for node in nodes:
            self._evaluate_consumptions(event_id, node, generation, now)
            self._evaluate_destructions(event_id, node, generation, now)
            self._evaluate_loans(principal, event, node, generation, now)
            self._evaluate_sample_stock(principal, event, node, generation, now)

    # ------------------------------------------------- 各类对象的评估与处置

    def _evaluate_consumptions(self, event_id: int, node: dict[str, Any], generation: int, now: str) -> None:
        records = [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM consumption_records WHERE sample_id=? ORDER BY id",
                (node["id"],),
            ).fetchall()
        ]
        for record in records:
            reason = (
                f"样品 {node['sample_code']} 是污染起点的分装后代（第 {node['lineage_depth']} 代），"
                f"其 {record['quantity']} 单位已用于实验 {record['experiment_code']}，实物无法追回"
            )
            self._upsert_action(
                event_id, "consumption", record["id"], node["id"], "irreversible", "applied",
                reason, generation, now,
                detail={
                    "experiment_code": record["experiment_code"],
                    "quantity": record["quantity"],
                    "operator_user_id": record["operator_user_id"],
                    "occurred_at": record["occurred_at"],
                    "sample_code": node["sample_code"],
                },
            )

    def _evaluate_destructions(self, event_id: int, node: dict[str, Any], generation: int, now: str) -> None:
        rows = self.connection.execute(
            "SELECT * FROM destruction_records WHERE sample_id=? ORDER BY id", (node["id"],)
        ).fetchall()
        for row in rows:
            record = dict(row)
            reason = (
                f"样品 {node['sample_code']} 是污染后代，其中 {record['destroyed_quantity']} 单位已按销毁单销毁，"
                "实物不复存在，构成不可撤销影响"
            )
            self._upsert_action(
                event_id, "destruction", record["id"], node["id"], "irreversible", "applied",
                reason, generation, now,
                detail={
                    "method": record["method"],
                    "destroyed_quantity": record["destroyed_quantity"],
                    "destroyed_at": record["destroyed_at"],
                    "request_id": record["request_id"],
                    "sample_code": node["sample_code"],
                },
            )

    def _evaluate_loans(
        self,
        principal: Principal,
        event: dict[str, Any],
        node: dict[str, Any],
        generation: int,
        now: str,
    ) -> None:
        event_id = event["id"]
        loans = [
            dict(row)
            for row in self.connection.execute(
                """SELECT l.*,u.username AS borrower_username,u.display_name AS borrower_name,
                          u.email AS borrower_email
                   FROM loans l JOIN users u ON u.id=l.borrower_user_id
                   WHERE l.sample_id=? ORDER BY l.id""",
                (node["id"],),
            ).fetchall()
        ]
        for loan in loans:
            if loan["state"] in ACTIVE_LOAN_STATES:
                outstanding = round(loan["quantity"] - loan["returned_quantity"], 9)
                reason = (
                    f"样品 {node['sample_code']} 是污染后代，借用单 {loan['loan_code']} 仍有 "
                    f"{outstanding} 单位在借（状态 {loan['state']}），必须通知借用人并召回"
                )
                self._upsert_action(
                    event_id, "loan", loan["id"], node["id"], "recalled", "pending",
                    reason, generation, now,
                    detail={
                        "loan_code": loan["loan_code"],
                        "borrower_user_id": loan["borrower_user_id"],
                        "borrower_name": loan["borrower_name"],
                        "outstanding_quantity": outstanding,
                        "due_at": loan["due_at"],
                        "sample_code": node["sample_code"],
                    },
                )
                self._send_recall_notification(principal, event, loan, node, now)
                self._mark_action_status(event_id, "loan", loan["id"], "applied", now)
            else:
                reason = (
                    f"借用单 {loan['loan_code']} 状态为 {loan['state']}，样品已回库，"
                    "无需向借用人召回，回库实物按在库样品统一冻结核查"
                )
                self._upsert_action(
                    event_id, "loan", loan["id"], node["id"], "excluded", "excluded",
                    reason, generation, now,
                    detail={"loan_code": loan["loan_code"], "state": loan["state"],
                            "sample_code": node["sample_code"]},
                    exclusion_reason=reason,
                )

    def _evaluate_sample_stock(
        self,
        principal: Principal,
        event: dict[str, Any],
        node: dict[str, Any],
        generation: int,
        now: str,
    ) -> None:
        event_id = event["id"]
        state = node["lifecycle_state"]
        if state in STOCK_STATES:
            reason = (
                f"样品 {node['sample_code']} 是污染起点的分装后代（第 {node['lineage_depth']} 代），"
                f"当前在库数量 {node['quantity'] - node['reserved_quantity']:g}，须立即冻结"
            )
            self._upsert_action(
                event_id, "sample", node["id"], node["id"], "frozen", "pending",
                reason, generation, now,
                detail={"sample_code": node["sample_code"], "lifecycle_state": state,
                        "quantity": node["quantity"], "reserved_quantity": node["reserved_quantity"]},
            )
            lock_result = self._apply_lock(event, node, now, change_state=True, principal=principal)
            if lock_result == "skipped":
                self._mark_action_status(event_id, "sample", node["id"], "skipped", now)
            else:
                self._mark_action_status(event_id, "sample", node["id"], "applied", now)
            return

        if state == "loaned":
            # 样品仍在借用：加污染锁以阻止继续消耗/再借，状态保持 loaned，
            # 具体召回通知按未归还借用单逐条发出；归还时由借用流程转入隔离
            reason = f"样品 {node['sample_code']} 是污染后代且整体处于借用中，按其未归还借用单执行召回"
            self._upsert_action(
                event_id, "sample", node["id"], node["id"], "recalled", "applied",
                reason, generation, now,
                detail={"sample_code": node["sample_code"], "lifecycle_state": state},
            )
            self._apply_lock(event, node, now, change_state=False, principal=principal)
            return

        # consumed / destroyed / pending_destruction：没有可冻结的在库实物，明确登记排除理由
        exclusion = {
            "consumed": f"样品 {node['sample_code']} 已全部消耗，无在库实物可冻结，相关实验记录已列入不可撤销影响清单",
            "destroyed": f"样品 {node['sample_code']} 已销毁，无在库实物可冻结，销毁记录已列入不可撤销影响清单",
            "pending_destruction": f"样品 {node['sample_code']} 已进入待销毁流程，维持管控并等待销毁审批结论",
        }.get(state, f"样品 {node['sample_code']} 当前状态为 {state}，无在库实物可冻结")
        disposition = "excluded"
        status = "excluded"
        if state == "pending_destruction":
            # 待销毁仍可能被执行销毁，保持冻结标记以防被挪作他用
            disposition, status = "frozen", "applied"
        self._upsert_action(
            event_id, "sample", node["id"], node["id"], disposition, status,
            exclusion, generation, now,
            detail={"sample_code": node["sample_code"], "lifecycle_state": state,
                    "quantity": node["quantity"]},
            exclusion_reason=None if disposition == "frozen" else exclusion,
        )
        if disposition == "frozen":
            self._apply_lock(event, node, now, change_state=False, principal=principal)

    def _apply_lock(
        self,
        event: dict[str, Any],
        node: dict[str, Any],
        now: str,
        *,
        change_state: bool,
        principal: Principal,
    ) -> str:
        """给样品加污染锁；change_state=True 时同时转为隔离状态。

        返回 'locked'（本次新加锁）、'existing'（本事件此前已加锁，重试命中）或
        'skipped'（样品被其他污染事件锁定，不抢占）。保证处置可安全重试、不重复写事件。
        """
        event_id = event["id"]
        if node["contamination_lock"] == event_id:
            return "existing"
        if node["contamination_lock"] not in (None, event_id):
            # 已被其他污染事件锁定：不抢占冻结锁，但保留本事件的影响记录
            self.audit.record(
                principal, "contamination.freeze.skip", "sample", str(node["id"]),
                metadata={"event_id": event_id, "locked_by_event": node["contamination_lock"]},
            )
            return "skipped"
        if change_state:
            self.connection.execute(
                """UPDATE samples SET lifecycle_state='quarantined',contamination_lock=?,
                       pre_freeze_state=COALESCE(pre_freeze_state,?),
                       version=version+1,updated_at=? WHERE id=?""",
                (event_id, node["lifecycle_state"], now, node["id"]),
            )
        else:
            self.connection.execute(
                "UPDATE samples SET contamination_lock=?,version=version+1,updated_at=? WHERE id=?",
                (event_id, now, node["id"]),
            )
        event_type = "contamination.frozen" if change_state else "contamination.lock_placed"
        self.samples.append_event(
            node["id"], event_type, principal.user_id, now,
            from_state=node["lifecycle_state"],
            to_state="quarantined" if change_state else node["lifecycle_state"],
            details={"event_id": event_id, "event_code": event["event_code"]},
        )
        self.audit.record(
            principal, "contamination.freeze", "sample", str(node["id"]),
            before={"lifecycle_state": node["lifecycle_state"]},
            after={"lifecycle_state": "quarantined" if change_state else node["lifecycle_state"]},
            metadata={"event_id": event_id, "change_state": change_state},
        )
        return "locked"

    def _send_recall_notification(
        self,
        principal: Principal,
        event: dict[str, Any],
        loan: dict[str, Any],
        node: dict[str, Any],
        now: str,
    ) -> dict[str, Any]:
        event_id = event["id"]
        # 去重键保证重试或多轮重新计算不会对同一借用单重复通知
        dedup_key = f"recall:{event_id}:loan:{loan['id']}"
        existing = self.connection.execute(
            "SELECT * FROM recall_notifications WHERE dedup_key=?", (dedup_key,)
        ).fetchone()
        if existing:
            return dict(existing)
        outstanding = round(loan["quantity"] - loan["returned_quantity"], 9)
        recipient = loan["borrower_email"] or loan["borrower_username"]
        content = (
            f"【污染召回】事件 {event['event_code']}：{event['title']}。"
            f"您借用的样品 {node['sample_code']}（借用单 {loan['loan_code']}，尚有 {outstanding:g} 单位未归还，"
            f"应还时间 {loan['due_at']}）被判定受污染，请立即停止一切实验使用、单独封存并联系样品管理员归还。"
        )
        cursor = self.connection.execute(
            """INSERT INTO recall_notifications(
                   event_id,loan_id,borrower_user_id,channel,recipient,content,dedup_key,
                   status,sent_at,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?, 'sent', ?, ?, ?)""",
            (event_id, loan["id"], loan["borrower_user_id"], "in_system", recipient, content,
             dedup_key, now, now, now),
        )
        notification = dict(
            self.connection.execute("SELECT * FROM recall_notifications WHERE id=?", (cursor.lastrowid,)).fetchone()
        )
        self.samples.append_event(
            node["id"], "contamination.recall_issued", principal.user_id, now,
            details={"event_id": event_id, "loan_id": loan["id"], "notification_id": notification["id"]},
        )
        self.audit.record(
            principal, "contamination.recall.notify", "loan", str(loan["id"]),
            after={"notification_id": notification["id"], "borrower_user_id": loan["borrower_user_id"]},
            metadata={"event_id": event_id, "dedup_key": dedup_key},
        )
        return notification

    # --------------------------------------------------------- 处置动作落库

    def _upsert_action(
        self,
        event_id: int,
        object_type: str,
        object_id: int,
        sample_id: int,
        disposition: str,
        status: str,
        inclusion_reason: str,
        generation: int,
        now: str,
        *,
        detail: dict[str, Any],
        exclusion_reason: str | None = None,
    ) -> None:
        self.connection.execute(
            """INSERT INTO containment_actions(
                   event_id,lineage_generation,object_type,object_id,sample_id,disposition,status,
                   applied_at,detail_json,inclusion_reason,exclusion_reason,
                   first_seen_generation,last_seen_generation,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(event_id,object_type,object_id) DO UPDATE SET
                   lineage_generation=excluded.lineage_generation,
                   disposition=excluded.disposition,
                   status=CASE
                       WHEN excluded.status='excluded' THEN 'excluded'
                       WHEN containment_actions.status IN ('applied','skipped') THEN containment_actions.status
                       ELSE excluded.status END,
                   detail_json=excluded.detail_json,
                   inclusion_reason=excluded.inclusion_reason,
                   exclusion_reason=excluded.exclusion_reason,
                   last_seen_generation=excluded.last_seen_generation,
                   updated_at=excluded.updated_at""",
            (
                event_id, generation, object_type, object_id, sample_id, disposition, status,
                now if status == "applied" else None, json.dumps(detail, ensure_ascii=False, sort_keys=True),
                inclusion_reason, exclusion_reason, generation, generation, now, now,
            ),
        )

    def _mark_action_status(
        self, event_id: int, object_type: str, object_id: int, status: str, now: str
    ) -> None:
        self.connection.execute(
            """UPDATE containment_actions SET status=?,applied_at=COALESCE(applied_at,?),updated_at=?
               WHERE event_id=? AND object_type=? AND object_id=?""",
            (status, now if status == "applied" else None, now, event_id, object_type, object_id),
        )

    # ------------------------------------------------------------- 调查结论

    def record_investigation(self, principal: Principal, event_id: int, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("contamination.manage")
        event = self._get_event_row(event_id)
        if event["state"] not in {"containing", "contained"}:
            raise ConflictError("只有管控中的事件可以补充调查结论")
        if not data.get("conclusion", "").strip():
            raise ValidationError("调查结论不能为空")
        now = to_storage(self.clock.now())
        self.connection.execute(
            """UPDATE contamination_events SET investigation_conclusion=?,investigation_recorded_by=?,
                   investigation_recorded_at=?,updated_at=? WHERE id=?""",
            (data["conclusion"].strip(), principal.user_id, now, now, event_id),
        )
        self.audit.record(
            principal, "contamination.investigation.record", "contamination_event", str(event_id),
            after={"conclusion": data["conclusion"].strip()},
        )
        return self.detail(principal, event_id)

    # ----------------------------------------------------- 解除控制与审批

    def request_release(self, principal: Principal, event_id: int, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("contamination.manage")
        event = self._get_event_row(event_id)
        if event["state"] not in {"containing", "contained"}:
            raise ConflictError("事件已经结束，无需申请解除")
        if not event["investigation_conclusion"]:
            raise ConflictError("必须先记录调查结论，才能申请解除控制")
        existing = self.connection.execute(
            "SELECT * FROM approval_requests WHERE action_type='contamination_release' AND resource_id=? AND state='pending'",
            (event_id,),
        ).fetchone()
        if existing:
            raise ConflictError("该事件已有待审批的解除申请", context={"request_id": existing["id"]})
        now_dt = self.clock.now()
        request_code = data.get("request_code") or f"APR-CTM-{uuid.uuid4().hex[:10]}"
        payload = {
            "event_code": event["event_code"],
            "source_sample_id": event["source_sample_id"],
            "investigation_conclusion": event["investigation_conclusion"],
            "note": data.get("note", ""),
        }
        cursor = self.connection.execute(
            """INSERT INTO approval_requests(
                   request_code,action_type,resource_type,resource_id,requested_by,payload_json,
                   state,required_approvals,expires_at,created_at,updated_at
               ) VALUES(?, 'contamination_release','contamination_event',?,?,?, 'pending',2,?,?,?)""",
            (request_code, event_id, principal.user_id, json.dumps(payload, ensure_ascii=False),
             data.get("expires_at") or to_storage(now_dt + timedelta(days=3)), to_storage(now_dt), to_storage(now_dt)),
        )
        request = self._approval(cursor.lastrowid)
        self.audit.record(
            principal, "contamination.release.request", "approval_request", str(request["id"]),
            after={"request_code": request_code, "event_id": event_id},
        )
        return request

    def decide_release(self, principal: Principal, request_id: int, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("contamination.release")
        request = self._approval(request_id)
        if request["action_type"] != "contamination_release":
            raise ValidationError("审批请求不是污染解除类型")
        if request["state"] != "pending":
            raise ConflictError("审批请求已经结束")
        if request["requested_by"] == principal.user_id:
            raise ValidationError("申请人不能审批自己发起的解除申请")
        already = self.connection.execute(
            "SELECT id FROM approval_decisions WHERE request_id=? AND approver_user_id=?",
            (request_id, principal.user_id),
        ).fetchone()
        if already:
            raise ConflictError("同一审批人不能重复决定，解除控制需要不同审批人的独立意见")
        now = to_storage(self.clock.now())
        self.connection.execute(
            "INSERT INTO approval_decisions(request_id,approver_user_id,decision,comment,decided_at) VALUES(?,?,?,?,?)",
            (request_id, principal.user_id, data["decision"], data.get("comment", ""), now),
        )
        decisions = self.connection.execute(
            "SELECT decision FROM approval_decisions WHERE request_id=?", (request_id,)
        ).fetchall()
        if any(row[0] == "reject" for row in decisions):
            new_state = "rejected"
        elif len(decisions) >= request["required_approvals"]:
            new_state = "approved"
        else:
            new_state = "pending"
        self.connection.execute(
            "UPDATE approval_requests SET state=?,version=version+1,updated_at=? WHERE id=?",
            (new_state, now, request_id),
        )
        self.audit.record(
            principal, "contamination.release.decide", "approval_request", str(request_id),
            after={"decision": data["decision"], "state": new_state},
            metadata={"event_id": request["resource_id"]},
        )
        if new_state == "approved":
            self._execute_release(principal, request["resource_id"], request_id, now)
        return self._approval(request_id)

    def _execute_release(
        self, principal: Principal, event_id: int, approval_id: int, now: str
    ) -> None:
        event = self._get_event_row(event_id)
        if event["state"] not in {"containing", "contained"}:
            raise ConflictError("事件已经结束")
        if not event["investigation_conclusion"]:
            raise ConflictError("缺少调查结论，不能解除控制")
        locked = [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM samples WHERE contamination_lock=?", (event_id,)
            ).fetchall()
        ]
        for sample in locked:
            if sample["pre_freeze_state"]:
                target_state = sample["pre_freeze_state"]
            elif sample["reserved_quantity"] > 0:
                target_state = "loaned"
            elif sample["quantity"] <= 0:
                target_state = "consumed"
            else:
                target_state = "available"
            self.connection.execute(
                """UPDATE samples SET lifecycle_state=?,contamination_lock=NULL,pre_freeze_state=NULL,
                       version=version+1,updated_at=? WHERE id=?""",
                (target_state, now, sample["id"]),
            )
            self.samples.append_event(
                sample["id"], "contamination.released", principal.user_id, now,
                from_state="quarantined", to_state=target_state,
                details={"event_id": event_id, "approval_id": approval_id},
            )
        self.connection.execute(
            """UPDATE contamination_events SET state='released',release_approval_id=?,
                   released_by=?,released_at=?,updated_at=? WHERE id=?""",
            (approval_id, principal.user_id, now, now, event_id),
        )
        final_event = self._get_event_row(event_id)
        final_report = self._build_report(
            final_event, self._actions(event_id), self._notifications(event_id)
        )
        self.connection.execute(
            "UPDATE contamination_events SET report_json=?,report_generated_at=? WHERE id=?",
            (json.dumps(final_report, ensure_ascii=False, sort_keys=True), now, event_id),
        )
        self.audit.record(
            principal, "contamination.release.execute", "contamination_event", str(event_id),
            after={"approval_id": approval_id, "unfrozen_count": len(locked)},
        )

    # ----------------------------------------------------------------- 查询

    def list_events(self, principal: Principal, state: str | None = None) -> list[dict[str, Any]]:
        principal.require("samples.read")
        sql = """SELECT e.*,s.sample_code AS source_sample_code,u.display_name AS reported_by_name
                 FROM contamination_events e
                 JOIN samples s ON s.id=e.source_sample_id
                 JOIN users u ON u.id=e.reported_by"""
        params: list[Any] = []
        if state:
            sql += " WHERE e.state=?"
            params.append(state)
        sql += " ORDER BY e.id DESC"
        return [dict(row) for row in self.connection.execute(sql, tuple(params)).fetchall()]

    def detail(self, principal: Principal, event_id: int) -> dict[str, Any]:
        principal.require("samples.read")
        event = self._get_event_row(event_id)
        result = dict(event)
        result["actions"] = self._actions(event_id)
        result["notifications"] = self._notifications(event_id)
        result["lineage"] = {
            "generation": event["lineage_generation"],
            "signature": event["lineage_signature"],
            "recalculation_count": event["recalculation_count"],
            "last_change_detected_at": event["last_change_detected_at"],
            "nodes": self._descendants(event["source_sample_id"]),
        }
        result["report"] = self._build_report(event, result["actions"], result["notifications"])
        if event["report_json"]:
            result["report_snapshot"] = {
                "generated_at": event["report_generated_at"],
                "report": json.loads(event["report_json"]),
            }
        return result

    def _actions(self, event_id: int) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.connection.execute(
                """SELECT a.*,s.sample_code
                   FROM containment_actions a JOIN samples s ON s.id=a.sample_id
                   WHERE a.event_id=?
                   ORDER BY CASE a.object_type
                       WHEN 'sample' THEN 0 WHEN 'loan' THEN 1
                       WHEN 'consumption' THEN 2 WHEN 'destruction' THEN 3 END,
                       a.lineage_generation,a.id""",
                (event_id,),
            ).fetchall()
        ]

    def _notifications(self, event_id: int) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.connection.execute(
                """SELECT n.*,l.loan_code FROM recall_notifications n
                   JOIN loans l ON l.id=n.loan_id WHERE n.event_id=? ORDER BY n.id""",
                (event_id,),
            ).fetchall()
        ]

    def _build_report(
        self, event: dict[str, Any], actions: list[dict[str, Any]], notifications: list[dict[str, Any]]
    ) -> dict[str, Any]:
        included = [a for a in actions if a["disposition"] != "excluded"]
        excluded = [a for a in actions if a["disposition"] == "excluded"]
        counts: dict[str, int] = {}
        for action in included:
            counts[action["disposition"]] = counts.get(action["disposition"], 0) + 1
        objects = []
        for action in actions:
            objects.append(
                {
                    "object_type": action["object_type"],
                    "object_id": action["object_id"],
                    "sample_id": action["sample_id"],
                    "sample_code": action["sample_code"],
                    "disposition": action["disposition"],
                    "status": action["status"],
                    "included": action["disposition"] != "excluded",
                    "reason": action["exclusion_reason"] if action["disposition"] == "excluded" else action["inclusion_reason"],
                    "first_seen_generation": action["first_seen_generation"],
                    "last_seen_generation": action["last_seen_generation"],
                }
            )
        return {
            "title": f"污染事件 {event['event_code']} 影响追踪与召回报告",
            "state": event["state"],
            "source_sample_id": event["source_sample_id"],
            "lineage_generation": event["lineage_generation"],
            "lineage_signature": event["lineage_signature"][:12],
            "recalculation_count": event["recalculation_count"],
            "summary": {
                "included_total": len(included),
                "excluded_total": len(excluded),
                "frozen": counts.get("frozen", 0),
                "recalled": counts.get("recalled", 0),
                "irreversible": counts.get("irreversible", 0),
                "notifications_sent": len(notifications),
            },
            "investigation_conclusion": event["investigation_conclusion"],
            "released_at": event["released_at"],
            "objects": objects,
        }

    # ----------------------------------------------------------------- 辅助

    def _get_event_row(self, event_id: int) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM contamination_events WHERE id=?", (event_id,)
        ).fetchone()
        if not row:
            raise NotFoundError("污染事件不存在")
        return dict(row)

    def _approval(self, request_id: int) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM approval_requests WHERE id=?", (request_id,)
        ).fetchone()
        if not row:
            raise NotFoundError("审批请求不存在")
        result = dict(row)
        result["decisions"] = [
            dict(item)
            for item in self.connection.execute(
                "SELECT * FROM approval_decisions WHERE request_id=? ORDER BY id", (request_id,)
            ).fetchall()
        ]
        return result
