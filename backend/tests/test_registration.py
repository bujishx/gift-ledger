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


def test_login_and_invitation_rate_limit_http(site_access):
    with site_access.client() as client:
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
