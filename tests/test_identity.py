from __future__ import annotations


def test_bootstrap_login_and_me(client, admin):
    response = client.get("/api/auth/me", headers=admin["headers"])
    assert response.status_code == 200
    assert response.json()["username"] == "admin"
    assert "samples.write" in response.json()["permissions"]


def test_bootstrap_is_single_use(client, admin):
    response = client.post("/api/auth/bootstrap", json={"username": "second", "password": "Admin!23456", "client_label": "tests"})
    assert response.status_code == 409


def test_user_role_and_session_revocation(client, admin):
    role = client.post(
        "/api/roles",
        headers=admin["headers"],
        json={"code": "lab.reader", "name": "实验室查看员", "permission_codes": ["samples.read"]},
    )
    assert role.status_code == 201, role.text
    user = client.post(
        "/api/users",
        headers=admin["headers"],
        json={"username": "researcher.one", "password": "Research!23456", "display_name": "研究员甲", "role_codes": ["lab.reader"]},
    )
    assert user.status_code == 201, user.text
    login = client.post("/api/auth/login", json={"username": "researcher.one", "password": "Research!23456", "client_label": "tests"})
    assert login.status_code == 200
    token = login.json()["token"]
    changed = client.patch(f"/api/users/{user.json()['id']}", headers=admin["headers"], json={"status": "disabled"})
    assert changed.status_code == 200
    assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401
