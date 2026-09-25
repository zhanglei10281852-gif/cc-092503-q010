from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path):
    os.environ["SAMPLE_DATABASE_PATH"] = str(tmp_path / "test.db")
    from app.database import close_connection
    from app.main import app

    close_connection()
    with TestClient(app) as test_client:
        yield test_client
    close_connection()


@pytest.fixture()
def admin(client):
    response = client.post(
        "/api/auth/bootstrap",
        json={"username": "admin", "password": "Admin!23456", "display_name": "样品平台主管", "client_label": "tests"},
    )
    assert response.status_code == 201, response.text
    login = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "Admin!23456", "client_label": "tests"},
    )
    assert login.status_code == 200, login.text
    body = login.json()
    return {"headers": {"Authorization": f"Bearer {body['token']}"}, "body": body}
