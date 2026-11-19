import hashlib
import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from uuid import uuid4

import pytest

from app.core.security import PasswordPolicyError
from app.services import maintenance_sessions
from app.services.maintenance_sessions import (
    ATTEMPT_SECONDS,
    CODE_SECONDS,
    DATABASE,
    MAX_ATTEMPTS,
    MAX_SESSIONS,
    SESSION_SECONDS,
    OwnerSessions,
    SessionDenied,
    SessionRateLimited,
    SessionStoreError,
)

URL = "https://192.0.2.9:16449"


@pytest.fixture
def setup(tmp_path):
    directory = (tmp_path / "sessions").resolve()
    directory.mkdir(mode=0o700)
    moment = [2_000_000_000]
    kw = {
        "project": "gift-test",
        "instance_id": str(uuid4()),
        "origin": URL,
        "clock": lambda: moment[0],
    }
    return directory, kw, moment


def granted(sessions):
    return sessions.login_with_code(sessions.issue_login_code(), request_origin=URL)


def test_storage_and_restart_keep_only_digest(setup):
    directory, kw, _ = setup
    first = OwnerSessions(directory, **kw)
    token = granted(first)
    db_file = directory / DATABASE
    assert db_file.stat().st_mode & 0o777 == 0o600
    assert token.encode("ascii") not in db_file.read_bytes()
    with closing(sqlite3.connect(db_file)) as db:
        saved = db.execute("SELECT digest FROM sessions").fetchall()
    assert saved == [(hashlib.sha256(token.encode("ascii")).digest(),)]
    OwnerSessions(directory, **kw).require(token)
    with pytest.raises(SessionDenied):
        first.require("a" * 64)  # A phase-one worker token is a different credential.


@pytest.mark.parametrize("changed", ["project", "instance_id", "origin"])
def test_binding_survives_another_site_using_same_directory(setup, changed):
    directory, kw, _ = setup
    original = OwnerSessions(directory, **kw)
    a = granted(original)
    other = {
        "project": "other-project",
        "instance_id": str(uuid4()),
        "origin": "https://192.0.2.9:16450",
    }
    with pytest.raises(SessionStoreError, match="binding_mismatch"):
        OwnerSessions(directory, **{**kw, changed: other[changed]})
    original.require(a)
    second = directory.parent / "second"
    second.mkdir(mode=0o700)
    other_store = OwnerSessions(second, **{**kw, changed: other[changed]})
    with pytest.raises(SessionDenied):
        other_store.require(a)
    source_code = original.issue_login_code()
    with pytest.raises(SessionDenied):
        other_store.login_with_code(source_code, request_origin=other_store.origin)
    original.require(original.login_with_code(source_code, request_origin=URL))


def test_upgrade_of_session_only_store_is_bound_to_original_site(setup):
    directory, kw, _ = setup
    original = OwnerSessions(directory, **kw)
    token = granted(original)
    with closing(sqlite3.connect(directory / DATABASE)) as db:
        db.execute("DROP TABLE owner")
        db.execute("PRAGMA user_version=1")
        db.commit()
    with pytest.raises(SessionStoreError, match="binding_mismatch"):
        OwnerSessions(directory, **{**kw, "project": "another-project"})
    with closing(sqlite3.connect(directory / DATABASE)) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
    reopened = OwnerSessions(directory, **kw)
    with pytest.raises(SessionDenied):
        reopened.require(token)
    reopened.require(granted(reopened))


@pytest.mark.parametrize(
    "invalid",
    [
        "http://192.0.2.9:16449",
        URL + "/",
        URL + "#fragment",
        URL + "?q=1",
        "https://someone@192.0.2.9:16449",
        "https://with space:16449",
        "https://example.com:443",
        "https://example.com:",
        "https://example.com:65536",
        "https://127.1",
        "https://0x7f000001",
    ],
)
def test_origin_configuration(setup, invalid):
    directory, kw, _ = setup
    with pytest.raises(SessionStoreError):
        OwnerSessions(directory, **{**kw, "origin": invalid})


def test_cross_origin_and_credentials_never_reach_verifier(setup):
    directory, kw, _ = setup
    sessions = OwnerSessions(directory, **kw)
    wrong = "https://192.0.2.9:17443"
    with pytest.raises(SessionDenied):
        sessions.login_with_code("not a code", request_origin=wrong)
    token = granted(sessions)
    sessions.require(token, request_origin=URL, mutation=True)
    for origin in [None, wrong, "null", URL + "/"]:
        with pytest.raises(SessionDenied, match="origin_refused"):
            sessions.require(token, request_origin=origin, mutation=True)
    with pytest.raises(SessionDenied, match="origin_refused"):
        sessions.require(token, request_origin=wrong)
    sessions.require(token)
    with pytest.raises(SessionDenied):
        sessions.logout(token, request_origin=wrong)
    sessions.require(token)


def test_logout_expiry_and_clock_roll_back(setup):
    directory, kw, moment = setup
    owner = OwnerSessions(directory, **kw)
    a, b = granted(owner), granted(owner)
    assert a != b
    owner.logout(a, request_origin=URL)
    with pytest.raises(SessionDenied):
        owner.require(a)
    moment[0] -= 1
    with pytest.raises(SessionDenied):
        owner.require(b)
    moment[0] += SESSION_SECONDS
    owner.require(b)
    moment[0] += 1
    with pytest.raises(SessionDenied):
        owner.require(b)


def test_failed_admissions_persist_and_valid_bearers_stay_usable(setup):
    directory, kw, moment = setup
    owner = OwnerSessions(directory, **kw)
    good = granted(owner)
    for _ in range(MAX_ATTEMPTS):
        with pytest.raises(SessionDenied):
            owner.login_with_code("bad", request_origin=URL)
        with pytest.raises(SessionDenied):
            owner.require("invalid")
    reopened = OwnerSessions(directory, **kw)
    with pytest.raises(SessionRateLimited):
        reopened.login_with_code("bad", request_origin=URL)
    with pytest.raises(SessionRateLimited):
        reopened.require("invalid")
    reopened.require(good)
    reopened.logout(good, request_origin=URL)
    moment[0] += ATTEMPT_SECONDS
    with pytest.raises(SessionDenied):
        reopened.login_with_code("bad", request_origin=URL)
    reopened.require(granted(reopened))


def test_pending_login_does_not_survive_revocation(setup, monkeypatch):
    directory, kw, _ = setup
    owner = OwnerSessions(directory, **kw)
    pw = "test-for-pending-owner-login"
    existing = owner.set_password_with_code(owner.issue_login_code(), pw, request_origin=URL)
    entered = threading.Event()
    release = threading.Event()
    check_password = maintenance_sessions.verify_password

    def slow_verifier(password, expected):
        entered.set()
        assert release.wait(5)
        return check_password(password, expected)

    monkeypatch.setattr(maintenance_sessions, "verify_password", slow_verifier)
    with ThreadPoolExecutor(max_workers=1) as threads:
        pending = threads.submit(owner.login_with_password, pw, request_origin=URL)
        assert entered.wait(5)
        OwnerSessions(directory, **kw).revoke_all()
        release.set()
        with pytest.raises(SessionDenied):
            pending.result(5)
    with pytest.raises(SessionDenied):
        owner.require(existing)
    owner.require(granted(owner))


def test_concurrent_first_open_and_rate_limit_across_connections(setup, monkeypatch):
    directory, kw, _ = setup
    barrier = threading.Barrier(10)
    verified = []
    lock = threading.Lock()

    def dummy(_):
        with lock:
            verified.append(1)

    monkeypatch.setattr(maintenance_sessions, "verify_unknown_user_password", dummy)

    def attempt(_):
        barrier.wait(5)
        store = OwnerSessions(directory, **kw)
        try:
            store.login_with_password("failed-password", request_origin=URL)
        except SessionRateLimited:
            return "limited"
        except SessionDenied:
            return "denied"
        pytest.fail("Bad credential succeeded")

    with ThreadPoolExecutor(max_workers=10) as threads:
        result = list(threads.map(attempt, range(10)))
    assert len(verified) == result.count("denied") == MAX_ATTEMPTS
    assert result.count("limited") == 10 - MAX_ATTEMPTS


def test_active_session_count_is_bounded_and_tokens_deduplicated(setup):
    directory, kw, _ = setup
    owner = OwnerSessions(directory, **kw)
    batch = [granted(owner) for _ in range(MAX_SESSIONS + 1)]
    assert len(set(batch)) == len(batch)
    with pytest.raises(SessionDenied):
        owner.require(batch[0])
    for token in batch[1:]:
        owner.require(token)


def test_wrong_file_or_permissions_fail_closed(setup):
    directory, kw, _ = setup
    os.chmod(directory, 0o755)
    with pytest.raises(SessionStoreError, match="permissions_invalid"):
        OwnerSessions(directory, **kw)
    os.chmod(directory, 0o700)
    unrelated = directory.parent / "unrelated"
    unrelated.touch(mode=0o600)
    (directory / DATABASE).symlink_to(unrelated)
    with pytest.raises(SessionStoreError):
        OwnerSessions(directory, **kw)
    assert unrelated.stat().st_size == 0
    (directory / DATABASE).unlink()
    with closing(sqlite3.connect(directory / DATABASE)) as db:
        db.execute("CREATE TABLE unrelated(a TEXT)")
        db.commit()
    (directory / DATABASE).chmod(0o600)
    with pytest.raises(SessionStoreError, match="store_unknown"):
        OwnerSessions(directory, **kw)


def test_rotating_and_expiring_server_code(setup):
    directory, kw, moment = setup
    owner = OwnerSessions(directory, **kw)
    old, current = owner.issue_login_code(), owner.issue_login_code()
    assert len(current) == 19 and current.count("-") == 3 and old != current
    for raw in (current, current.replace("-", "")):
        assert raw.encode("ascii") not in (directory / DATABASE).read_bytes()
    with pytest.raises(SessionDenied):
        owner.login_with_code(old, request_origin=URL)
    moment[0] += CODE_SECONDS - 1
    owner.require(owner.login_with_code(current.lower().replace("-", " "), request_origin=URL))
    with pytest.raises(SessionDenied):
        owner.login_with_code(current, request_origin=URL)
    fresh = owner.issue_login_code()
    moment[0] += CODE_SECONDS
    with pytest.raises(SessionDenied):
        owner.login_with_code(fresh, request_origin=URL)
    owner.require(granted(owner))


def test_code_and_password_can_both_log_in_and_host_code_manages_password(setup):
    directory, kw, _ = setup
    owner = OwnerSessions(directory, **kw)
    old = granted(owner)
    code = owner.issue_login_code()
    pw = "test-for-optional-owner-password"
    with pytest.raises(PasswordPolicyError):
        owner.set_password_with_code(code, "12345678", request_origin=URL)
    with pytest.raises(PasswordPolicyError):
        owner.set_password_with_code(code, None, request_origin=URL)
    with pytest.raises(SessionDenied):
        owner.set_password_with_code(old, pw, request_origin=URL)
    with pytest.raises(SessionDenied):
        owner.set_password_with_code(code, pw, request_origin=URL + "/")
    changed = owner.set_password_with_code(code, pw, request_origin=URL)
    assert pw.encode("utf-8") not in (directory / DATABASE).read_bytes()
    with closing(sqlite3.connect(directory / DATABASE)) as db:
        assert db.execute("SELECT password_hash FROM owner").fetchone()[0].startswith("$argon2")
    with pytest.raises(SessionDenied):
        owner.require(old)
    owner.require(changed)
    for _ in range(2):
        owner.require(owner.login_with_password(pw, request_origin=URL))
    reopened = OwnerSessions(directory, **kw)
    current = granted(reopened)
    with pytest.raises(SessionDenied):
        reopened.remove_password_with_code(current, request_origin=URL)
    replacement = "new-owner-password-for-testing"
    reset = reopened.set_password_with_code(
        reopened.issue_login_code(), replacement, request_origin=URL
    )
    with pytest.raises(SessionDenied):
        reopened.require(current)
    with pytest.raises(SessionDenied):
        reopened.login_with_password(pw, request_origin=URL)
    reopened.require(reset)
    reopened.require(reopened.login_with_password(replacement, request_origin=URL))
    removed = reopened.remove_password_with_code(reopened.issue_login_code(), request_origin=URL)
    with pytest.raises(SessionDenied):
        reopened.require(reset)
    with pytest.raises(SessionDenied):
        reopened.login_with_password(replacement, request_origin=URL)
    reopened.require(removed)
    reopened.require(granted(reopened))


def test_lost_code_reissue_resets_only_code_attempts(setup, monkeypatch):
    directory, kw, _ = setup
    owner = OwnerSessions(directory, **kw)
    monkeypatch.setattr(maintenance_sessions, "verify_unknown_user_password", lambda _: None)
    for _ in range(MAX_ATTEMPTS):
        with pytest.raises(SessionDenied):
            owner.login_with_password("wrong-password", request_origin=URL)
        with pytest.raises(SessionDenied):
            owner.login_with_code("wrong", request_origin=URL)
    with pytest.raises(SessionRateLimited):
        owner.login_with_code("wrong", request_origin=URL)
    owner.require(granted(owner))
    with pytest.raises(SessionRateLimited):
        owner.login_with_password("wrong-password", request_origin=URL)


def test_code_is_consumed_exactly_once_across_concurrent_requests(setup):
    directory, kw, _ = setup
    owner = OwnerSessions(directory, **kw)
    code = owner.issue_login_code()
    gate = threading.Barrier(5)

    def redeem(_):
        other = OwnerSessions(directory, **kw)
        gate.wait(5)
        try:
            return other.login_with_code(code, request_origin=URL)
        except SessionDenied:
            return None

    with ThreadPoolExecutor(max_workers=5) as pool:
        actual = [s for s in pool.map(redeem, range(5)) if s is not None]
    assert len(actual) == 1
    owner.require(actual[0])


def test_rotated_code_cannot_change_password_during_hash(setup, monkeypatch):
    directory, kw, _ = setup
    owner = OwnerSessions(directory, **kw)
    old_code = owner.issue_login_code()
    saved_hash = maintenance_sessions.hash_password
    entered, release = threading.Event(), threading.Event()

    def slow_hash(value):
        entered.set()
        assert release.wait(5)
        return saved_hash(value)

    monkeypatch.setattr(maintenance_sessions, "hash_password", slow_hash)
    with ThreadPoolExecutor(max_workers=1) as threads:
        pending = threads.submit(
            owner.set_password_with_code, old_code, "a-valid-password", request_origin=URL
        )
        assert entered.wait(5)
        current = owner.issue_login_code()
        release.set()
        with pytest.raises(SessionDenied):
            pending.result(5)
    with pytest.raises(SessionDenied):
        owner.login_with_password("a-valid-password", request_origin=URL)
    owner.require(owner.login_with_code(current, request_origin=URL))


def test_code_returns_if_session_creation_does_not_commit(setup, monkeypatch):
    directory, kw, _ = setup
    owner = OwnerSessions(directory, **kw)
    code = owner.issue_login_code()

    def fail(_db, _now):
        raise RuntimeError("simulated insertion failure")

    with monkeypatch.context() as m:
        m.setattr(owner, "_issue", fail)
        with pytest.raises(RuntimeError, match="simulated"):
            owner.set_password_with_code(code, "does-not-commit", request_origin=URL)
    with pytest.raises(SessionDenied):
        owner.login_with_password("does-not-commit", request_origin=URL)
    owner.require(owner.login_with_code(code, request_origin=URL))
