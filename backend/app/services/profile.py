from sqlalchemy import and_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import Contact, RelationshipType, User
from app.schemas.dictionaries import DictionaryCategory
from app.services.contacts import (
    ensure_contact_identity_available,
    raise_identity_conflict_after_integrity_error,
)
from app.services.dictionaries import get_system_dictionary_item
from app.services.system_dictionary_catalog import (
    SELF_RELATIONSHIP_FIXED_ID,
    SELF_RELATIONSHIP_SYSTEM_CODE,
)


class ProfileValidationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ProfileAlreadyExistsError(ValueError):
    pass


class ProfileNotFoundError(ValueError):
    pass


class AccountUnavailableError(ValueError):
    pass


class SelfRelationshipUnavailableError(RuntimeError):
    pass


def validate_profile_name(name: str) -> str:
    normalized = name.strip()
    if not normalized:
        raise ProfileValidationError("INVALID_PROFILE_NAME", "本人真实姓名不能为空")
    if len(normalized) > 50:
        raise ProfileValidationError("PROFILE_NAME_TOO_LONG", "本人真实姓名最多允许 50 个字符")
    return normalized


def normalize_profile_location(location: str | None) -> str | None:
    if location is None:
        return None
    normalized = location.strip()
    if not normalized:
        return None
    if len(normalized) > 50:
        raise ProfileValidationError("PROFILE_LOCATION_TOO_LONG", "常住地最多允许 50 个字符")
    return normalized


def get_self_contact(session: Session, user_id: int) -> Contact | None:
    return session.scalar(
        select(Contact)
        .join(
            RelationshipType,
            and_(
                RelationshipType.id == Contact.relation_type_id,
                RelationshipType.user_id.is_(None),
                RelationshipType.system_code == SELF_RELATIONSHIP_SYSTEM_CODE,
            ),
        )
        .where(Contact.user_id == user_id)
    )


def _locked_active_user(session: Session, user_id: int) -> User | None:
    return session.scalar(
        select(User).where(User.id == user_id, User.is_active == 1).with_for_update()
    )


def _locked_self_contact(session: Session, user_id: int) -> Contact | None:
    return session.scalar(
        select(Contact)
        .join(
            RelationshipType,
            and_(
                RelationshipType.id == Contact.relation_type_id,
                RelationshipType.user_id.is_(None),
                RelationshipType.system_code == SELF_RELATIONSHIP_SYSTEM_CODE,
            ),
        )
        .where(Contact.user_id == user_id)
        .with_for_update()
    )


def create_self_profile(
    session: Session,
    user_id: int,
    name: str,
    location: str | None,
) -> Contact:
    validated_name = validate_profile_name(name)
    normalized_location = normalize_profile_location(location)

    try:
        user = _locked_active_user(session, user_id)
        if user is None:
            raise AccountUnavailableError

        self_type = get_system_dictionary_item(
            session,
            DictionaryCategory.RELATIONSHIP,
            SELF_RELATIONSHIP_SYSTEM_CODE,
            require_active=True,
        )
        if self_type is None or self_type.id != SELF_RELATIONSHIP_FIXED_ID:
            raise SelfRelationshipUnavailableError
        if _locked_self_contact(session, user_id) is not None:
            raise ProfileAlreadyExistsError

        identity_hash = ensure_contact_identity_available(
            session,
            user_id,
            name=validated_name,
            location=normalized_location,
            remark=None,
        )

        contact = Contact(
            user_id=user_id,
            name=validated_name,
            relation_type_id=self_type.id,
            location=normalized_location,
            phone=None,
            remark=None,
            identity_hash=identity_hash,
            is_active=1,
        )
        session.add(contact)
        session.flush()
        session.commit()
        return contact
    except IntegrityError as exc:
        session.rollback()
        if get_self_contact(session, user_id) is not None:
            raise ProfileAlreadyExistsError from exc
        raise_identity_conflict_after_integrity_error(
            session,
            user_id,
            name=validated_name,
            location=normalized_location,
            remark=None,
        )
        raise
    except Exception:
        session.rollback()
        raise


def update_self_profile(
    session: Session,
    user_id: int,
    *,
    name: str | None,
    location: str | None,
    fields: set[str],
) -> Contact:
    if not fields:
        raise ProfileValidationError("PROFILE_UPDATE_EMPTY", "至少需要提交一个要修改的字段")
    if fields - {"name", "location"}:
        raise ProfileValidationError("PROFILE_FIELD_FORBIDDEN", "本人身份和所属账号不能修改")
    validated_name = validate_profile_name(name) if "name" in fields and name is not None else None
    if "name" in fields and name is None:
        raise ProfileValidationError("INVALID_PROFILE_NAME", "本人真实姓名不能为空")
    normalized_location = normalize_profile_location(location) if "location" in fields else None

    candidate_identity: tuple[str, str | None, str | None] | None = None
    candidate_contact_id: int | None = None
    try:
        user = _locked_active_user(session, user_id)
        if user is None:
            raise AccountUnavailableError
        contact = _locked_self_contact(session, user_id)
        if contact is None:
            raise ProfileNotFoundError

        candidate_name = validated_name if "name" in fields else contact.name
        candidate_location = normalized_location if "location" in fields else contact.location
        candidate_identity = (candidate_name, candidate_location, contact.remark)
        candidate_contact_id = contact.id
        identity_hash = ensure_contact_identity_available(
            session,
            user_id,
            name=candidate_name,
            location=candidate_location,
            remark=contact.remark,
            exclude_contact_id=contact.id,
        )

        if "name" in fields:
            assert validated_name is not None
            contact.name = validated_name
        if "location" in fields:
            contact.location = normalized_location
        contact.identity_hash = identity_hash
        session.flush()
        session.commit()
        return contact
    except IntegrityError:
        session.rollback()
        if candidate_identity is not None and candidate_contact_id is not None:
            candidate_name, candidate_location, candidate_remark = candidate_identity
            raise_identity_conflict_after_integrity_error(
                session,
                user_id,
                name=candidate_name,
                location=candidate_location,
                remark=candidate_remark,
                exclude_contact_id=candidate_contact_id,
            )
        raise
    except Exception:
        session.rollback()
        raise
