from unittest.mock import MagicMock

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.security import hash_password
from app.db.models import User
from app.services.auth import (
    AuthenticationFailedError,
    UsernameAlreadyExistsError,
    authenticate_user,
    get_active_user,
    get_self_name,
    register_user,
)


def test_register_creates_only_a_normal_active_user() -> None:
    session = MagicMock(spec=Session)
    session.scalar.return_value = None
    captured_user = None

    def capture_user(user: User) -> None:
        nonlocal captured_user
        captured_user = user

    def assign_id() -> None:
        assert captured_user is not None
        captured_user.id = 12

    session.add.side_effect = capture_user
    session.flush.side_effect = assign_id

    user = register_user(session, "New_User", "a valid 密码 123")

    assert user.id == 12
    assert user.username == "new_user"
    assert user.is_admin == 0
    assert user.is_active == 1
    assert user.password_hash != "a valid 密码 123"
    assert session.add.call_count == 1


def test_register_maps_precheck_and_concurrent_duplicates_to_same_error() -> None:
    session = MagicMock(spec=Session)
    session.scalar.return_value = 9
    with pytest.raises(UsernameAlreadyExistsError):
        register_user(session, "taken_name", "a valid password 456")
    session.add.assert_not_called()

    concurrent_session = MagicMock(spec=Session)
    concurrent_session.scalar.return_value = None
    concurrent_session.flush.side_effect = IntegrityError("duplicate", {}, Exception())
    with pytest.raises(UsernameAlreadyExistsError):
        register_user(concurrent_session, "new_name", "a valid password 789")


def test_login_is_case_normalized_and_rejects_wrong_or_inactive_accounts() -> None:
    user = User(
        id=3,
        username="some_user",
        password_hash=hash_password("correct password"),
        is_admin=0,
        is_active=1,
    )
    session = MagicMock(spec=Session)
    session.scalar.return_value = user

    assert authenticate_user(session, "Some_User", "correct password") is user

    with pytest.raises(AuthenticationFailedError):
        authenticate_user(session, "Some_User", "wrong password")

    user.is_active = 0
    with pytest.raises(AuthenticationFailedError):
        authenticate_user(session, "Some_User", "correct password")


def test_unknown_and_malformed_usernames_share_generic_login_failure() -> None:
    session = MagicMock(spec=Session)
    session.scalar.return_value = None

    with pytest.raises(AuthenticationFailedError):
        authenticate_user(session, "unknown_user", "some password")
    with pytest.raises(AuthenticationFailedError):
        authenticate_user(session, "bad-name", "some password")


def test_active_user_lookup_includes_active_status_filter() -> None:
    session = MagicMock(spec=Session)
    session.scalar.return_value = None

    assert get_active_user(session, 5) is None
    statement = session.scalar.call_args.args[0]
    assert "users.id" in str(statement)
    assert "users.is_active" in str(statement)


def test_self_name_uses_stable_relationship_code() -> None:
    session = MagicMock(spec=Session)
    session.scalar.return_value = "张三"

    assert get_self_name(session, 7) == "张三"
    query = session.scalar.call_args.args[0]
    statement = str(query)
    assert "contacts.user_id" in statement
    assert "relationship_types.system_code" in statement
    assert "relationship.self" in query.compile().params.values()
