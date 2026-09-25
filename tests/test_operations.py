from __future__ import annotations


def _bootstrap_sample(client, admin):
    location = client.post(
        "/api/samples/locations",
        headers=admin["headers"],
        json={
            "code": "OPS-01",
            "building": "样品楼",
            "room": "常温库",
            "cabinet": "三号柜",
            "shelf": "二层",
            "sensitivity": "normal",
            "capacity_units": 50,
        },
    ).json()
    batch = client.post(
        "/api/samples/batches",
        headers=admin["headers"],
        json={"batch_code": "OPS-BATCH", "project_code": "OPS", "expected_count": 1},
    ).json()
    sample = client.post(
        "/api/samples",
        headers=admin["headers"],
        json={
            "sample_code": "OPS-SAMPLE",
            "batch_id": batch["id"],
            "sample_type": "水样",
            "quantity": 20,
            "unit": "mL",
            "location_id": location["id"],
        },
    ).json()
    return location, batch, sample


def test_collection_registration_is_idempotent(client, admin):
    payload = {
        "field_code": "FIELD-001",
        "project_code": "OPS",
        "collected_by": "野外组甲",
        "collected_at": "2026-09-25T08:00:00+00:00",
        "source_kind": "河水",
        "source_reference": "断面 A",
        "quantity": 500,
        "unit": "mL",
        "preservation": "4 摄氏度避光",
    }
    first = client.post("/api/sample-operations/collections", headers=admin["headers"], json=payload)
    second = client.post("/api/sample-operations/collections", headers=admin["headers"], json=payload)
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert second.json()["replayed"] is True


def test_inventory_reconciles_and_closes(client, admin):
    location, _, sample = _bootstrap_sample(client, admin)
    session = client.post(
        "/api/sample-operations/inventory",
        headers=admin["headers"],
        json={"location_id": location["id"], "session_code": "INV-OPS-01"},
    )
    assert session.status_code == 201, session.text
    count = client.post(
        f"/api/sample-operations/inventory/{session.json()['id']}/counts",
        headers=admin["headers"],
        json={"sample_id": sample["id"], "observed_present": True, "observed_quantity": 20},
    )
    assert count.status_code == 200, count.text
    reconciled = client.post(
        f"/api/sample-operations/inventory/{session.json()['id']}/reconcile",
        headers=admin["headers"],
    )
    assert reconciled.status_code == 200
    assert reconciled.json()["differences"] == []
    closed = client.post(
        f"/api/sample-operations/inventory/{session.json()['id']}/close",
        headers=admin["headers"],
    )
    assert closed.status_code == 200
    assert closed.json()["state"] == "closed"


def test_transfer_updates_lineage_event(client, admin):
    _, _, sample = _bootstrap_sample(client, admin)
    target = client.post(
        "/api/samples/locations",
        headers=admin["headers"],
        json={
            "code": "OPS-02",
            "building": "样品楼",
            "room": "低温库",
            "cabinet": "一号柜",
            "shelf": "一层",
            "sensitivity": "restricted",
            "capacity_units": 50,
        },
    ).json()
    moved = client.post(
        f"/api/sample-operations/{sample['id']}/transfers",
        headers=admin["headers"],
        json={"location_id": target["id"], "expected_version": sample["version"], "reason": "转入低温保存"},
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["sample"]["location_id"] == target["id"]
    detail = client.get(f"/api/samples/{sample['id']}", headers=admin["headers"])
    assert detail.json()["events"][-1]["event_type"] == "location.transferred"
