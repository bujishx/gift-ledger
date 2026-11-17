from unittest.mock import MagicMock

import pytest
from pydantic import SecretStr
from sqlalchemy.orm import Session

from app.core.security import (
    PasswordPolicyError,
    access_token_matches_password,
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)
from app.db.models import User
from app.services.user_management import (
    UserNotFoundError,
    create_admin_user,
    reset_user_password,
    set_user_active,
)

JWT_SECRET = SecretStr("0123456789abcdef0123456789abcdef")


def user() -> User:
    return User(
        id=9,
        username="managed_user",
        password_hash=hash_password("old password"),
        is_admin=0,
        is_active=1,
    )


def test_create_admin_reuses_account_rules_but_sets_admin_server_side() -> None:
    session = MagicMock(spec=Session)
    session.scalar.return_value = None
    captured_user = None

    def capture_user(created_user: User) -> None:
        nonlocal captured_user
        captured_user = created_user

    def assign_id() -> None:
        assert captured_user is not None
        captured_user.id = 10

    session.add.side_effect = capture_user
    session.flush.side_effect = assign_id

    created = create_admin_user(session, "First_Admin", "secure 管理密码 123")

    assert created.id == 10
    assert created.username == "first_admin"
    assert created.is_admin == 1
    assert created.is_active == 1
    assert verify_password("secure 管理密码 123", created.password_hash)


def test_reset_password_locks_user_and_invalidates_existing_jwt() -> None:
    session = MagicMock(spec=Session)
    managed_user = user()
    old_hash = managed_user.password_hash
    old_token = create_access_token(managed_user.id, JWT_SECRET, old_hash)
    old_claims = decode_access_token(old_token, JWT_SECRET)
    session.scalar.return_value = managed_user

    result = reset_user_password(session, "Managed_User", "new secure 密码 456")

    assert result is managed_user
    assert result.password_hash != old_hash
    assert verify_password("new secure 密码 456", result.password_hash)
    assert access_token_matches_password(old_claims, result.password_hash, JWT_SECRET) is False
    statement = str(session.scalar.call_args.args[0])
    assert "users.username" in statement
    assert "FOR UPDATE" in statement
    session.flush.assert_called_once()
    session.commit.assert_called_once()


def test_reset_password_rejects_weak_password_before_database_access() -> None:
    session = MagicMock(spec=Session)

    with pytest.raises(PasswordPolicyError):
        reset_user_password(session, "managed_user", "12345678")

    session.scalar.assert_not_called()
    session.commit.assert_not_called()


def test_missing_user_rolls_back_management_changes() -> None:
    reset_session = MagicMock(spec=Session)
    reset_session.scalar.return_value = None
    with pytest.raises(UserNotFoundError):
        reset_user_password(reset_session, "missing_user", "new secure password")
    reset_session.rollback.assert_called_once()

    active_session = MagicMock(spec=Session)
    active_session.scalar.return_value = None
    with pytest.raises(UserNotFoundError):
        set_user_active(active_session, "missing_user", is_active=False)
    active_session.rollback.assert_called_once()


def test_set_user_active_changes_only_active_flag() -> None:
    session = MagicMock(spec=Session)
    managed_user = user()
    original_hash = managed_user.password_hash
    session.scalar.return_value = managed_user

    result = set_user_active(session, "MANAGED_USER", is_active=False)

    assert result is managed_user
    assert result.is_active == 0
    assert result.is_admin == 0
    assert result.password_hash == original_hash
    session.flush.assert_called_once()
    session.commit.assert_called_once()
