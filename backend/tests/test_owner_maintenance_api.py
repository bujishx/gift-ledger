import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
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
        conn.exec_driver_sql("CREATE TABLE event_records(id INTEGER PRIMARY KEY, user_id INTEGER)")
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
            "instance": json.loads((site.root / "owner-site.json").read_bytes())["instance_id"],
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


def test_overview_usage_and_last_successful_login_do_not_expose_ledger_or_other_sites(site):
    site.seed()
    token = site.verified()
    status = site.root / "status/overview.json"
    now = datetime.now(UTC)
    bid = uuid4().hex
    sample = dict(
        format="giftledger-owner-overview-v1",
        project=site.project,
        instance=site.store.binding[1],
        sampled_at=now.isoformat(),
        web_status="ok",
        backup_state="succeeded",
        backup_id=bid,
        decryption_verified=True,
        backup_checked_at=now.isoformat(),
        ledger_free_bytes=4321000,
        backup_free_bytes=1234000,
        private_note="fixture-private-note",
        certificate_expires_at=(now + timedelta(days=57, hours=1)).isoformat(),
    )
    status.write_text(json.dumps(sample))
    with site.db.begin() as sql:
        sql.exec_driver_sql(
            "INSERT INTO relationship_types VALUES(1,NULL,'relationship.self'),(2,NULL,'other')"
        )
        sql.exec_driver_sql(
            "INSERT INTO contacts VALUES('本人样稿',1,1),('不得返回',1,2),('其他',2,2)"
        )
        sql.exec_driver_sql("INSERT INTO event_records VALUES(1,1),(2,1),(3,2)")
        sql.exec_driver_sql("INSERT INTO gift_records VALUES(1,1,51.82),(2,2,208)")
    with site.client() as c:
        owner = headers(token)
        for endpoint in ("overview", "usage/0"):
            assert c.get(f"{BASE}/{endpoint}", headers=headers(site.legacy)).status_code == 403
        result = c.get(BASE + "/overview", headers=owner)
        assert result.status_code == 200 and result.headers["cache-control"] == "no-store"
        data = result.json()
        assert data["totals"] == dict(users=3, contacts=3, events=3, entries=2)
        assert data["database"]["version"] and data["database"]["schema_revision"] is None
        assert data["service"] == dict(web="ok", api="ok", database="ok")
        assert data["certificate"]["remaining_days"] == 57 and data["backup"]["backup_id"] == bid
        assert (
            data["storage"]["backup_free_bytes"] == 1234000
            and "fixture-private-note" not in result.text
        )
        assert login(c, "ledger_user", "wrong").status_code == 401
        before = c.get(BASE + "/usage/0", headers=owner).json()
        assert all(user["last_login_at"] is None for user in before["users"])
        assert before["users"][0] == dict(
            username="ledger_user",
            display_name="本人样稿",
            is_active=True,
            contacts=2,
            events=2,
            entries=1,
            last_login_at=None,
        )
        assert login(c, "ledger_user", site.old_password).status_code == 200
        for endpoint in ("overview", "usage/0"):
            assert c.get(f"{BASE}/{endpoint}").status_code == 401
        seen = c.get(BASE + "/usage/0", headers=owner)
        assert (
            "不得返回" not in seen.text and "51.82" not in seen.text and "password" not in seen.text
        )
        assert datetime.fromisoformat(seen.json()["users"][0]["last_login_at"]).tzinfo
        assert seen.json()["users"][1]["last_login_at"] is None
        assert len(c.get(BASE + "/usage/2", headers=owner).json()["users"]) == 1
        for replacement in (
            dict(sample, instance=str(uuid4())),
            dict(sample, sampled_at=(now - timedelta(minutes=6)).isoformat()),
            dict(sample, sampled_at=(now + timedelta(days=1)).isoformat()),
        ):
            status.write_text(json.dumps(replacement))
            stale = c.get(BASE + "/overview", headers=owner).json()
            assert stale["backup"]["state"] == "unknown" and stale["service"]["web"] == "unknown"
            assert (
                stale["storage"]["backup_free_bytes"] is None and stale["totals"] == data["totals"]
            )


def test_stage_one_bearer_has_no_authority_once_owner_sessions_are_enabled(site):
    with site.client() as client:
        assert_no_maintenance_access(client, headers(site.legacy), site.root)
        assert client.get(BASE + "/session", headers=headers(site.verified())).status_code == 200


def test_overview_retains_host_results_when_database_is_down_and_reports_newer_web_failure(site):
    owner = headers(site.verified())
    now = datetime.now(UTC)
    previous = now - timedelta(seconds=30)
    status = site.root / "status"
    (status / "overview.json").write_text(
        json.dumps(
            {
                "format": "giftledger-owner-overview-v1",
                "project": site.project,
                "instance": site.store.binding[1],
                "sampled_at": now.isoformat(),
                "backup_state": "succeeded",
                "backup_id": uuid4().hex,
                "decryption_verified": True,
                "backup_checked_at": previous.isoformat(),
                "web_status": "ok",
                "backup_free_bytes": 560_000_000,
            }
        )
    )
    op = uuid4().hex
    task = dict(project=site.project, operation_id=op, state="failed")
    (status / "backup-observation.json").write_text(
        json.dumps(dict(task, observed_at=now.isoformat()))
    )
    (status / "result.json").write_text(
        json.dumps(dict(task, format="giftledger-maintenance-status-v1"))
    )
    with (
        site.client() as c,
        patch.object(Session, "scalar", side_effect=OperationalError("SELECT 1", (), None)),
    ):
        data = c.get(BASE + "/overview", headers=owner).json()
        assert data["service"] == {"web": "ok", "api": "ok", "database": "unavailable"}
        assert set(data["totals"].values()) == {None}
        assert data["storage"]["backup_free_bytes"] == 560_000_000
        assert data["backup"]["state"] == "failed" and data["backup"]["decryption_verified"] is None
        (site.root / "requests/backup.request").write_text(uuid4().hex + "\n")
        assert c.get(BASE + "/overview", headers=owner).json()["backup"]["state"] == "queued"


def test_usage_cursor_covers_unique_accounts_without_loading_passwords_or_duplicate_counts(site):
    site.seed()
    with site.db.begin() as db:
        db.exec_driver_sql(
            "INSERT INTO users(id,username,is_active,password_hash,is_admin) "
            "VALUES(?,?,1,'unused',0)",
            [(i, f"user{i}") for i in range(4, 58)],
        )
    owner = headers(site.verified())
    with site.client() as c:
        first = c.get(BASE + "/usage/0", headers=owner).json()
        second = c.get(BASE + "/usage/" + str(first["next_after"]), headers=owner).json()
    assert len(first["users"]) == 50 and len(second["users"]) == 7 and second["next_after"] is None
    assert len({row["username"] for row in first["users"] + second["users"]}) == 57


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
