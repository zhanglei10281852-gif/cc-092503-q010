from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from typing import Any

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.security import Principal
from app.samples.repository import SampleRepository
from app.services.audit import AuditService

OPEN_LOAN_STATES = ("active", "partially_returned", "overdue", "disputed")
# quarantined 也允许被污染控制接管（例如此前因异常已隔离的样品），
# 解除时会按冻结事件记录的原状态还原。
IN_STOCK_FREEZABLE_STATES = ("available", "partially_consumed", "pending_destruction", "quarantined")

_CHANNEL_BORROWER = "borrower"


class ContaminationRecallService:
    """污染影响追踪与召回。

    一次污染事件（contamination_events）对应一组可重算的召回批次（recall_runs）：
    每个批次按当时的谱系快照计算受影响后代，冻结在库样品、召回在借样品、
    登记不可撤销影响，并为每个对象记录纳入或排除理由。
    """

    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None):
        self.connection = connection
        self.clock = clock or SystemClock()
        self.audit = AuditService(connection, self.clock)

    # ------------------------------------------------------------------ 开立

    def open_case(self, principal: Principal, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("recalls.manage")
        sample = self._get_sample(data["source_sample_id"])
        event = self._get_event(data["source_sample_id"], data["source_event_id"])
        if data.get("idempotency_key"):
            existing = self.connection.execute(
                "SELECT * FROM contamination_events WHERE idempotency_key=?",
                (data["idempotency_key"],),
            ).fetchone()
            if existing:
                case = self.get_case(principal, existing["id"])
                return {**case, "replayed": True}
        active = self.connection.execute(
            "SELECT id FROM contamination_events WHERE source_sample_id=? AND state!='released'",
            (sample["id"],),
        ).fetchone()
        if active:
            raise ConflictError("该样品已有未解除的污染控制事件", context={"case_id": active["id"]})
        now = to_storage(self.clock.now())
        case_code = data.get("case_code") or f"CTM-{uuid.uuid4().hex[:12]}"
        cursor = self.connection.execute(
            """INSERT INTO contamination_events(
                   case_code,source_sample_id,source_event_id,source_sample_version,idempotency_key,
                   contaminant_label,description,state,opened_by,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,'controlled',?,?,?)""",
            (
                case_code, sample["id"], event["id"], sample["version"], data.get("idempotency_key"),
                data["contaminant_label"], data.get("description", ""), principal.user_id, now, now,
            ),
        )
        case_id = cursor.lastrowid
        run = self._execute_run(case_id, principal, trigger="open", now=now)
        self.audit.record(
            principal, "recall.open", "contamination_event", str(case_id),
            after={"case_code": case_code, "run_no": run["run_no"]},
            metadata={"source_sample_id": sample["id"], "source_event_id": event["id"]},
        )
        return {**self.get_case(principal, case_id), "replayed": False}

    # ------------------------------------------------------------------ 重算

    def recalculate(self, principal: Principal, case_id: int) -> dict[str, Any]:
        principal.require("recalls.manage")
        case = self._load_case_row(case_id)
        if case["state"] == "released":
            raise ConflictError("污染事件已解除控制，不能重新计算")
        latest = self._latest_run(case_id)
        signature = self._lineage_signature(case)
        if latest is not None and latest["lineage_signature"] == signature:
            return {**self.get_case(principal, case_id), "changed": False, "run_no": latest["run_no"]}
        now = to_storage(self.clock.now())
        run = self._execute_run(case_id, principal, trigger="recalculate", now=now)
        self.audit.record(
            principal, "recall.recalculate", "contamination_event", str(case_id),
            metadata={"run_no": run["run_no"], "lineage_changed": run["lineage_changed"]},
        )
        return {**self.get_case(principal, case_id), "changed": True, "run_no": run["run_no"]}

    # ------------------------------------------------------------------ 查询

    def list_cases(self, principal: Principal, state: str | None = None) -> list[dict[str, Any]]:
        principal.require("samples.read")
        if state:
            rows = self.connection.execute(
                """SELECT c.*,s.sample_code
                   FROM contamination_events c JOIN samples s ON s.id=c.source_sample_id
                   WHERE c.state=? ORDER BY c.id DESC""",
                (state,),
            ).fetchall()
        else:
            rows = self.connection.execute(
                """SELECT c.*,s.sample_code
                   FROM contamination_events c JOIN samples s ON s.id=c.source_sample_id
                   ORDER BY c.id DESC"""
            ).fetchall()
        return [dict(row) for row in rows]

    def get_case(self, principal: Principal, case_id: int) -> dict[str, Any]:
        principal.require("samples.read")
        case = self._load_case_row(case_id)
        result = dict(case)
        result["latest_run"] = self._present_run(case_id, result["last_run_no"])
        result["items"] = self._list_items(case_id)
        result["counters"] = self._summarize_items(result["items"])
        result["notifications"] = self._list_notifications(case_id)
        return result

    def get_report(self, principal: Principal, case_id: int, run_no: int | None = None) -> dict[str, Any]:
        principal.require("samples.read")
        self._load_case_row(case_id)
        if run_no is None:
            run = self._latest_run(case_id)
            if run is None:
                raise NotFoundError("该污染事件尚无召回批次")
        else:
            run = self.connection.execute(
                "SELECT * FROM recall_runs WHERE case_id=? AND run_no=?", (case_id, run_no),
            ).fetchone()
            if run is None:
                raise NotFoundError("召回批次不存在")
            run = dict(run)
        return self._present_run_row(run)

    # ------------------------------------------------------------------ 解除

    def request_release(self, principal: Principal, case_id: int, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("recalls.manage")
        case = self._load_case_row(case_id)
        conclusion = (data.get("conclusion") or "").strip()
        if len(conclusion) < 10:
            raise ValidationError("调查结论至少填写 10 个字符，说明污染性质与解除依据")
        if case["state"] == "released":
            raise ConflictError("污染事件已解除控制")
        pending = self.connection.execute(
            "SELECT * FROM recall_release_requests WHERE case_id=? AND state='pending'",
            (case_id,),
        ).fetchone()
        if pending:
            raise ConflictError("已有待审批的解除申请", context={"request_id": pending["id"]})
        now = to_storage(self.clock.now())
        cursor = self.connection.execute(
            """INSERT INTO recall_release_requests(
                   case_id,requested_by,conclusion,state,created_at,updated_at
               ) VALUES(?, ?, ?, 'pending', ?, ?)""",
            (case_id, principal.user_id, conclusion, now, now),
        )
        request_id = cursor.lastrowid
        self.connection.execute(
            "UPDATE contamination_events SET state='release_pending',investigation_conclusion=?,"
            "release_request_id=?,updated_at=? WHERE id=?",
            (conclusion, request_id, now, case_id),
        )
        request = self._load_release_request(request_id)
        self.audit.record(
            principal, "recall.release_request", "recall_release_request", str(request_id),
            after=request, metadata={"case_id": case_id},
        )
        return request

    def decide_release(self, principal: Principal, request_id: int, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("recalls.approve")
        row = self.connection.execute(
            "SELECT * FROM recall_release_requests WHERE id=?", (request_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError("解除申请不存在")
        request = dict(row)
        if request["state"] != "pending":
            raise ConflictError("解除申请已经结束审批")
        if request["requested_by"] == principal.user_id:
            raise ValidationError("解除控制必须由申请人之外的独立审批人批准")
        now = to_storage(self.clock.now())
        comment = (data.get("comment") or "").strip()
        if data["decision"] == "reject":
            self.connection.execute(
                "UPDATE recall_release_requests SET state='rejected',decided_by=?,decision_comment=?,"
                "decided_at=?,updated_at=? WHERE id=?",
                (principal.user_id, comment, now, now, request_id),
            )
            self.connection.execute(
                "UPDATE contamination_events SET state='controlled',updated_at=? WHERE id=?",
                (now, request["case_id"]),
            )
            result = self._load_release_request(request_id)
            self.audit.record(
                principal, "recall.release_rejected", "recall_release_request", str(request_id),
                after=result, metadata={"case_id": request["case_id"]},
            )
            return result
        outstanding = self.connection.execute(
            "SELECT id,loan_code FROM loans WHERE recall_case_id=? AND state IN (?,?,?,?)",
            (request["case_id"], *OPEN_LOAN_STATES),
        ).fetchall()
        if outstanding:
            raise ConflictError(
                "仍有召回中的借用未归还，不能解除控制",
                context={"loan_codes": [row["loan_code"] for row in outstanding]},
            )
        self._lift_containment(request["case_id"], principal, now)
        self.connection.execute(
            "UPDATE recall_release_requests SET state='approved',decided_by=?,decision_comment=?,"
            "decided_at=?,updated_at=? WHERE id=?",
            (principal.user_id, comment, now, now, request_id),
        )
        self.connection.execute(
            "UPDATE contamination_events SET state='released',released_by=?,released_at=?,updated_at=? WHERE id=?",
            (principal.user_id, now, now, request["case_id"]),
        )
        result = self._load_release_request(request_id)
        self.audit.record(
            principal, "recall.release_approved", "recall_release_request", str(request_id),
            after=result, metadata={"case_id": request["case_id"]},
        )
        return result

    # ------------------------------------------------------------ 核心计算

    def _execute_run(self, case_id: int, principal: Principal, *, trigger: str, now: str) -> dict[str, Any]:
        case = self._load_case_row(case_id)
        nodes = self._lineage_nodes(case["source_sample_id"])
        baseline_ids = {
            node["sample_id"]
            for node in nodes
            if node["created_event_id"] is None or node["created_event_id"] <= case["source_event_id"]
        }
        signature = self._signature_for(nodes, case_id)
        previous = self._latest_run(case_id)
        lineage_changed = previous is not None and previous["lineage_signature"] != signature

        run_no = 1 if previous is None else previous["run_no"] + 1
        cursor = self.connection.execute(
            """INSERT INTO recall_runs(
                   case_id,run_no,trigger,lineage_signature,lineage_changed,
                   affected_sample_count,frozen_count,loan_recall_count,irreversible_count,
                   excluded_count,notification_count,report_json,created_by,created_at
               ) VALUES(?,?,?,?,?,0,0,0,0,0,0,'{}',?,?)""",
            (case_id, run_no, trigger, signature, int(lineage_changed), principal.user_id, now),
        )
        run_id = cursor.lastrowid

        items: list[dict[str, Any]] = []
        notification_count = 0
        frozen_count = 0
        loan_recall_count = 0
        irreversible_count = 0
        excluded_count = 0
        notified_loan_ids = {
            row["object_id"]
            for row in self.connection.execute(
                "SELECT object_id FROM recall_notifications WHERE case_id=? AND object_type='loan'",
                (case_id,),
            ).fetchall()
        }

        for node in nodes:
            sample = self._get_sample(node["sample_id"])
            new_after_baseline = sample["id"] not in baseline_ids
            related_loans = [
                dict(row)
                for row in self.connection.execute(
                    "SELECT * FROM loans WHERE sample_id=? ORDER BY id", (sample["id"],)
                ).fetchall()
            ]
            open_loans = [loan for loan in related_loans if loan["state"] in OPEN_LOAN_STATES]
            closed_loans = [loan for loan in related_loans if loan["state"] not in OPEN_LOAN_STATES]
            consumptions = [
                dict(row)
                for row in self.connection.execute(
                    "SELECT * FROM consumption_records WHERE sample_id=? ORDER BY id", (sample["id"],)
                ).fetchall()
            ]
            destructions = [
                dict(row)
                for row in self.connection.execute(
                    "SELECT * FROM destruction_records WHERE sample_id=? ORDER BY id", (sample["id"],)
                ).fetchall()
            ]

            sample_classification = self._classify_sample(sample, bool(open_loans))
            lineage_note = "污染确认事件后新增分装，经版本变化重算纳入；" if new_after_baseline else ""
            if sample_classification == "frozen":
                was_frozen = self._freeze_sample(sample["id"], case_id, now)
                if was_frozen:
                    SampleRepository(self.connection).append_event(
                        sample["id"], "containment.frozen", principal.user_id, now,
                        from_state=sample["lifecycle_state"], to_state="quarantined",
                        details={"case_id": case_id, "run_no": run_no, "new_after_baseline": new_after_baseline},
                    )
                frozen_count += 1
                items.append(self._upsert_item(
                    case_id, run_id, "sample", sample["id"], included=True,
                    category="sample.frozen", action="frozen",
                    reason=lineage_note + "样品仍在库且可动用，已置为隔离冻结（quarantined），停止分装、借出、转移与消耗",
                    detail={"sample_code": sample["sample_code"], "quantity": sample["quantity"],
                            "unit": sample["unit"], "new_after_baseline": new_after_baseline,
                            "previously_controlled": not was_frozen},
                    now=now,
                ))
            elif sample_classification == "loan_recalled":
                newly_flagged = sample["control_case_id"] is None
                self._flag_sample_controlled(sample["id"], case_id, now)
                if newly_flagged:
                    SampleRepository(self.connection).append_event(
                        sample["id"], "containment.flagged", principal.user_id, now,
                        details={"case_id": case_id, "run_no": run_no, "reason": "样品在借，随借用记录召回"},
                    )
                items.append(self._upsert_item(
                    case_id, run_id, "sample", sample["id"], included=True,
                    category="sample.on_loan", action="flagged",
                    reason=lineage_note + "样品实物在借，无法直接入库冻结，已标记控制并随借用记录一并召回",
                    detail={"sample_code": sample["sample_code"], "quantity": sample["quantity"],
                            "unit": sample["unit"], "new_after_baseline": new_after_baseline},
                    now=now,
                ))
            else:
                self._flag_sample_controlled(sample["id"], case_id, now)
                irreversible_count += 1
                reason = (
                    "样品已全部消耗于实验，实物不可召回，仅能追溯实验影响"
                    if sample["lifecycle_state"] == "consumed"
                    else "样品已销毁，实物不可召回，仅能登记销毁凭证与影响"
                )
                items.append(self._upsert_item(
                    case_id, run_id, "sample", sample["id"], included=True,
                    category=f"sample.irreversible_{sample_classification}", action="irreversible_listed",
                    reason=lineage_note + reason,
                    detail={"sample_code": sample["sample_code"],
                            "lifecycle_state": sample["lifecycle_state"],
                            "new_after_baseline": new_after_baseline},
                    now=now,
                ))

            for loan in open_loans:
                borrower = dict(self.connection.execute(
                    "SELECT id,display_name,username FROM users WHERE id=?",
                    (loan["borrower_user_id"],),
                ).fetchone())
                already_flagged = loan["recall_case_id"] is not None
                if not already_flagged:
                    self.connection.execute(
                        "UPDATE loans SET recall_case_id=?,recall_loan_version=version,version=version+1,updated_at=? WHERE id=?",
                        (case_id, now, loan["id"]),
                    )
                subject = f"污染召回通知：借用 {loan['loan_code']}（{sample['sample_code']}）请立即归还"
                body = (
                    f"污染事件 {case['case_code']} 确认样品 {sample['sample_code']} 受污染，"
                    f"借用记录 {loan['loan_code']} 名下尚未归还数量 "
                    f"{loan['quantity'] - loan['returned_quantity']} {sample['unit']}，"
                    "请立即停止使用并归还样品库；已开展实验请登记反馈。"
                )
                sent = self._send_notification(
                    case_id, run_id,
                    channel=_CHANNEL_BORROWER,
                    target_key=f"loan:{loan['id']}",
                    recipient_user_id=borrower["id"],
                    object_type="loan", object_id=loan["id"],
                    subject=subject, body=body, now=now,
                )
                if sent:
                    notification_count += 1
                if not already_flagged:
                    self.connection.execute(
                        "UPDATE loans SET recall_notified_at=? WHERE id=?", (now, loan["id"])
                    )
                self._mark_item_notified(case_id, "loan", loan["id"], now)
                loan_recall_count += 1
                items.append(self._upsert_item(
                    case_id, run_id, "loan", loan["id"], included=True,
                    category="loan.recall", action="recall_notified" if sent else "recall_already_notified",
                    reason=("在借未结清，已标记召回并向借用人发送通知" if sent
                            else "在借未结清；通知已在此前批次发送，按去重规则不重复通知")
                    + (f"；借用人 {borrower['display_name']}"),
                    detail={"loan_code": loan["loan_code"], "state": loan["state"],
                            "borrower_user_id": borrower["id"],
                            "outstanding_quantity": loan["quantity"] - loan["returned_quantity"],
                            "new_after_baseline": new_after_baseline},
                    notified=True, now=now,
                ))

            for loan in closed_loans:
                if loan["id"] in notified_loan_ids:
                    items.append(self._upsert_item(
                        case_id, run_id, "loan", loan["id"], included=True,
                        category="loan.returned_after_recall", action="returned_quarantined",
                        reason="该借用曾因本污染事件被召回并已归还，归还入库的样品继续按隔离冻结管控，不重复通知",
                        detail={"loan_code": loan["loan_code"], "state": loan["state"],
                                "recall_notified_at": loan["recall_notified_at"]},
                        notified=True, now=now,
                    ))
                else:
                    excluded_count += 1
                    items.append(self._upsert_item(
                        case_id, run_id, "loan", loan["id"], included=False,
                        category="loan.closed", action="none",
                        reason="借用记录在召回执行前已归还结清，不发送召回通知；归还后的样品按在库状态另行冻结或核查",
                        detail={"loan_code": loan["loan_code"], "state": loan["state"]},
                        now=now,
                    ))

            for record in consumptions:
                irreversible_count += 1
                items.append(self._upsert_item(
                    case_id, run_id, "consumption_record", record["id"], included=True,
                    category="consumption.irreversible", action="irreversible_listed",
                    reason=f"样品已于实验 {record['experiment_code']} 中消耗 {record['quantity']} {sample['unit']}，"
                           "实验过程不可撤销，纳入不可撤销影响清单并提示结果复核",
                    detail={"sample_code": sample["sample_code"],
                            "experiment_code": record["experiment_code"],
                            "quantity": record["quantity"],
                            "occurred_at": record["occurred_at"]},
                    now=now,
                ))

            for record in destructions:
                irreversible_count += 1
                items.append(self._upsert_item(
                    case_id, run_id, "destruction_record", record["id"], included=True,
                    category="destruction.irreversible", action="irreversible_listed",
                    reason="样品已按销毁凭证销毁，处置不可撤销，纳入不可撤销影响清单",
                    detail={"sample_code": sample["sample_code"],
                            "method": record["method"],
                            "destroyed_quantity": record["destroyed_quantity"],
                            "certificate_digest": record["certificate_digest"],
                            "destroyed_at": record["destroyed_at"]},
                    now=now,
                ))

        affected_sample_count = len(nodes)
        report = {
            "case_id": case_id,
            "case_code": case["case_code"],
            "run_no": run_no,
            "trigger": trigger,
            "lineage_signature": signature,
            "lineage_changed": bool(lineage_changed),
            "source_sample_id": case["source_sample_id"],
            "source_event_id": case["source_event_id"],
            "generated_at": now,
            "items": [self._public_item(item) for item in items],
        }
        self.connection.execute(
            """UPDATE recall_runs SET affected_sample_count=?,frozen_count=?,loan_recall_count=?,
               irreversible_count=?,excluded_count=?,notification_count=?,report_json=? WHERE id=?""",
            (
                affected_sample_count, frozen_count, loan_recall_count, irreversible_count,
                excluded_count, notification_count,
                json.dumps(report, ensure_ascii=False, sort_keys=True), run_id,
            ),
        )
        return self._present_run_row(dict(self.connection.execute(
            "SELECT * FROM recall_runs WHERE id=?", (run_id,)
        ).fetchone()))

    # ------------------------------------------------------------ 控制/解除

    def _freeze_sample(self, sample_id: int, case_id: int, now: str) -> bool:
        """返回 True 表示本次调用真正改变了样品状态（用于只发一次事件）。"""
        cursor = self.connection.execute(
            """UPDATE samples SET lifecycle_state='quarantined',control_case_id=?,
                  version=version+1,updated_at=?
               WHERE id=? AND control_case_id IS NULL
                 AND lifecycle_state IN (?,?,?,?)""",
            (case_id, now, sample_id, *IN_STOCK_FREEZABLE_STATES),
        )
        if cursor.rowcount == 1:
            return True
        # 已被本案例控制或状态异常：至少补上控制标记。
        self._flag_sample_controlled(sample_id, case_id, now)
        return False

    def _flag_sample_controlled(self, sample_id: int, case_id: int, now: str) -> None:
        self.connection.execute(
            "UPDATE samples SET control_case_id=?,updated_at=? WHERE id=? AND control_case_id IS NULL",
            (case_id, now, sample_id),
        )

    def _lift_containment(self, case_id: int, principal: Principal, now: str) -> None:
        controlled = [
            dict(row)
            for row in self.connection.execute(
                "SELECT id,lifecycle_state,quantity FROM samples WHERE control_case_id=?",
                (case_id,),
            ).fetchall()
        ]
        for sample in controlled:
            if sample["lifecycle_state"] == "quarantined":
                target_state = self._restore_state(sample["id"], sample["quantity"])
                self.connection.execute(
                    "UPDATE samples SET lifecycle_state=?,control_case_id=NULL,version=version+1,updated_at=? WHERE id=?",
                    (target_state, now, sample["id"]),
                )
                SampleRepository(self.connection).append_event(
                    sample["id"], "containment.released", principal.user_id, now,
                    from_state="quarantined", to_state=target_state,
                    details={"case_id": case_id},
                )
            else:
                self.connection.execute(
                    "UPDATE samples SET control_case_id=NULL,updated_at=? WHERE id=?",
                    (now, sample["id"]),
                )
        self.connection.execute(
            "UPDATE loans SET recall_case_id=NULL,version=version+1,updated_at=? WHERE recall_case_id=?",
            (now, case_id),
        )

    def _restore_state(self, sample_id: int, quantity: float) -> str:
        if quantity == 0:
            return "consumed"
        frozen_event = self.connection.execute(
            "SELECT from_state FROM sample_events WHERE sample_id=? AND event_type='containment.frozen' "
            "ORDER BY id DESC LIMIT 1",
            (sample_id,),
        ).fetchone()
        prior = frozen_event["from_state"] if frozen_event and frozen_event["from_state"] else None
        if prior in IN_STOCK_FREEZABLE_STATES:
            return prior
        if self.connection.execute(
            "SELECT 1 FROM consumption_records WHERE sample_id=? LIMIT 1", (sample_id,)
        ).fetchone():
            return "partially_consumed"
        return "available"

    # ------------------------------------------------------------ 通知/条目

    def _send_notification(
        self, case_id: int, run_id: int, *, channel: str, target_key: str,
        recipient_user_id: int, object_type: str, object_id: int,
        subject: str, body: str, now: str,
    ) -> bool:
        cursor = self.connection.execute(
            """INSERT OR IGNORE INTO recall_notifications(
                   case_id,channel,target_key,recipient_user_id,object_type,object_id,
                   subject,body,run_id,sent_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (case_id, channel, target_key, recipient_user_id, object_type, object_id,
             subject, body, run_id, now),
        )
        return cursor.rowcount == 1

    def _upsert_item(
        self, case_id: int, run_id: int, object_type: str, object_id: int, *,
        included: bool, category: str, action: str, reason: str,
        detail: dict[str, Any], now: str, notified: bool = False,
    ) -> dict[str, Any]:
        payload = json.dumps(detail, ensure_ascii=False, sort_keys=True)
        notified_at = now if notified else None
        self.connection.execute(
            """INSERT INTO recall_items(
                   case_id,object_type,object_id,included,category,action,reason,detail_json,
                   first_seen_run_id,last_run_id,notified_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(case_id,object_type,object_id) DO UPDATE SET
                   included=excluded.included,
                   category=excluded.category,
                   action=excluded.action,
                   reason=excluded.reason,
                   detail_json=excluded.detail_json,
                   last_run_id=excluded.last_run_id,
                   notified_at=COALESCE(excluded.notified_at, recall_items.notified_at)""",
            (
                case_id, object_type, object_id, int(included), category, action, reason,
                payload, run_id, run_id, notified_at,
            ),
        )
        row = self.connection.execute(
            "SELECT * FROM recall_items WHERE case_id=? AND object_type=? AND object_id=?",
            (case_id, object_type, object_id),
        ).fetchone()
        return self._public_item(dict(row))

    def _mark_item_notified(self, case_id: int, object_type: str, object_id: int, now: str) -> None:
        self.connection.execute(
            "UPDATE recall_items SET notified_at=COALESCE(notified_at,?) WHERE case_id=? AND object_type=? AND object_id=?",
            (now, case_id, object_type, object_id),
        )

    # ------------------------------------------------------------ 谱系/签名

    def _lineage_nodes(self, source_sample_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """WITH RECURSIVE descendants(sample_id, created_event_id) AS (
                   SELECT id, NULL FROM samples WHERE id=?
                   UNION ALL
                   SELECT s.id,
                          (SELECT MIN(se.id) FROM sample_events se
                            WHERE se.sample_id=s.id AND se.event_type='aliquot.created')
                     FROM samples s JOIN descendants d ON s.parent_sample_id=d.sample_id
               )
               SELECT * FROM descendants ORDER BY sample_id""",
            (source_sample_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def _lineage_signature(self, case: dict[str, Any]) -> str:
        return self._signature_for(self._lineage_nodes(case["source_sample_id"]), case["id"])

    def _signature_for(self, nodes: list[dict[str, Any]], case_id: int) -> str:
        sample_ids = [node["sample_id"] for node in nodes]
        # 结构边只取父子关系：新增分装必然新增一条边；样品 version 会被冻结/释放
        # 本身改动，不能进入签名，否则控制动作会被误判为“谱系变化”。
        edges = [
            [row["parent_sample_id"] or 0, row["id"]]
            for row in self.connection.execute(
                f"SELECT id,parent_sample_id FROM samples WHERE id IN ({','.join('?' for _ in sample_ids)}) ORDER BY id",
                sample_ids,
            ).fetchall()
        ] if sample_ids else []
        loans = [
            (row["sample_id"], row["id"], row["state"], row["returned_quantity"])
            for row in self.connection.execute(
                f"SELECT sample_id,id,state,returned_quantity FROM loans WHERE sample_id IN ({','.join('?' for _ in sample_ids)}) ORDER BY id",
                sample_ids,
            ).fetchall()
        ] if sample_ids else []
        consumptions = [
            (row["sample_id"], row["id"])
            for row in self.connection.execute(
                f"SELECT sample_id,id FROM consumption_records WHERE sample_id IN ({','.join('?' for _ in sample_ids)}) ORDER BY id",
                sample_ids,
            ).fetchall()
        ] if sample_ids else []
        max_event = self.connection.execute(
            f"SELECT COALESCE(MAX(id),0) FROM sample_events WHERE sample_id IN "
            f"({','.join('?' for _ in sample_ids)}) AND event_type NOT LIKE 'containment.%'",
            sample_ids,
        ).fetchone()[0] if sample_ids else 0
        material = json.dumps(
            {"case_id": case_id, "edges": sorted(edges), "loans": loans,
             "consumptions": consumptions, "max_event": max_event},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    @staticmethod
    def _classify_sample(sample: dict[str, Any], has_open_loan: bool) -> str:
        if sample["lifecycle_state"] == "destroyed":
            return "destroyed"
        if sample["lifecycle_state"] == "consumed" or sample["quantity"] == 0:
            return "consumed"
        if has_open_loan or sample["lifecycle_state"] == "loaned":
            return "loan_recalled"
        return "frozen"

    # ------------------------------------------------------------ 辅助

    def _get_sample(self, sample_id: int) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM samples WHERE id=?", (sample_id,)).fetchone()
        if row is None:
            raise NotFoundError("样品不存在")
        return dict(row)

    def _get_event(self, sample_id: int, event_id: int) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM sample_events WHERE id=? AND sample_id=?", (event_id, sample_id),
        ).fetchone()
        if row is None:
            raise NotFoundError("指定的样品事件不存在或不属于该样品")
        return dict(row)

    def _load_case_row(self, case_id: int) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM contamination_events WHERE id=?", (case_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError("污染事件不存在")
        case = dict(row)
        latest = self.connection.execute(
            "SELECT MAX(run_no) AS last_run_no FROM recall_runs WHERE case_id=?", (case_id,),
        ).fetchone()
        case["last_run_no"] = latest["last_run_no"]
        return case

    def _latest_run(self, case_id: int) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM recall_runs WHERE case_id=? ORDER BY run_no DESC LIMIT 1", (case_id,),
        ).fetchone()
        return dict(row) if row else None

    def _present_run(self, case_id: int, run_no: int | None) -> dict[str, Any] | None:
        if run_no is None:
            return None
        row = self.connection.execute(
            "SELECT * FROM recall_runs WHERE case_id=? AND run_no=?", (case_id, run_no),
        ).fetchone()
        return self._present_run_row(dict(row)) if row else None

    def _present_run_row(self, run: dict[str, Any]) -> dict[str, Any]:
        result = dict(run)
        result["lineage_changed"] = bool(result["lineage_changed"])
        result["report"] = json.loads(result.pop("report_json") or "{}")
        return result

    def _list_items(self, case_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM recall_items WHERE case_id=? ORDER BY object_type,object_id",
            (case_id,),
        ).fetchall()
        return [self._public_item(dict(row)) for row in rows]

    def _list_notifications(self, case_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT id,channel,target_key,recipient_user_id,object_type,object_id,subject,body,run_id,sent_at "
            "FROM recall_notifications WHERE case_id=? ORDER BY id",
            (case_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _public_item(row: dict[str, Any]) -> dict[str, Any]:
        item = dict(row)
        item["included"] = bool(item["included"])
        detail = item.pop("detail_json", None)
        if isinstance(detail, str):
            item["detail"] = json.loads(detail or "{}")
        return item

    @staticmethod
    def _summarize_items(items: list[dict[str, Any]]) -> dict[str, int]:
        included = sum(1 for item in items if item["included"])
        return {
            "total": len(items),
            "included": included,
            "excluded": len(items) - included,
        }

    def _load_release_request(self, request_id: int) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM recall_release_requests WHERE id=?", (request_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError("解除申请不存在")
        return dict(row)


def assert_sample_not_controlled(sample: dict[str, Any]) -> None:
    """供分装/消耗/转移/借用/销毁等作业复用的控制态拦截。"""
    if sample.get("control_case_id"):
        raise ConflictError(
            "样品处于污染控制冻结中，相关作业已暂停",
            context={"control_case_id": sample["control_case_id"]},
        )
