import socket
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import select
from test_owner_maintenance_api import BASE, ORIGIN, headers
from test_owner_maintenance_api import site as original_site  # noqa: F401

from app.db.registration import DATABASE, invitations, metadata, seed_policy
from app.services import registration
from app.services.registration_limits import AttemptLimitReached, FailureLimiter

PUBLIC = "https://ledger.example:51888"
AUTH = "/api/v1/auth"
PASSWORD = "valid QA password 456"


@pytest.fixture
def site_access(original_site):  # noqa: F811
    site = original_site
    site.settings = site.settings.model_copy(update={"reverse_proxy_origin": PUBLIC})
    site.db = site.db.execution_options(schema_translate_map={DATABASE: None})
    with site.db.begin() as conn:
        metadata.create_all(conn)
        seed_policy(conn, True)
    return site


def signup(client, username, *, origin=PUBLIC, password=PASSWORD, code=None):
    payload = {"username": username, "password": password}
    if code is not None:
        payload["invite_code"] = code
    return client.post(origin + AUTH + "/register", json=payload, headers={"Origin": origin})


def test_install_existing_open_closed_and_policy_persistence(site_access):
    site = site_access
    token = site.verified()
    with site.client() as client:

        def policy():
            return client.get(BASE + "/registration", headers=headers(token)).json()

        assert policy() == {"lan_enabled": True, "public_mode": "invite"}
        assert client.get(ORIGIN + AUTH + "/registration").json() == {"mode": "open"}
        assert client.get(PUBLIC + AUTH + "/registration").json() == {"mode": "invite"}
        assert client.get("https://unknown.test" + AUTH + "/registration").json() == {
            "mode": "closed"
        }
        assert client.get(BASE + "/registration").status_code == 401
        assert (
            client.get(PUBLIC + BASE + "/registration", headers=headers(token)).status_code == 404
        )
        assert (
            client.post(
                BASE + "/registration", json=policy(), headers=headers(token, PUBLIC)
            ).status_code
            == 403
        )
        assert signup(client, "new_local", origin=ORIGIN).status_code == 201
        assert signup(client, "new_public").json()["error"]["code"] == "INVITE_INVALID"
        changed = {"lan_enabled": False, "public_mode": "open"}
        assert (
            client.post(BASE + "/registration", json=changed, headers=headers(token)).json()
            == changed
        )
        assert signup(client, "other_local", origin=ORIGIN).status_code == 403
        assert signup(client, "new_public").status_code == 201
    with site.db.begin() as conn:
        seed_policy(conn, False)
    with site.client() as client:
        assert client.get(BASE + "/registration", headers=headers(token)).json() == changed
        assert client.get(PUBLIC + AUTH + "/registration").headers["cache-control"] == "no-store"
        # An earlier site with legacy registration disabled must initialize closed on both entries.
        with site.db.begin() as conn:
            from app.db.registration import policy as policy_table

            conn.execute(policy_table.delete())
            seed_policy(conn, False)
        assert client.get(BASE + "/registration", headers=headers(token)).json() == {
            "lan_enabled": False,
            "public_mode": "closed",
        }
        result = client.post(
            PUBLIC + AUTH + "/login",
            json={"username": "new_public", "password": PASSWORD},
            headers={"Origin": PUBLIC},
        )
        assert result.status_code == 200


def test_one_use_only_after_successful_registration(site_access):
    site = site_access
    token = site.verified()
    with site.client() as client:

        def issue():
            result = client.post(BASE + "/invitations", json={}, headers=headers(token))
            assert result.status_code == 200
            assert result.headers["cache-control"] == "no-store"
            return result.json()

        invitation = issue()
        code = invitation["code"]
        with site.db.connect() as conn:
            row = conn.execute(select(invitations)).one()
            assert row.expires_at - row.created_at == 48 * 3600
            assert row.code_hash == registration.code_digest(code)
        assert signup(client, "backup_name", origin=ORIGIN).status_code == 201
        assert signup(client, "backup_name", code=code).status_code == 409
        assert signup(client, "invited_name", code=code, password="weak").status_code == 422

        barrier = Barrier(2)

        def contender(name):
            with site.client() as contender_client:
                barrier.wait(timeout=10)
                return signup(contender_client, name, code=code).status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(contender, ("invited_first", "invited_second"))) == [201, 403]
        with site.db.begin() as conn:
            conn.exec_driver_sql("DELETE FROM users WHERE username LIKE 'invited_%'")
        assert (
            signup(client, "invited_again", code=code).json()["error"]["code"] == "INVITE_INVALID"
        )
        revoked = issue()
        assert client.post(
            BASE + "/invitations/revoke", json={"id": revoked["id"]}, headers=headers(token)
        ).json() == {"revoked": True}
        expired = issue()
        with site.db.begin() as conn:
            conn.execute(
                invitations.update().where(invitations.c.id == expired["id"]).values(expires_at=1)
            )
        for bad in (None, "", "wrong", code, revoked["code"], expired["code"]):
            assert (
                signup(client, "invited_again", code=bad).json()["error"]["code"]
                == "INVITE_INVALID"
            )
        entries = client.get(BASE + "/invitations", headers=headers(token))
        assert code not in entries.text and registration.code_digest(code) not in entries.text
        assert {item["status"] for item in entries.json()["invitations"]} == {
            "revoked",
            "expired",
            "used",
        }


def test_limiter_counts_failures_only_and_releases_after_window():
    now = [10]
    limiter = FailureLimiter(clock=lambda: now[0])
    for _ in range(8):
        with pytest.raises(ValueError), limiter.attempt("login", "peer", "user", ValueError):
            raise ValueError
    with (
        pytest.raises(AttemptLimitReached) as raised,
        limiter.attempt("login", "elsewhere", "user", ValueError),
    ):
        pass
    assert raised.value.retry_after == 600
    with limiter.attempt("invite", "peer", "", ValueError):
        pass
    with limiter.attempt("login", "peer", "other", ValueError):
        pass
    now[0] += 600
    with limiter.attempt("login", "peer", "user", ValueError):
        pass


def test_login_and_invitation_rate_limit_http(site_access, monkeypatch):
    monkeypatch.setenv("GIFTLEDGER_AUTH_GATEWAY", "web")
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("192.0.2.10", 0))],
    )
    with site_access.client(peer="192.0.2.10") as client:
        client.headers["X-GiftLedger-Client-IP"] = "192.0.2.51"
        for _ in range(8):
            response = client.post(
                PUBLIC + AUTH + "/login",
                json={"username": "absent", "password": PASSWORD},
                headers={"Origin": PUBLIC},
            )
            assert response.status_code == 401
        denied = client.post(
            PUBLIC + AUTH + "/login",
            json={"username": "absent", "password": PASSWORD},
            headers={"Origin": PUBLIC, "X-Forwarded-For": "192.0.2.5"},
        )
        assert denied.status_code == 429 and int(denied.headers["Retry-After"]) > 0
        assert denied.json()["error"]["code"] == "LOGIN_RATE_LIMITED"
        for number in range(60):
            assert signup(client, f"unregistered_{number}", code="wrong").status_code == 403
        denied = signup(client, "one_more", code="wrong")
        assert denied.status_code == 429 and int(denied.headers["Retry-After"]) > 0
        assert denied.json()["error"]["code"] == "INVITE_RATE_LIMITED"
        client.headers["X-GiftLedger-Client-IP"] = "192.0.2.52"
        assert signup(client, "another_source", code="wrong").status_code == 403
        assert {
            entry.peer for entry in client.app.state.registration_limits.attempts["invite"]
        } == {"192.0.2.51", "192.0.2.52"}


def test_busy_login_is_shared_across_entries_and_workers_without_charging_failures(
    site_access, tmp_path, monkeypatch
):
    shared = tmp_path / "site-app-data"
    shared.mkdir()
    monkeypatch.setenv("GIFTLEDGER_LOGIN_LOCK_DIR", str(shared))
    site_access.seed()
    payload = {"username": "ledger_user", "password": site_access.old_password}
    with site_access.client() as first, site_access.client() as second:
        with first.app.state.registration_limits.password_check():
            for client, origin in ((first, ORIGIN), (second, PUBLIC)):
                busy = client.post(
                    origin + AUTH + "/login", json=payload, headers={"Origin": origin}
                )
                assert busy.status_code == 429 and busy.headers["Retry-After"] == "1"
                assert busy.json()["error"]["code"] == "LOGIN_BUSY"
                assert not client.app.state.registration_limits.attempts["login"]
            assert second.get(PUBLIC + AUTH + "/registration").status_code == 200
        assert (
            second.post(
                PUBLIC + AUTH + "/login", json=payload, headers={"Origin": PUBLIC}
            ).status_code
            == 200
        )
    assert not list(shared.iterdir())


def test_missing_proxy_identity_uses_account_and_site_limits_only():
    limiter = FailureLimiter()
    for number in range(240):
        with pytest.raises(ValueError), limiter.attempt("login", None, f"user{number}", ValueError):
            raise ValueError
    with pytest.raises(AttemptLimitReached), limiter.attempt("login", None, "another", ValueError):
        pass


def test_web_peer_verification_precedes_public_credentials(site_access, monkeypatch):
    monkeypatch.setenv("GIFTLEDGER_AUTH_GATEWAY", "web")
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("192.0.2.10", 0))],
    )
    site_access.seed()
    payload = {"username": "ledger_user", "password": site_access.old_password}
    with (
        site_access.client(peer="192.0.2.11") as other,
        site_access.client(peer="192.0.2.10") as web,
    ):
        for client, forwarded in ((other, "site"), (web, "invalid")):
            reply = client.post(
                PUBLIC + AUTH + "/login",
                json=payload,
                headers={"Origin": PUBLIC, "X-GiftLedger-Client-IP": forwarded},
            )
            assert reply.status_code == 403
        web.headers["X-GiftLedger-Client-IP"] = "site"
        assert (
            web.post(PUBLIC + AUTH + "/login", json=payload, headers={"Origin": PUBLIC}).status_code
            == 200
        )
        assert (
            other.post(
                ORIGIN + AUTH + "/login", json=payload, headers={"Origin": ORIGIN}
            ).status_code
            == 200
        )


def test_shared_hash_slot_preserves_invitation_and_failure_counters(site_access):
    site_access.seed()
    with site_access.client() as client:
        inv = client.post(BASE + "/invitations", json={}, headers=headers(site_access.verified()))
        assert inv.status_code == 200
        code = inv.json()["code"]
        with client.app.state.registration_limits.password_check():
            reply = signup(client, "new_member", code=code)
            assert reply.status_code == 429
            assert not client.app.state.registration_limits.attempts["invite"]
        assert signup(client, "new_member", code=code).status_code == 201
