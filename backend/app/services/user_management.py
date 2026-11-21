from dataclasses import dataclass

from sqlalchemy import delete, exists, or_, select
from sqlalchemy.orm import Session

from app.core.security import hash_password, normalize_username, validate_new_password
from app.db.models import (
    Contact,
    EventRecord,
    EventType,
    GiftRecord,
    KinshipType,
    LegacyImportBatch,
    RelationshipType,
    User,
)
from app.services.auth import create_user


class UserNotFoundError(ValueError):
    pass


class AccountHasDataError(ValueError):
    pass


def has_business_data(user_id):
    # A self profile is a Contact. Archived contacts and dictionary entries also count.
    return or_(
        *(
            exists().where(model.user_id == user_id)
            for model in (
                Contact,
                EventRecord,
                GiftRecord,
                RelationshipType,
                KinshipType,
                EventType,
                LegacyImportBatch,
            )
        )
    )


@dataclass(frozen=True, slots=True)
class BusinessAccountSummary:
    username: str
    is_active: bool
    is_admin: bool


def list_business_accounts(session: Session) -> list[BusinessAccountSummary]:
    rows = session.execute(
        select(User.username, User.is_active, User.is_admin).order_by(User.id)
    ).all()
    return [
        BusinessAccountSummary(name, is_active=bool(active), is_admin=bool(admin))
        for name, active, admin in rows
    ]


def create_admin_user(session: Session, username: str, password: str) -> User:
    return create_user(session, username, password, is_admin=True)


def _locked_user_by_username(session: Session, username: str) -> User | None:
    normalized_username = normalize_username(username)
    return session.scalar(
        select(User).where(User.username == normalized_username).with_for_update()
    )


def reset_user_password(session: Session, username: str, new_password: str) -> User:
    validate_new_password(new_password)
    new_password_hash = hash_password(new_password)
    try:
        user = _locked_user_by_username(session, username)
        if user is None:
            raise UserNotFoundError
        user.password_hash = new_password_hash
        session.flush()
        session.commit()
        return user
    except Exception:
        session.rollback()
        raise


def set_user_active(
    session: Session, username: str, *, is_active: bool, expected_id: int | None = None
) -> User:
    try:
        user = _locked_user_by_username(session, username)
        if user is None or (expected_id is not None and user.id != expected_id):
            raise UserNotFoundError
        user.is_active = int(is_active)
        session.flush()
        session.commit()
        return user
    except Exception:
        session.rollback()
        raise


def delete_empty_account(session: Session, username: str, expected_id: int) -> None:
    with session.begin():
        user = _locked_user_by_username(session, username)
        if user is None or user.id != expected_id:
            raise UserNotFoundError
        # The parent row lock serializes profile creation and FK-backed writes. Check again
        # in the DELETE, so a stale UI or an earlier transaction snapshot cannot authorize it.
        result = session.execute(
            delete(User)
            .where(User.id == expected_id, ~has_business_data(User.id))
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            raise AccountHasDataError
