from __future__ import annotations


def _setup_tree(client, admin, *, consumed=False, loaned=False):
    """建立 母样 -> 两个子样 的谱系，可选消耗/借用其一。"""
    location = client.post(
        "/api/samples/locations",
        headers=admin["headers"],
        json={"code": "CTM-LOC", "building": "样品楼", "room": "隔离库", "cabinet": "A", "shelf": "1",
              "sensitivity": "normal", "capacity_units": 100},
    )
    assert location.status_code == 201, location.text
    batch = client.post(
        "/api/samples/batches",
        headers=admin["headers"],
        json={"batch_code": "CTM-BATCH", "project_code": "CTM", "expected_count": 1},
    )
    assert batch.status_code == 201, batch.text
    root = client.post(
        "/api/samples",
        headers=admin["headers"],
        json={"sample_code": "CTM-ROOT", "batch_id": batch.json()["id"], "sample_type": "组织",
              "quantity": 100, "unit": "g", "location_id": location.json()["id"]},
    )
    assert root.status_code == 201, root.text
    aliquot = client.post(
        f"/api/samples/{root.json()['id']}/aliquots",
        headers=admin["headers"],
        json={"requested_quantity": 40, "loss_quantity": 0,
              "children": [{"sample_code": "CTM-C1", "quantity": 20}, {"sample_code": "CTM-C2", "quantity": 20}]},
    )
    assert aliquot.status_code == 201, aliquot.text
    children = aliquot.json()["children"]
    c1, c2 = children[0], children[1]
    loan = None
    if loaned:
        loan = client.post(
            "/api/samples/loans",
            headers=admin["headers"],
            json={"sample_id": c2["id"], "borrower_user_id": admin["body"]["user"]["id"],
                  "quantity": 20, "due_at": "2026-10-30T00:00:00+00:00"},
        )
        assert loan.status_code == 201, loan.text
        loan = loan.json()
    if consumed:
        resp = client.post(
            f"/api/samples/{c1['id']}/consumptions",
            headers=admin["headers"],
            json={"experiment_code": "EXP-CTM-1", "quantity": 20, "idempotency_key": "ctm-consume-1", "note": "全量消耗"},
        )
        assert resp.status_code == 201, resp.text
    return location.json(), batch.json(), root.json(), c1, c2, loan


def _create_event(client, admin, source_id, title="上游母样污染", severity="critical"):
    resp = client.post(
        "/api/contamination/events",
        headers=admin["headers"],
        json={"source_sample_id": source_id, "title": title, "description": "上游确认污染", "severity": severity},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _make_approver(client, admin, username):
    created = client.post(
        "/api/users",
        headers=admin["headers"],
        json={"username": username, "password": "Approver!234", "display_name": username,
              "role_codes": ["approver"]},
    )
    assert created.status_code == 201, created.text
    login = client.post(
        "/api/auth/login",
        json={"username": username, "password": "Approver!234", "client_label": "tests"},
    )
    assert login.status_code == 200, login.text
    return {"headers": {"Authorization": f"Bearer {login.json()['token']}"}, "body": login.json()}


def test_containment_freezes_instock_descendants(client, admin):
    _, _, root, c1, c2, _ = _setup_tree(client, admin)
    event = _create_event(client, admin, root["id"])
    assert event["state"] == "contained"
    dispositions = {(a["object_type"], a["object_id"]): a for a in event["actions"]}
    frozen_ids = {a["object_id"] for a in event["actions"] if a["disposition"] == "frozen"}
    assert {root["id"], c1["id"], c2["id"]} <= frozen_ids
    for sample_id in (root["id"], c1["id"], c2["id"]):
        detail = client.get(f"/api/samples/{sample_id}", headers=admin["headers"]).json()
        assert detail["lifecycle_state"] == "quarantined"
        assert detail["contamination_lock"] == event["id"]
    summary = event["report"]["summary"]
    assert summary["frozen"] == 3
    assert summary["irreversible"] == 0
    # 根样上留有冻结事件
    detail = client.get(f"/api/samples/{root['id']}", headers=admin["headers"]).json()
    assert any(e["event_type"] == "contamination.frozen" for e in detail["events"])
    assert dispositions[("sample", root["id"])]["status"] == "applied"


def test_consumed_descendant_is_irreversible_and_explained(client, admin):
    _, _, root, c1, c2, _ = _setup_tree(client, admin, consumed=True)
    event = _create_event(client, admin, root["id"])
    irreversible = [a for a in event["actions"] if a["disposition"] == "irreversible"]
    assert len(irreversible) == 1
    item = irreversible[0]
    assert item["object_type"] == "consumption"
    assert item["sample_id"] == c1["id"]
    assert "EXP-CTM-1" in item["inclusion_reason"]
    assert item["status"] == "applied"
    # 被完全消耗的子样没有在库冻结动作，但其样品对象有排除说明
    sample_action = next(a for a in event["actions"] if a["object_type"] == "sample" and a["object_id"] == c1["id"])
    assert sample_action["disposition"] == "excluded"
    assert sample_action["exclusion_reason"]
    report_objects = event["report"]["objects"]
    assert all(o["reason"] for o in report_objects)  # 每个对象都说明为何纳入/排除


def test_loan_recall_notification_sent_once_across_retries(client, admin):
    _, _, root, c1, c2, loan = _setup_tree(client, admin, loaned=True)
    event = _create_event(client, admin, root["id"])
    notifications = event["notifications"]
    assert len(notifications) == 1
    note = notifications[0]
    assert note["loan_id"] == loan["id"]
    assert note["status"] == "sent"
    assert loan["loan_code"] in note["content"]
    # 在借子样：加锁但状态仍为 loaned
    detail = client.get(f"/api/samples/{c2['id']}", headers=admin["headers"]).json()
    assert detail["lifecycle_state"] == "loaned"
    assert detail["contamination_lock"] == event["id"]
    recalled = [a for a in event["actions"] if a["disposition"] == "recalled"]
    assert any(a["object_type"] == "loan" and a["object_id"] == loan["id"] for a in recalled)
    # 多次重试不重复通知、不重复冻结
    for _ in range(2):
        again = client.post(f"/api/contamination/events/{event['id']}/recalculate", headers=admin["headers"])
        assert again.status_code == 200, again.text
        assert again.json()["lineage_changed"] is False
        assert len(again.json()["notifications"]) == 1
    detail = client.get(f"/api/samples/{c2['id']}", headers=admin["headers"]).json()
    freeze_events = [e for e in detail["events"] if e["event_type"] == "contamination.lock_placed"]
    assert len(freeze_events) == 1


def test_recalculate_detects_new_aliquot_and_freezes_it(client, admin):
    _, _, root, c1, c2, _ = _setup_tree(client, admin)
    event = _create_event(client, admin, root["id"])
    sig_before = event["lineage"]["signature"]
    # 已冻结样品禁止继续分装（管控后新增分装只能走重算通道）
    blocked = client.post(
        f"/api/samples/{c1['id']}/aliquots",
        headers=admin["headers"],
        json={"requested_quantity": 1, "loss_quantity": 0,
              "children": [{"sample_code": "CTM-C1-X", "quantity": 1}]},
    )
    assert blocked.status_code == 409
    # 直接在库中插入新增分装，代表首轮计算窗口外新增的后代
    from app.database import transaction

    with transaction(immediate=True) as connection:
        now = "2026-09-25T10:00:00+00:00"
        cur = connection.execute(
            """INSERT INTO samples(sample_code,batch_id,parent_sample_id,root_sample_id,sample_type,
                   quantity,reserved_quantity,unit,lifecycle_state,location_id,custody_user_id,
                   lineage_depth,created_at,updated_at)
               SELECT 'CTM-C2-X',batch_id,?,root_sample_id,sample_type,5,0,unit,'available',location_id,?,2,?,?
               FROM samples WHERE id=?""",
            (c2["id"], admin["body"]["user"]["id"], now, now, c2["id"]),
        )
        new_id = cur.lastrowid
    recalc = client.post(f"/api/contamination/events/{event['id']}/recalculate", headers=admin["headers"])
    assert recalc.status_code == 200, recalc.text
    body = recalc.json()
    assert body["lineage_changed"] is True
    assert body["lineage"]["signature"] != sig_before
    assert body["lineage"]["recalculation_count"] >= 1
    frozen_ids = {a["object_id"] for a in body["actions"] if a["disposition"] == "frozen"}
    assert new_id in frozen_ids
    detail = client.get(f"/api/samples/{new_id}", headers=admin["headers"]).json()
    assert detail["lifecycle_state"] == "quarantined"
    assert detail["contamination_lock"] == event["id"]
    # 再次重算谱系稳定
    stable = client.post(f"/api/contamination/events/{event['id']}/recalculate", headers=admin["headers"])
    assert stable.json()["lineage_changed"] is False


def test_locked_samples_block_consume_loan_transfer(client, admin):
    _, _, root, c1, c2, _ = _setup_tree(client, admin)
    _create_event(client, admin, root["id"])
    consume = client.post(
        f"/api/samples/{root['id']}/consumptions",
        headers=admin["headers"],
        json={"experiment_code": "EXP-X", "quantity": 1, "idempotency_key": "lock-block-1"},
    )
    assert consume.status_code == 409
    loan = client.post(
        "/api/samples/loans",
        headers=admin["headers"],
        json={"sample_id": c1["id"], "borrower_user_id": admin["body"]["user"]["id"],
              "quantity": 1, "due_at": "2026-10-30T00:00:00+00:00"},
    )
    assert loan.status_code == 409


def test_release_requires_conclusion_and_two_independent_approvals(client, admin):
    _, batch, root, c1, c2, _ = _setup_tree(client, admin)
    event = _create_event(client, admin, root["id"])
    # 未记录调查结论不能申请解除
    no_conclusion = client.post(
        f"/api/contamination/events/{event['id']}/release-requests", headers=admin["headers"], json={"note": "解除"}
    )
    assert no_conclusion.status_code == 409
    recorded = client.post(
        f"/api/contamination/events/{event['id']}/investigation",
        headers=admin["headers"],
        json={"conclusion": "经复检为上游标签错误，本批次后代实际无污染，同意解除控制"},
    )
    assert recorded.status_code == 200, recorded.text
    request = client.post(
        f"/api/contamination/events/{event['id']}/release-requests", headers=admin["headers"], json={"note": "申请解除"}
    )
    assert request.status_code == 201, request.text
    request_id = request.json()["id"]
    approver_one = _make_approver(client, admin, "approverone")
    approver_two = _make_approver(client, admin, "approvertwo")
    # 申请人不能自审
    self_decide = client.post(
        f"/api/contamination/release-requests/{request_id}/decisions",
        headers=admin["headers"], json={"decision": "approve", "comment": "自审"},
    )
    assert self_decide.status_code == 422
    first = client.post(
        f"/api/contamination/release-requests/{request_id}/decisions",
        headers=approver_one["headers"], json={"decision": "approve", "comment": "同意"},
    )
    assert first.status_code == 200, first.text
    assert first.json()["state"] == "pending"
    # 同一审批人不能重复决定
    duplicate = client.post(
        f"/api/contamination/release-requests/{request_id}/decisions",
        headers=approver_one["headers"], json={"decision": "approve", "comment": "再次同意"},
    )
    assert duplicate.status_code == 409
    second = client.post(
        f"/api/contamination/release-requests/{request_id}/decisions",
        headers=approver_two["headers"], json={"decision": "approve", "comment": "同意"},
    )
    assert second.status_code == 200, second.text
    assert second.json()["state"] == "approved"
    detail = client.get(f"/api/contamination/events/{event['id']}", headers=admin["headers"]).json()
    assert detail["state"] == "released"
    assert detail["released_at"]
    # 样品恢复在库、污染锁解除
    for sample_id in (root["id"], c1["id"], c2["id"]):
        sample = client.get(f"/api/samples/{sample_id}", headers=admin["headers"]).json()
        assert sample["lifecycle_state"] == "available"
        assert sample["contamination_lock"] is None
        assert any(e["event_type"] == "contamination.released" for e in sample["events"])
    # 已解除不能再重算
    gone = client.post(f"/api/contamination/events/{event['id']}/recalculate", headers=admin["headers"])
    assert gone.status_code == 409


def test_reject_release_keeps_containment(client, admin):
    _, _, root, c1, c2, _ = _setup_tree(client, admin)
    event = _create_event(client, admin, root["id"])
    client.post(
        f"/api/contamination/events/{event['id']}/investigation",
        headers=admin["headers"], json={"conclusion": "仍在等待第三方检测结果，暂不能给出最终结论"},
    )
    request = client.post(
        f"/api/contamination/events/{event['id']}/release-requests", headers=admin["headers"], json={}
    )
    approver_one = _make_approver(client, admin, "approverthree")
    rejected = client.post(
        f"/api/contamination/release-requests/{request.json()['id']}/decisions",
        headers=approver_one["headers"], json={"decision": "reject", "comment": "证据不足"},
    )
    assert rejected.json()["state"] == "rejected"
    detail = client.get(f"/api/contamination/events/{event['id']}", headers=admin["headers"]).json()
    assert detail["state"] == "contained"
    sample = client.get(f"/api/samples/{c1['id']}", headers=admin["headers"]).json()
    assert sample["lifecycle_state"] == "quarantined"
    assert sample["contamination_lock"] == event["id"]


def test_returned_loan_during_containment_goes_to_quarantine(client, admin):
    _, _, root, c1, c2, loan = _setup_tree(client, admin, loaned=True)
    _create_event(client, admin, root["id"])
    # 借用人归还：样品应进入隔离而非可借
    returned = client.post(
        f"/api/samples/loans/{loan['id']}/returns", headers=admin["headers"], json={"quantity": 20}
    )
    assert returned.status_code == 200, returned.text
    assert returned.json()["state"] == "returned"
    sample = client.get(f"/api/samples/{c2['id']}", headers=admin["headers"]).json()
    assert sample["lifecycle_state"] == "quarantined"
    assert sample["contamination_lock"] is not None


def test_loan_returned_before_contamination_is_excluded_with_reason(client, admin):
    _, _, root, c1, c2, loan = _setup_tree(client, admin, loaned=True)
    # 污染确认前已全部归还
    returned = client.post(
        f"/api/samples/loans/{loan['id']}/returns", headers=admin["headers"], json={"quantity": 20}
    )
    assert returned.status_code == 200, returned.text
    event = _create_event(client, admin, root["id"])
    loan_action = next(
        a for a in event["actions"] if a["object_type"] == "loan" and a["object_id"] == loan["id"]
    )
    assert loan_action["disposition"] == "excluded"
    assert loan_action["status"] == "excluded"
    assert loan_action["exclusion_reason"]
    # 回库样品仍按在库冻结
    frozen = {a["object_id"] for a in event["actions"] if a["disposition"] == "frozen"}
    assert c2["id"] in frozen
    assert event["notifications"] == []
    report = next(
        o for o in event["report"]["objects"]
        if o["object_type"] == "loan" and o["object_id"] == loan["id"]
    )
    assert report["included"] is False and report["reason"]
