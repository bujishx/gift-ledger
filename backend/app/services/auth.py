from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.security import (
    UsernamePolicyError,
    hash_password,
    normalize_username,
    validate_new_password,
    verify_password,
    verify_unknown_user_password,
)
from app.db.models import Contact, RelationshipType, User
from app.services.system_dictionary_catalog import SELF_RELATIONSHIP_SYSTEM_CODE


class UsernameAlreadyExistsError(ValueError):
    pass


class AuthenticationFailedError(ValueError):
    pass


def create_user(
    session: Session,
    username: str,
    password: str,
    *,
    is_admin: bool,
) -> User:
    normalized_username = normalize_username(username)
    validate_new_password(password)
    password_hash = hash_password(password)

    try:
        with session.begin():
            existing_id = session.scalar(
                select(User.id).where(User.username == normalized_username)
            )
            if existing_id is not None:
                raise UsernameAlreadyExistsError

            user = User(
                username=normalized_username,
                password_hash=password_hash,
                is_admin=int(is_admin),
                is_active=1,
            )
            session.add(user)
            session.flush()
    except IntegrityError as exc:
        raise UsernameAlreadyExistsError from exc

    return user


def register_user(session: Session, username: str, password: str) -> User:
    return create_user(session, username, password, is_admin=False)


def authenticate_user(session: Session, username: str, password: str) -> User:
    try:
        normalized_username = normalize_username(username)
    except UsernamePolicyError as exc:
        verify_unknown_user_password(password)
        raise AuthenticationFailedError from exc

    user = session.scalar(select(User).where(User.username == normalized_username))
    if user is None:
        verify_unknown_user_password(password)
        raise AuthenticationFailedError
    if not verify_password(password, user.password_hash) or not user.is_active:
        raise AuthenticationFailedError
    return user


def get_active_user(session: Session, user_id: int) -> User | None:
    return session.scalar(select(User).where(User.id == user_id, User.is_active == 1))


def get_self_name(session: Session, user_id: int) -> str | None:
    return session.scalar(
        select(Contact.name)
        .join(RelationshipType, RelationshipType.id == Contact.relation_type_id)
        .where(
            Contact.user_id == user_id,
            RelationshipType.user_id.is_(None),
            RelationshipType.system_code == SELF_RELATIONSHIP_SYSTEM_CODE,
        )
    )
