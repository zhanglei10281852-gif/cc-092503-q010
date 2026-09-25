from __future__ import annotations

from app.core.clock import to_storage, utc_now
from app.database import get_connection


def _make_approver(client, admin):
    response = client.post(
        "/api/users",
        headers=admin["headers"],
        json={
            "username": "approver1",
            "password": "Approver!23",
            "display_name": "独立审批人甲",
            "role_codes": ["approver"],
        },
    )
    assert response.status_code == 201, response.text
    login = client.post(
        "/api/auth/login",
        json={"username": "approver1", "password": "Approver!23", "client_label": "tests"},
    )
    assert login.status_code == 200, login.text
    body = login.json()
    return {"headers": {"Authorization": f"Bearer {body['token']}"}, "body": body, "id": body["user"]["id"]}


def _build_contaminated_tree(client, admin, approver):
    location = client.post(
        "/api/samples/locations",
        headers=admin["headers"],
        json={
            "code": "RCL-01", "building": "样品楼", "room": "常温库",
            "cabinet": "一号柜", "shelf": "一层", "sensitivity": "normal",
            "capacity_units": 100,
        },
    ).json()
    batch = client.post(
        "/api/samples/batches",
        headers=admin["headers"],
        json={"batch_code": "RCL-BATCH", "project_code": "RCL", "expected_count": 4},
    ).json()
    root = client.post(
        "/api/samples",
        headers=admin["headers"],
        json={
            "sample_code": "RCL-ROOT", "batch_id": batch["id"], "sample_type": "组织样",
            "quantity": 100, "unit": "g", "location_id": location["id"],
        },
    ).json()

    aliquot = client.post(
        f"/api/samples/{root['id']}/aliquots",
        headers=admin["headers"],
        json={
            "requested_quantity": 60,
            "children": [
                {"sample_code": "RCL-A", "quantity": 40},
                {"sample_code": "RCL-B", "quantity": 12},
                {"sample_code": "RCL-C", "quantity": 8},
            ],
        },
    ).json()
    a, b, c = aliquot["children"]

    # A 已部分消耗于实验（不可撤销影响）
    consumed = client.post(
        f"/api/samples/{a['id']}/consumptions",
        headers=admin["headers"],
        json={"experiment_code": "EXP-RCL-1", "quantity": 10, "idempotency_key": "rcl-consume-a"},
    )
    assert consumed.status_code == 201, consumed.text

    # B 正在借出（召回对象）
    loan_b = client.post(
        "/api/samples/loans",
        headers=admin["headers"],
        json={
            "sample_id": b["id"], "borrower_user_id": approver["id"],
            "quantity": 12, "due_at": "2026-12-01T00:00:00+00:00",
        },
    ).json()

    # C 曾被借出并已归还（应排除通知）
    loan_c = client.post(
        "/api/samples/loans",
        headers=admin["headers"],
        json={
            "sample_id": c["id"], "borrower_user_id": approver["id"],
            "quantity": 8, "due_at": "2026-10-01T00:00:00+00:00",
        },
    ).json()
    returned = client.post(
        f"/api/samples/loans/{loan_c['id']}/returns",
        headers=admin["headers"],
        json={"quantity": 8},
    )
    assert returned.json()["state"] == "returned"

    detail = client.get(f"/api/samples/{root['id']}", headers=admin["headers"]).json()
    source_event_id = detail["events"][-1]["id"]
    return {
        "location": location, "batch": batch, "root": root,
        "a": a, "b": b, "c": c, "loan_b": loan_b, "loan_c": loan_c,
        "source_event_id": source_event_id,
    }


def _open_case(client, admin, tree, key="ctm-key-0001"):
    response = client.post(
        "/api/recalls/contamination-events",
        headers=admin["headers"],
        json={
            "source_sample_id": tree["root"]["id"],
            "source_event_id": tree["source_event_id"],
            "contaminant_label": "外源核酸污染",
            "description": "母样复检发现污染指标阳性",
            "idempotency_key": key,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_open_case_freezes_flags_and_lists_irreversible(client, admin):
    approver = _make_approver(client, admin)
    tree = _build_contaminated_tree(client, admin, approver)
    case = _open_case(client, admin, tree)

    assert case["state"] == "controlled"
    assert case["replayed"] is False
    run = case["latest_run"]
    assert run["affected_sample_count"] == 4  # 母样 + A/B/C
    assert run["frozen_count"] == 3          # 母样、A、C 在库冻结
    assert run["loan_recall_count"] == 1     # B 在借召回
    assert run["irreversible_count"] == 1    # A 的实验消耗
    assert run["excluded_count"] == 1        # C 的借用已归还

    by_key = {(item["object_type"], item["object_id"]): item for item in case["items"]}
    frozen_a = client.get(f"/api/samples/{tree['a']['id']}", headers=admin["headers"]).json()
    assert frozen_a["lifecycle_state"] == "quarantined"
    assert frozen_a["control_case_id"] == case["id"]
    on_loan_b = client.get(f"/api/samples/{tree['b']['id']}", headers=admin["headers"]).json()
    assert on_loan_b["control_case_id"] == case["id"]

    loan_item = by_key[("loan", tree["loan_b"]["id"])]
    assert loan_item["included"] is True
    assert "召回" in loan_item["reason"]
    closed_loan_item = by_key[("loan", tree["loan_c"]["id"])]
    assert closed_loan_item["included"] is False
    assert closed_loan_item["category"] == "loan.closed"
    consumption_item = next(item for item in case["items"] if item["object_type"] == "consumption_record")
    assert consumption_item["included"] is True
    assert "EXP-RCL-1" in consumption_item["reason"]

    assert len(case["notifications"]) == 1
    assert case["notifications"][0]["target_key"] == f"loan:{tree['loan_b']['id']}"


def test_controlled_samples_block_operations(client, admin):
    approver = _make_approver(client, admin)
    tree = _build_contaminated_tree(client, admin, approver)
    _open_case(client, admin, tree)

    consume = client.post(
        f"/api/samples/{tree['a']['id']}/consumptions",
        headers=admin["headers"],
        json={"experiment_code": "EXP-X", "quantity": 1, "idempotency_key": "blocked-1"},
    )
    assert consume.status_code == 409
    aliquot = client.post(
        f"/api/samples/{tree['a']['id']}/aliquots",
        headers=admin["headers"],
        json={"requested_quantity": 1, "children": [{"sample_code": "RCL-BLOCKED", "quantity": 1}]},
    )
    assert aliquot.status_code == 409
    transfer = client.post(
        f"/api/sample-operations/{tree['a']['id']}/transfers",
        headers=admin["headers"],
        json={"location_id": tree["location"]["id"], "expected_version": 1, "reason": "试图转移被冻结样品"},
    )
    assert transfer.status_code == 409
    loan = client.post(
        "/api/samples/loans",
        headers=admin["headers"],
        json={
            "sample_id": tree["c"]["id"], "borrower_user_id": approver["id"],
            "quantity": 1, "due_at": "2026-12-01T00:00:00+00:00",
        },
    )
    assert loan.status_code == 409


def test_open_is_idempotent_and_recalculate_does_not_renotify(client, admin):
    approver = _make_approver(client, admin)
    tree = _build_contaminated_tree(client, admin, approver)
    first = _open_case(client, admin, tree)
    second = _open_case(client, admin, tree)
    assert second["replayed"] is True
    assert second["id"] == first["id"]
    assert second["latest_run"]["run_no"] == first["latest_run"]["run_no"]

    recalc = client.post(
        f"/api/recalls/contamination-events/{first['id']}/recalculate",
        headers=admin["headers"],
    )
    assert recalc.status_code == 200
    assert recalc.json()["changed"] is False

    detail = client.get(f"/api/recalls/contamination-events/{first['id']}", headers=admin["headers"]).json()
    assert len(detail["notifications"]) == 1


def test_recalculate_detects_new_aliquot_and_freezes_it(client, admin):
    approver = _make_approver(client, admin)
    tree = _build_contaminated_tree(client, admin, approver)
    case = _open_case(client, admin, tree)

    # 模拟处理期间另一节点从母样新增分装（并发提交，绕过应用层冻结拦截）。
    now = to_storage(utc_now())
    connection = get_connection()
    root = connection.execute("SELECT * FROM samples WHERE id=?", (tree["root"]["id"],)).fetchone()
    cursor = connection.execute(
        """INSERT INTO samples(sample_code,batch_id,collection_event_id,parent_sample_id,root_sample_id,
               sample_type,quantity,unit,lifecycle_state,location_id,custody_user_id,lineage_depth,
               created_at,updated_at)
           VALUES('RCL-D',?,?,?,?,'组织样',5,'g','available',?,1,1,?,?)""",
        (tree["batch"]["id"], root["collection_event_id"], tree["root"]["id"], tree["root"]["id"],
         tree["location"]["id"], now, now),
    )
    new_id = cursor.lastrowid
    connection.execute(
        """INSERT INTO sample_events(sample_id,event_type,actor_user_id,from_state,to_state,details_json,occurred_at)
           VALUES(?, 'aliquot.created', 1, NULL, 'available', '{}', ?)""",
        (new_id, now),
    )
    connection.commit()

    recalc = client.post(
        f"/api/recalls/contamination-events/{case['id']}/recalculate",
        headers=admin["headers"],
    )
    assert recalc.status_code == 200
    body = recalc.json()
    assert body["changed"] is True
    assert body["latest_run"]["run_no"] == 2
    assert body["latest_run"]["lineage_changed"] is True
    # 快照口径：母样/A/C 仍在冻结桶，新增 D 也进入冻结桶；B 仍在借召回桶
    assert body["latest_run"]["frozen_count"] == 4
    assert body["latest_run"]["loan_recall_count"] == 1
    # 本批次没有新发送通知（去重）
    assert body["latest_run"]["notification_count"] == 0

    d = client.get(f"/api/samples/{new_id}", headers=admin["headers"]).json()
    assert d["lifecycle_state"] == "quarantined"
    detail = client.get(f"/api/recalls/contamination-events/{case['id']}", headers=admin["headers"]).json()
    d_item = next(item for item in detail["items"] if item["object_type"] == "sample" and item["object_id"] == new_id)
    assert d_item["detail"]["new_after_baseline"] is True
    assert "版本变化重算纳入" in d_item["reason"]
    # 既有借用仍只有一条通知
    assert len(detail["notifications"]) == 1


def test_report_explains_inclusion_and_exclusion(client, admin):
    approver = _make_approver(client, admin)
    tree = _build_contaminated_tree(client, admin, approver)
    case = _open_case(client, admin, tree)
    report = client.get(
        f"/api/recalls/contamination-events/{case['id']}/report",
        headers=admin["headers"],
    ).json()
    assert report["run_no"] == case["latest_run"]["run_no"]
    # 4 个样品 + B 的在借召回 + C 的已归还借用 + A 的实验消耗
    assert len(report["report"]["items"]) == 7
    reasons = {item["category"]: item["reason"] for item in report["report"]["items"]}
    assert "sample.frozen" in reasons
    assert "loan.closed" in reasons
    assert "consumption.irreversible" in reasons
    assert all(len(reason) > 10 for reason in reasons.values())


def test_release_requires_conclusion_independent_approval_and_settles_loans(client, admin):
    approver = _make_approver(client, admin)
    tree = _build_contaminated_tree(client, admin, approver)
    case = _open_case(client, admin, tree)

    # 调查结论过短
    short = client.post(
        f"/api/recalls/contamination-events/{case['id']}/release-requests",
        headers=admin["headers"],
        json={"conclusion": "无污染"},
    )
    assert short.status_code == 422

    request = client.post(
        f"/api/recalls/contamination-events/{case['id']}/release-requests",
        headers=admin["headers"],
        json={"conclusion": "经三轮复检，污染仅来自一次性耗材，分装后代检测全部阴性，建议解除控制。"},
    )
    assert request.status_code == 201, request.text
    request_id = request.json()["id"]

    # 申请人不能自批
    own = client.post(
        f"/api/recalls/release-requests/{request_id}/decisions",
        headers=admin["headers"],
        json={"decision": "approve", "comment": "同意"},
    )
    assert own.status_code == 422

    # 召回借用未归还前不能解除
    blocked = client.post(
        f"/api/recalls/release-requests/{request_id}/decisions",
        headers=approver["headers"],
        json={"decision": "approve", "comment": "同意解除"},
    )
    assert blocked.status_code == 409
    assert tree["loan_b"]["loan_code"] in blocked.text

    # 借用人归还：入库样品继续隔离
    returned = client.post(
        f"/api/samples/loans/{tree['loan_b']['id']}/returns",
        headers=admin["headers"],
        json={"quantity": 12},
    )
    assert returned.status_code == 200, returned.text
    b = client.get(f"/api/samples/{tree['b']['id']}", headers=admin["headers"]).json()
    assert b["lifecycle_state"] == "quarantined"

    # 独立审批人批准解除
    approved = client.post(
        f"/api/recalls/release-requests/{request_id}/decisions",
        headers=approver["headers"],
        json={"decision": "approve", "comment": "复检证据充分，同意解除"},
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["state"] == "approved"
    assert approved.json()["decided_by"] == approver["id"]

    detail = client.get(f"/api/recalls/contamination-events/{case['id']}", headers=admin["headers"]).json()
    assert detail["state"] == "released"
    for sid, expected_state in (
        (tree["root"]["id"], "available"),
        (tree["a"]["id"], "partially_consumed"),
        (tree["b"]["id"], "available"),
        (tree["c"]["id"], "available"),
    ):
        sample = client.get(f"/api/samples/{sid}", headers=admin["headers"]).json()
        assert sample["control_case_id"] is None
        assert sample["lifecycle_state"] == expected_state, (sid, sample["lifecycle_state"])

    # 解除后作业恢复：再次登记消耗成功
    consume = client.post(
        f"/api/samples/{tree['a']['id']}/consumptions",
        headers=admin["headers"],
        json={"experiment_code": "EXP-RCL-2", "quantity": 5, "idempotency_key": "rcl-consume-a-2"},
    )
    assert consume.status_code == 201, consume.text


def test_release_rejection_keeps_control(client, admin):
    approver = _make_approver(client, admin)
    tree = _build_contaminated_tree(client, admin, approver)
    case = _open_case(client, admin, tree)
    request = client.post(
        f"/api/recalls/contamination-events/{case['id']}/release-requests",
        headers=admin["headers"],
        json={"conclusion": "初步判断为检测误差，但证据仍在补充中。"},
    ).json()
    rejected = client.post(
        f"/api/recalls/release-requests/{request['id']}/decisions",
        headers=approver["headers"],
        json={"decision": "reject", "comment": "证据不足，维持控制"},
    )
    assert rejected.status_code == 200
    assert rejected.json()["state"] == "rejected"
    detail = client.get(f"/api/recalls/contamination-events/{case['id']}", headers=admin["headers"]).json()
    assert detail["state"] == "controlled"
    a = client.get(f"/api/samples/{tree['a']['id']}", headers=admin["headers"]).json()
    assert a["lifecycle_state"] == "quarantined"
