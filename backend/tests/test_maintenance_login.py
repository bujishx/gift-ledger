import json
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from app.services.maintenance_sessions import OwnerSessions

ORIGIN = "https://192.0.2.10:16450"
PROJECT = "owner-login-test"
BASE = "/api/v1/maintenance"


@pytest.fixture
def site(tmp_path: Path, monkeypatch):
    project_state = tmp_path / "maintenance"
    owner_state = tmp_path / "owner"
    project_state.mkdir()
    owner_state.mkdir(mode=0o700)
    config = {"project": PROJECT, "origin": ORIGIN, "instance_id": str(uuid4())}
    (project_state / "owner-site.json").write_text(json.dumps(config))
    monkeypatch.setenv("GIFTLEDGER_MAINTENANCE_DIR", str(project_state))
    monkeypatch.setenv("GIFTLEDGER_OWNER_STATE_DIR", str(owner_state))
    monkeypatch.setenv("GIFTLEDGER_PROJECT", PROJECT)
    with TestClient(
        create_app(Settings(environment="test", trusted_origins=ORIGIN, _env_file=None)),
        base_url=ORIGIN,
    ) as client:
        yield client, OwnerSessions(owner_state, **config)


def test_both_login_methods_and_forgotten_password(site):
    client, owner = site
    origin = {"Origin": ORIGIN}
    assert client.get(BASE + "/access-methods").json() == {
        "methods": ["code", "password"],
        "default": "code",
        "username_required": False,
    }

    def login(method, credential):
        return client.post(
            BASE + "/login", json={"method": method, "credential": credential}, headers=origin
        )

    def set_password(value):
        return client.post(
            BASE + "/owner-password",
            json={"code": owner.issue_login_code(), "new_password": value},
            headers=origin,
        )

    def authorized(response):
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert "set-cookie" not in response.headers
        assert response.json()["role"] == "site_owner" and response.json()["project"] == PROJECT
        return {"Authorization": "Bearer " + response.json()["token"]}

    code = owner.issue_login_code()
    first_session = authorized(login("code", code))
    assert login("code", code).status_code == 403
    assert client.get(BASE + "/session", headers=first_session).status_code == 200
    password = "Unique-test-maintenance-A"
    assert login("password", password).status_code == 403
    authorized(set_password(password))
    assert client.get(BASE + "/session", headers=first_session).status_code == 403
    previous = authorized(login("password", password))
    # The server-issued code still works even when an owner password has been set.
    authorized(login("code", owner.issue_login_code()))
    replacement = "Unique-test-replacement-B"
    authorized(set_password(replacement))
    assert client.get(BASE + "/session", headers=previous).status_code == 403
    assert login("password", password).status_code == 403
    authorized(login("password", replacement))
    current = authorized(set_password(None))
    assert login("password", replacement).status_code == 403
    assert client.post(BASE + "/logout", json={}, headers={**origin, **current}).status_code == 200
    assert client.get(BASE + "/session", headers=current).status_code == 403
    authorized(login("code", owner.issue_login_code()))


def test_foreign_origin_does_not_consume_code_and_missing_password_is_not_removal(site):
    client, owner = site
    code = owner.issue_login_code()
    body = {"method": "code", "credential": code}
    headers = {"Origin": ORIGIN}
    assert (
        client.post(BASE + "/login", json=body, headers={"Origin": ORIGIN[:-1] + "1"}).status_code
        == 403
    )
    assert client.post(BASE + "/login", json=body).status_code == 403
    assert client.post(BASE + "/login?code=discard", json=body, headers=headers).status_code == 400
    assert (
        client.post(BASE + "/owner-password", json={"code": code}, headers=headers).status_code
        == 422
    )
    assert client.post(BASE + "/login", json=body, headers=headers).status_code == 200
