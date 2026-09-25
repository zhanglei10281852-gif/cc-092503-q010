from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import datetime
from typing import Any

from app.core.clock import Clock, SystemClock, from_storage
from app.core.errors import NotFoundError
from app.core.security import Principal


class BatchReconciliationService:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def detail(self, principal: Principal, batch_id: int) -> dict[str, Any]:
        principal.require("samples.read")
        batch = self.connection.execute(
            "SELECT * FROM receipt_batches WHERE id=?", (batch_id,)
        ).fetchone()
        if not batch:
            raise NotFoundError("接收批次不存在")
        samples = [
            dict(row)
            for row in self.connection.execute(
                """SELECT id,sample_code,sample_type,quantity,reserved_quantity,unit,
                          lifecycle_state,location_id,parent_sample_id,root_sample_id
                   FROM samples WHERE batch_id=? ORDER BY sample_code""",
                (batch_id,),
            ).fetchall()
        ]
        by_state: dict[str, int] = defaultdict(int)
        by_type: dict[str, int] = defaultdict(int)
        quantities: dict[str, float] = defaultdict(float)
        unlocated = []
        for sample in samples:
            by_state[sample["lifecycle_state"]] += 1
            by_type[sample["sample_type"]] += 1
            quantities[sample["unit"]] += float(sample["quantity"])
            if sample["location_id"] is None and sample["lifecycle_state"] not in {
                "loaned", "consumed", "destroyed"
            }:
                unlocated.append(sample["sample_code"])
        accepted = len(samples)
        expected = int(batch["expected_count"])
        return {
            "batch": dict(batch),
            "sample_count": accepted,
            "count_delta": accepted - expected,
            "is_count_reconciled": accepted + int(batch["rejected_count"]) == expected,
            "by_state": dict(sorted(by_state.items())),
            "by_type": dict(sorted(by_type.items())),
            "quantity_by_unit": dict(sorted(quantities.items())),
            "unlocated_sample_codes": unlocated,
            "samples": samples,
        }

    def open_batches(self, principal: Principal) -> list[dict[str, Any]]:
        principal.require("samples.read")
        rows = self.connection.execute(
            """SELECT b.*,
                      COUNT(s.id) AS actual_count,
                      SUM(CASE WHEN s.location_id IS NULL THEN 1 ELSE 0 END) AS unlocated_count
               FROM receipt_batches b LEFT JOIN samples s ON s.batch_id=b.id
               WHERE b.status IN ('open','reconciled','quarantined')
               GROUP BY b.id ORDER BY b.received_at,b.id"""
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["count_delta"] = int(item["actual_count"]) + int(item["rejected_count"]) - int(item["expected_count"])
            result.append(item)
        return result


class ExceptionAgingService:
    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None):
        self.connection = connection
        self.clock = clock or SystemClock()

    def summary(self, principal: Principal) -> dict[str, Any]:
        principal.require("samples.read")
        now = self.clock.now()
        cases = [
            dict(row)
            for row in self.connection.execute(
                """SELECT * FROM anomaly_cases
                   WHERE state NOT IN ('resolved','dismissed') ORDER BY created_at,id"""
            ).fetchall()
        ]
        overdue_loans = [
            dict(row)
            for row in self.connection.execute(
                """SELECT l.*,s.sample_code,u.display_name AS borrower_name
                   FROM loans l JOIN samples s ON s.id=l.sample_id
                   JOIN users u ON u.id=l.borrower_user_id
                   WHERE l.state IN ('active','partially_returned','overdue') AND l.due_at<?
                   ORDER BY l.due_at""",
                (now.isoformat(),),
            ).fetchall()
        ]
        aging = {"0-1d": 0, "2-3d": 0, "4-7d": 0, "8d+": 0}
        critical = []
        for case in cases:
            created = from_storage(case["created_at"])
            days = max(0, int((now - created).total_seconds() // 86400))
            if days <= 1:
                bucket = "0-1d"
            elif days <= 3:
                bucket = "2-3d"
            elif days <= 7:
                bucket = "4-7d"
            else:
                bucket = "8d+"
            aging[bucket] += 1
            case["age_days"] = days
            if case["severity"] == "critical":
                critical.append(case)
        return {
            "open_anomaly_count": len(cases),
            "anomaly_aging": aging,
            "critical_anomalies": critical,
            "overdue_loan_count": len(overdue_loans),
            "overdue_loans": overdue_loans,
        }
