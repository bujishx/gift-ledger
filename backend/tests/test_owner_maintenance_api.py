import hashlib
import json
import secrets
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.api.dependencies import get_authentication_session, get_database_session
from app.core.config import Settings
from app.core.security import hash_password
from app.db.models import User
from app.main import create_app
from app.services.maintenance_sessions import OwnerSessions

ORIGIN = "https://192.0.2.8:16449"
SIBLING = "https://192.0.2.8:16450"
BASE = "/api/v1/maintenance"


@dataclass
class Site:
    root: Path
    store: OwnerSessions
    db: Engine
    legacy: str
    old_password: str
    project: str
    settings: Settings

    def client(self) -> TestClient:
        app = create_app(self.settings)

        def database():
            with Session(self.db) as session:
                yield session

        app.dependency_overrides[get_database_session] = database
        app.dependency_overrides[get_authentication_session] = database
        return TestClient(app, base_url=ORIGIN)

    def verified(self):
        return self.store.login_with_code(self.store.issue_login_code(), request_origin=ORIGIN)

    def seed(self):
        encoded = hash_password(self.old_password)
        with Session(self.db) as session:
            for id_, name, active, admin in (
                (1, "ledger_user", 1, 0),
                (2, "ledger_admin", 1, 1),
                (3, "disabled_user", 0, 0),
            ):
                session.add(
                    User(
                        id=id_,
                        username=name,
                        password_hash=encoded,
                        is_active=active,
                        is_admin=admin,
                    )
                )
            session.commit()


@pytest.fixture
def site(monkeypatch, tmp_path):
    root = tmp_path / "maintenance"
    root.mkdir()
    (root / "requests").mkdir()
    (root / "status").mkdir()
    directory = (tmp_path / "private-owner").resolve()
    directory.mkdir(mode=0o700)
    site_id = str(uuid4())
    project = "qa-owner"
    binding = {"instance_id": site_id, "project": project, "origin": ORIGIN}
    (root / "owner-site.json").write_text(json.dumps(binding))
    legacy = secrets.token_hex(32)
    (root / "owner.sha256").write_text(hashlib.sha256(legacy.encode()).hexdigest() + "\n")
    status = {
        "format": "giftledger-maintenance-status-v1",
        "project": project,
        "state": "idle",
        "operation_id": None,
    }
    (root / "status" / "result.json").write_text(json.dumps(status))
    monkeypatch.setenv("GIFTLEDGER_OWNER_STATE_DIR", str(directory))
    monkeypatch.setenv("GIFTLEDGER_MAINTENANCE_DIR", str(root))
    monkeypatch.setenv("GIFTLEDGER_PROJECT", project)
    settings = Settings(
        environment="test",
        trusted_origins=ORIGIN,
        jwt_secret=secrets.token_urlsafe(36),
        _env_file=None,
    )
    db = create_engine(f"sqlite:///{tmp_path / 'ledger.sqlite3'}")
    # Use only the columns used by these application routes; production DDL runs in the MySQL suite.
    with db.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT UNIQUE, "
            "password_hash TEXT, is_active INTEGER, is_admin INTEGER, "
            "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )
        conn.exec_driver_sql(
            "CREATE TABLE contacts (name TEXT, user_id INTEGER, relation_type_id INTEGER)"
        )
        conn.exec_driver_sql(
            "CREATE TABLE relationship_types (id INTEGER, user_id INTEGER, system_code TEXT)"
        )
        conn.exec_driver_sql(
            "CREATE TABLE gift_records (id INTEGER PRIMARY KEY, user_id INTEGER, amount NUMERIC)"
        )
    try:
        yield Site(
            root,
            OwnerSessions(directory, **binding),
            db,
            legacy,
            secrets.token_urlsafe(16),
            project,
            settings,
        )
    finally:
        db.dispose()


def headers(token, origin=ORIGIN):
    result = {"Authorization": "Bearer " + token}
    if origin is not None:
        result["Origin"] = origin
    return result


def login(client, username, password):
    return client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
        headers={"Origin": ORIGIN},
    )


def assert_no_maintenance_access(client, auth, root):
    for endpoint in ("session", "accounts", "status"):
        assert client.get(f"{BASE}/{endpoint}", headers=auth).status_code in (401, 403)
    for endpoint, payload in (
        ("backup", {"operation_id": uuid4().hex}),
        (
            "accounts/reset-password",
            {"username": "ledger_user", "new_password": secrets.token_urlsafe(16)},
        ),
    ):
        assert client.post(f"{BASE}/{endpoint}", json=payload, headers=auth).status_code in (
            401,
            403,
        )
    assert not (root / "requests" / "backup.request").exists()


def test_empty_target_and_restarted_api_accept_own_verified_session(site):
    token = site.verified()
    with site.client() as before:
        assert before.get(BASE + "/session", headers=headers(token)).json() == {
            "authenticated": True,
            "role": "site_owner",
            "project": site.project,
        }
        assert before.get(BASE + "/accounts", headers=headers(token)).json() == {"accounts": []}
    operation = uuid4().hex
    with site.client() as restarted:
        assert restarted.get(BASE + "/status", headers=headers(token)).json()["state"] == "idle"
        response = restarted.post(
            BASE + "/backup", headers=headers(token), json={"operation_id": operation}
        )
        assert response.status_code == 202
        assert response.json()["operation_id"] == operation
        assert (
            site.root / "requests" / "backup.request"
        ).read_bytes() == operation.encode() + b"\n"


def test_stage_one_bearer_has_no_authority_once_owner_sessions_are_enabled(site):
    with site.client() as client:
        assert_no_maintenance_access(client, headers(site.legacy), site.root)
        assert client.get(BASE + "/session", headers=headers(site.verified())).status_code == 200


@pytest.mark.parametrize("user", ["ledger_user", "ledger_admin"])
def test_real_business_cookie_never_grants_owner_access(site, user):
    site.seed()
    with site.client() as client:
        assert login(client, user, site.old_password).status_code == 200
        assert client.get("/api/v1/auth/me").json()["username"] == user
        assert_no_maintenance_access(client, {"Origin": ORIGIN}, site.root)


def test_session_issued_for_other_site_on_same_ip_is_rejected(site, tmp_path):
    path = (tmp_path / "sibling-owner").resolve()
    path.mkdir(mode=0o700)
    sibling = OwnerSessions(path, project="other-qa", instance_id=str(uuid4()), origin=SIBLING)
    token = sibling.login_with_code(sibling.issue_login_code(), request_origin=SIBLING)
    sibling.require(token, request_origin=SIBLING)
    with site.client() as client:
        assert_no_maintenance_access(client, headers(token), site.root)


def test_only_exact_origin_can_reset_and_only_target_credentials_change(site):
    site.seed()
    with site.db.begin() as conn:
        conn.exec_driver_sql("INSERT INTO gift_records VALUES (1, 1, 16.88)")
    token = site.verified()
    password = secrets.token_urlsafe(18)
    target = {"username": "ledger_user", "new_password": password}
    with site.client() as client:
        assert login(client, "ledger_user", site.old_password).status_code == 200
        accounts = client.get(BASE + "/accounts", headers=headers(token))
        assert accounts.json() == {
            "accounts": [
                {"username": "ledger_user", "is_active": True, "is_admin": False},
                {"username": "ledger_admin", "is_active": True, "is_admin": True},
                {"username": "disabled_user", "is_active": False, "is_admin": False},
            ]
        }
        for origin in (None, "null", SIBLING, ORIGIN + "/", ORIGIN.replace("https", "http")):
            response = client.post(
                BASE + "/accounts/reset-password", json=target, headers=headers(token, origin)
            )
            assert response.status_code == 403
        assert client.get("/api/v1/auth/me").status_code == 200
        reset = client.post(BASE + "/accounts/reset-password", json=target, headers=headers(token))
        assert reset.status_code == 200
        assert reset.json() == {"username": "ledger_user", "reset": True}
        assert reset.headers["cache-control"] == "no-store"
        assert password not in reset.text
        assert client.get("/api/v1/auth/me").status_code == 401
        assert login(client, "ledger_user", site.old_password).status_code == 401
        assert login(client, "ledger_user", password).status_code == 200
        assert login(client, "ledger_admin", site.old_password).status_code == 200
        stopped = client.post(
            BASE + "/accounts/reset-password",
            json={"username": "disabled_user", "new_password": password},
            headers=headers(token),
        )
        assert stopped.status_code == 200
        assert login(client, "disabled_user", password).status_code == 401
    with site.db.connect() as conn:
        assert conn.exec_driver_sql("SELECT id, user_id, amount FROM gift_records").all() == [
            (1, 1, 16.88)
        ]


def test_verified_zip_requires_owner_and_the_current_bundle(site):
    import zipfile

    backup = uuid4().hex
    download = site.root / "status/download.zip"
    with zipfile.ZipFile(download, "w") as out:
        for name in (
            "BUNDLE.json",
            "SHA256SUMS",
            "verify.json",
            "backup.json",
            f"backup-{backup}.glgui.age",
        ):
            out.writestr(name, "test")
    result = site.root / "status/result.json"
    record = json.loads(result.read_bytes())
    record.update(
        state="succeeded",
        operation_id=uuid4().hex,
        backup_id=backup,
        decryption_verified=True,
        download_bytes=download.stat().st_size,
    )
    result.write_text(json.dumps(record))
    with TestClient(create_app(site.settings), base_url=ORIGIN) as client:
        url = f"{BASE}/backups/{backup}/download"
        assert client.get(url).status_code == 401
        token = site.store.login_with_code(site.store.issue_login_code(), request_origin=ORIGIN)
        auth = headers(token)
        assert client.get(f"{BASE}/backups/{uuid4().hex}/download", headers=auth).status_code == 404
        response = client.get(url, headers=auth)
        assert response.status_code == 200 and response.content == download.read_bytes()
        assert str(backup) in response.headers["Content-Disposition"]
        assert response.headers["Cache-Control"] == "no-store"
        record["backup_id"] = uuid4().hex
        result.write_text(json.dumps(record))
        assert (
            client.get(f"{BASE}/backups/{record['backup_id']}/download", headers=auth).status_code
            == 409
        )


def test_first_key_enrollment_requires_owner_same_origin_and_single_pending_operation(site):
    from app.services import maintenance_enrollment_protocol as wire

    path = BASE + "/key-enrollment"
    op = uuid4().hex
    binding = {"project": site.project, "instance_id": site.store.binding[1]}
    latest = site.root / "status/enrollment.json"
    latest.write_bytes(wire.encode(dict(binding, state="idle", operation_id=None)))
    auth = headers(site.verified())
    with site.client() as client:
        assert (
            client.post(path, json={"operation_id": op}, headers=headers(site.legacy)).status_code
            == 403
        )
        assert (
            client.post(
                path, json={"operation_id": op}, headers={**auth, "Origin": SIBLING}
            ).status_code
            == 403
        )
        assert (
            client.post(path, json={"operation_id": op}, headers=auth).json()["state"] == "queued"
        )
        assert client.post(path, json={"operation_id": op}, headers=auth).status_code == 202
        assert client.get(path + "/" + op, headers=auth).json()["operation_id"] == op
        assert (
            client.post(path, json={"operation_id": uuid4().hex}, headers=auth).status_code == 409
        )
        assert client.get(path + "/" + uuid4().hex, headers=auth).status_code == 404
        assert client.post(f"{path}/{op}/cancel", json={}, headers=auth).status_code == 202
        assert (site.root / "requests" / (op + ".cancel")).read_text().strip() == op
        assert (site.root / "requests/enroll.request").read_text().strip() == op


def test_enrollment_packet_binds_target_never_accepts_plaintext_and_receipt_retry_is_exact(site):
    import base64

    from app.services import maintenance_enrollment_protocol as wire

    path = BASE + "/key-enrollment"
    op = uuid4().hex
    binding = {"project": site.project, "instance_id": site.store.binding[1]}
    started = dict(
        binding,
        format="gift-ledger-gui-key-target-v1",
        operation_id=uuid4().hex,
        started_at="2026-11-19T12:13:14+00:00",
    )
    target = dict(
        started,
        success=True,
        started_sha256=wire.digest(wire.encode(started)),
        recipient="age1" + "a" * 58,
    )
    status = dict(
        binding, state="awaiting_packet", target=target, registered=False, operation_id=op
    )
    latest = site.root / "status/enrollment.json"
    latest.write_bytes(wire.encode(status))
    (site.root / "requests/enroll.request").write_text(op + "\n")
    cipher = b"age-encryption.org/v1\n" + b"x" * 32
    req = dict(
        binding,
        format="gift-ledger-gui-key-import-request-v1",
        operation_id=op,
        target_recipient=target["recipient"],
        backup_recipient="age1" + "c" * 58,
        ciphertext="key-transfer-" + op + ".age",
        ciphertext_sha256=wire.digest(cipher),
    )
    packet = {"request": req, "ciphertext_b64": base64.b64encode(cipher).decode()}
    auth = headers(site.verified())
    endpoint = f"{path}/{op}/packet"
    with site.client() as client:
        for invalid in (
            {**packet, "identity": "plaintext-forbidden"},
            {**packet, "request": dict(req, project="other")},
            {**packet, "request": dict(req, instance_id=str(uuid4()))},
            {**packet, "request": dict(req, target_recipient=req["backup_recipient"])},
            {**packet, "request": dict(req, operation_id=uuid4().hex)},
            {**packet, "ciphertext_b64": base64.b64encode(b"plaintext").decode()},
        ):
            r = client.post(endpoint, json=invalid, headers=auth)
            assert r.status_code == 400 and "plaintext" not in r.text
        r = client.post(
            endpoint, content=b"x" * 32769, headers={**auth, "Content-Type": "application/json"}
        )
        assert r.status_code == 413
        assert not (site.root / "requests" / (op + ".packet")).exists()
        assert client.post(endpoint, json=packet, headers=auth).status_code == 202
        assert client.post(endpoint, json=packet, headers=auth).status_code == 202
        assert (site.root / "requests" / (op + ".packet")).read_bytes() == wire.encode(packet)
        assert client.post(f"{path}/{op}/cancel", json={}, headers=auth).status_code == 409
        different = {**packet, "request": dict(req, backup_recipient="age1" + "d" * 58)}
        assert client.post(endpoint, json=different, headers=auth).status_code == 409
        receipt = dict(
            binding,
            format="gift-ledger-gui-key-import-completed-v1",
            success=True,
            operation_id=op,
            backup_recipient=req["backup_recipient"],
            request_sha256=wire.digest(wire.encode(req)),
            started_sha256=wire.digest(wire.encode({"operation_id": op, "success": False})),
            identity_sha256="4" * 64,
        )
        latest.write_bytes(wire.encode(dict(status, state="succeeded", receipt=receipt)))
        assert client.post(endpoint, json=packet, headers=auth).status_code == 200
        assert client.post(endpoint, json=different, headers=auth).status_code == 400
        latest.write_bytes(
            wire.encode(
                dict(status, state="succeeded", receipt=dict(receipt, operation_id=uuid4().hex))
            )
        )
        assert client.get(path, headers=auth).status_code == 503
