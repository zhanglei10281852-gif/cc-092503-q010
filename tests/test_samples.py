from __future__ import annotations


def create_location(client, admin, code="L-A-01", sensitivity="restricted"):
    response = client.post(
        "/api/samples/locations",
        headers=admin["headers"],
        json={"code": code, "building": "科研楼", "room": "低温间", "cabinet": "柜一", "shelf": "二层", "sensitivity": sensitivity, "capacity_units": 100},
    )
    assert response.status_code == 201, response.text
    return response.json()


def create_batch_sample(client, admin):
    location = create_location(client, admin)
    batch = client.post(
        "/api/samples/batches",
        headers=admin["headers"],
        json={"batch_code": "BATCH-2026-001", "project_code": "P-ALPHA", "expected_count": 2},
    )
    assert batch.status_code == 201, batch.text
    sample = client.post(
        "/api/samples",
        headers=admin["headers"],
        json={"sample_code": "S-001", "batch_id": batch.json()["id"], "sample_type": "土壤", "quantity": 100, "unit": "g", "location_id": location["id"]},
    )
    assert sample.status_code == 201, sample.text
    return batch.json(), sample.json()


def test_receive_and_location_masking(client, admin):
    _, sample = create_batch_sample(client, admin)
    detail = client.get(f"/api/samples/{sample['id']}", headers=admin["headers"])
    assert detail.status_code == 200
    assert detail.json()["location_code"] == "L-A-01"
    assert detail.json()["events"][0]["event_type"] == "received"


def test_aliquot_preserves_mass_and_lineage(client, admin):
    _, sample = create_batch_sample(client, admin)
    response = client.post(
        f"/api/samples/{sample['id']}/aliquots",
        headers=admin["headers"],
        json={
            "requested_quantity": 30,
            "loss_quantity": 2,
            "children": [{"sample_code": "S-001-A", "quantity": 10}, {"sample_code": "S-001-B", "quantity": 18}],
            "note": "两份检测子样",
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["parent"]["quantity"] == 70
    assert [item["parent_sample_id"] for item in response.json()["children"]] == [sample["id"], sample["id"]]


def test_consumption_is_idempotent(client, admin):
    _, sample = create_batch_sample(client, admin)
    payload = {"experiment_code": "EXP-01", "quantity": 12.5, "idempotency_key": "consume-001", "note": "理化检测"}
    first = client.post(f"/api/samples/{sample['id']}/consumptions", headers=admin["headers"], json=payload)
    second = client.post(f"/api/samples/{sample['id']}/consumptions", headers=admin["headers"], json=payload)
    assert first.status_code == second.status_code == 201
    assert first.json()["sample"]["quantity"] == second.json()["sample"]["quantity"] == 87.5
    assert second.json()["replayed"] is True


def test_loan_partial_and_full_return(client, admin):
    _, sample = create_batch_sample(client, admin)
    loan = client.post(
        "/api/samples/loans",
        headers=admin["headers"],
        json={"sample_id": sample["id"], "borrower_user_id": admin["body"]["user"]["id"], "quantity": 20, "due_at": "2026-10-01T00:00:00+00:00"},
    )
    assert loan.status_code == 201, loan.text
    partial = client.post(f"/api/samples/loans/{loan.json()['id']}/returns", headers=admin["headers"], json={"quantity": 5})
    finished = client.post(f"/api/samples/loans/{loan.json()['id']}/returns", headers=admin["headers"], json={"quantity": 15})
    assert partial.json()["state"] == "partially_returned"
    assert finished.json()["state"] == "returned"


def test_two_distinct_approvers_required(client, admin):
    _, sample = create_batch_sample(client, admin)
    approval = client.post(
        "/api/samples/approvals",
        headers=admin["headers"],
        json={"action_type": "destruction", "resource_type": "sample", "resource_id": sample["id"], "payload": {"quantity": 10}},
    )
    assert approval.status_code == 201
    own = client.post(f"/api/samples/approvals/{approval.json()['id']}/decisions", headers=admin["headers"], json={"decision": "approve"})
    assert own.status_code == 422


def test_anomaly_requires_business_target(client, admin):
    response = client.post(
        "/api/samples/anomalies",
        headers=admin["headers"],
        json={"anomaly_type": "标签破损", "severity": "high", "description": "二维码与人工标签无法对应"},
    )
    assert response.status_code == 422
