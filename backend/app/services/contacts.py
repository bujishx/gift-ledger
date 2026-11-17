import logging
from dataclasses import dataclass

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import Contact, EventRecord, GiftRecord, RelationshipType
from app.schemas.contacts import ContactResponse
from app.schemas.dictionaries import DictionaryCategory
from app.services.contact_identity import (
    contact_identity_components,
    contact_identity_hash,
    normalize_identity_component,
)
from app.services.dictionaries import get_visible_dictionary_item
from app.services.system_dictionary_catalog import SELF_RELATIONSHIP_SYSTEM_CODE
from app.services.user_ledger_lock import lock_user_ledger_write

logger = logging.getLogger(__name__)


class ContactValidationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ContactNotFoundError(ValueError):
    pass


class SelfContactProtectedError(ValueError):
    pass


class ContactRelationshipUnavailableError(ValueError):
    pass


class ContactInUseError(ValueError):
    pass


class ContactIdentityConflictError(ValueError):
    def __init__(self, contact: Contact) -> None:
        super().__init__("联系人身份已经存在")
        self.contact = contact


class ContactIdentityHashCollisionError(RuntimeError):
    pass


class ContactDistinguishingInfoRequiredError(ValueError):
    def __init__(self, *, match_count: int) -> None:
        super().__init__("存在同名联系人，请填写不同的联系人备注加以区分")
        self.match_count = match_count


@dataclass(frozen=True)
class ContactView:
    contact: Contact
    relationship_type_name: str
    relationship_system_code: str | None
    has_references: bool = False


@dataclass(frozen=True)
class ContactPage:
    items: list[ContactView]
    total: int
    limit: int
    offset: int


def normalize_contact_name(name: str) -> str:
    normalized = name.strip()
    if not normalized:
        raise ContactValidationError("CONTACT_NAME_REQUIRED", "联系人姓名不能为空")
    if len(normalized) > 50:
        raise ContactValidationError("CONTACT_NAME_TOO_LONG", "联系人姓名最多允许 50 个字符")
    return normalized


def normalize_contact_location(location: str | None) -> str | None:
    return _normalize_optional_contact_value(
        location,
        max_length=50,
        code="CONTACT_LOCATION_TOO_LONG",
        message="常住地最多允许 50 个字符",
    )


def normalize_contact_phone(phone: str | None) -> str | None:
    without_whitespace = "".join(phone.split()) if phone is not None else None
    return _normalize_optional_contact_value(
        without_whitespace,
        max_length=20,
        code="CONTACT_PHONE_TOO_LONG",
        message="电话最多允许 20 个字符",
    )


def _normalize_optional_contact_value(
    value: str | None,
    *,
    max_length: int,
    code: str,
    message: str,
) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    if len(normalized) > max_length:
        raise ContactValidationError(code, message)
    return normalized


def _identity_match(
    session: Session,
    user_id: int,
    *,
    identity_hash: bytes,
    name: str,
    location: str | None,
    remark: str | None,
    exclude_contact_id: int | None = None,
) -> Contact | None:
    statement = select(Contact).where(
        Contact.user_id == user_id,
        Contact.identity_hash == identity_hash,
    )
    if exclude_contact_id is not None:
        statement = statement.where(Contact.id != exclude_contact_id)
    existing = session.scalar(statement.limit(1))
    if existing is None:
        return None
    if contact_identity_components(existing.name, existing.location, existing.remark) != (
        contact_identity_components(name, location, remark)
    ):
        logger.error(
            "contact_identity_hash_collision user_id=%s existing_contact_id=%s",
            user_id,
            existing.id,
        )
        raise ContactIdentityHashCollisionError
    return existing


def ensure_contact_identity_available(
    session: Session,
    user_id: int,
    *,
    name: str,
    location: str | None,
    remark: str | None,
    exclude_contact_id: int | None = None,
) -> bytes:
    identity_hash = contact_identity_hash(name, location, remark)
    existing = _identity_match(
        session,
        user_id,
        identity_hash=identity_hash,
        name=name,
        location=location,
        remark=remark,
        exclude_contact_id=exclude_contact_id,
    )
    if existing is not None:
        raise ContactIdentityConflictError(existing)

    return identity_hash


def raise_identity_conflict_after_integrity_error(
    session: Session,
    user_id: int,
    *,
    name: str,
    location: str | None,
    remark: str | None,
    exclude_contact_id: int | None = None,
) -> None:
    identity_hash = contact_identity_hash(name, location, remark)
    existing = _identity_match(
        session,
        user_id,
        identity_hash=identity_hash,
        name=name,
        location=location,
        remark=remark,
        exclude_contact_id=exclude_contact_id,
    )
    if existing is not None:
        raise ContactIdentityConflictError(existing)


def _contact_filters(
    user_id: int,
    *,
    search: str | None,
    relationship_type_id: int | None,
    is_active: bool | None,
):
    filters = [Contact.user_id == user_id]
    normalized_search = search.strip() if search is not None else ""
    if normalized_search:
        filters.append(
            or_(
                *(
                    column.contains(term, autoescape=True)
                    for term in normalized_search.split()
                    for column in (Contact.name, Contact.remark)
                )
            )
        )
    if relationship_type_id is not None:
        filters.append(Contact.relation_type_id == relationship_type_id)
    if is_active is not None:
        filters.append(Contact.is_active == int(is_active))
    return filters


def _contact_has_references():
    return or_(
        select(EventRecord.id)
        .where(EventRecord.user_id == Contact.user_id, EventRecord.contact_id == Contact.id)
        .exists(),
        select(GiftRecord.id)
        .where(GiftRecord.user_id == Contact.user_id, GiftRecord.contact_id == Contact.id)
        .exists(),
    )


def list_contacts(
    session: Session,
    user_id: int,
    *,
    search: str | None,
    relationship_type_id: int | None,
    is_active: bool | None,
    limit: int,
    offset: int,
) -> ContactPage:
    filters = _contact_filters(
        user_id,
        search=search,
        relationship_type_id=relationship_type_id,
        is_active=is_active,
    )
    relationship_scope = or_(
        RelationshipType.user_id.is_(None),
        RelationshipType.user_id == user_id,
    )
    relationship_join = and_(
        RelationshipType.id == Contact.relation_type_id,
        relationship_scope,
    )
    total = (
        session.scalar(
            select(func.count())
            .select_from(Contact)
            .join(RelationshipType, relationship_join)
            .where(*filters)
        )
        or 0
    )
    rows = session.execute(
        select(
            Contact,
            RelationshipType.type_name,
            RelationshipType.system_code,
            _contact_has_references(),
        )
        .join(RelationshipType, relationship_join)
        .where(*filters)
        .order_by(
            Contact.created_at.desc(),
            Contact.id.desc(),
        )
        .limit(limit)
        .offset(offset)
    ).all()
    return ContactPage(
        items=[
            ContactView(
                contact=row[0],
                relationship_type_name=row[1],
                relationship_system_code=row[2],
                has_references=bool(row[3]),
            )
            for row in rows
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


def list_contact_name_matches(
    session: Session,
    user_id: int,
    name: str,
) -> list[ContactView]:
    normalized_name = normalize_contact_name(name)
    canonical_name = normalize_identity_component(normalized_name, required=True)
    relationship_scope = or_(
        RelationshipType.user_id.is_(None),
        RelationshipType.user_id == user_id,
    )
    rows = session.execute(
        select(
            Contact,
            RelationshipType.type_name,
            RelationshipType.system_code,
            _contact_has_references(),
        )
        .join(
            RelationshipType,
            and_(
                RelationshipType.id == Contact.relation_type_id,
                relationship_scope,
            ),
        )
        .where(Contact.user_id == user_id)
        .order_by(Contact.is_active.desc(), Contact.id.asc())
    ).all()
    return [
        ContactView(
            contact=row[0],
            relationship_type_name=row[1],
            relationship_system_code=row[2],
            has_references=bool(row[3]),
        )
        for row in rows
        if normalize_identity_component(row[0].name, required=True) == canonical_name
    ]


def get_contact(
    session: Session,
    user_id: int,
    contact_id: int,
) -> ContactView | None:
    relationship_scope = or_(
        RelationshipType.user_id.is_(None),
        RelationshipType.user_id == user_id,
    )
    row = session.execute(
        select(
            Contact,
            RelationshipType.type_name,
            RelationshipType.system_code,
            _contact_has_references(),
        )
        .join(
            RelationshipType,
            and_(
                RelationshipType.id == Contact.relation_type_id,
                relationship_scope,
            ),
        )
        .where(Contact.id == contact_id, Contact.user_id == user_id)
    ).one_or_none()
    if row is None:
        return None
    return ContactView(
        contact=row[0],
        relationship_type_name=row[1],
        relationship_system_code=row[2],
        has_references=bool(row[3]),
    )


def _locked_contact(session: Session, user_id: int, contact_id: int) -> Contact:
    contact = session.scalar(
        select(Contact)
        .where(Contact.id == contact_id, Contact.user_id == user_id)
        .with_for_update()
    )
    if contact is None:
        raise ContactNotFoundError
    return contact


def _ensure_ordinary_contact(session: Session, contact: Contact) -> None:
    relationship_type = session.scalar(
        select(RelationshipType).where(
            RelationshipType.id == contact.relation_type_id,
            or_(
                RelationshipType.user_id.is_(None),
                RelationshipType.user_id == contact.user_id,
            ),
        )
    )
    if relationship_type is None:
        raise ContactRelationshipUnavailableError
    if relationship_type.system_code == SELF_RELATIONSHIP_SYSTEM_CODE:
        raise SelfContactProtectedError


def _validate_relationship_type(
    session: Session,
    user_id: int,
    relationship_type_id: int,
) -> None:
    relationship_type = get_visible_dictionary_item(
        session,
        DictionaryCategory.RELATIONSHIP,
        user_id,
        relationship_type_id,
        require_active=True,
    )
    if relationship_type is None:
        raise ContactRelationshipUnavailableError
    if relationship_type.system_code == SELF_RELATIONSHIP_SYSTEM_CODE:
        raise SelfContactProtectedError


def create_contact(
    session: Session,
    user_id: int,
    *,
    name: str,
    relationship_type_id: int,
    location: str | None,
    phone: str | None,
    remark: str | None,
) -> Contact:
    normalized_name = normalize_contact_name(name)
    normalized_location = normalize_contact_location(location)
    normalized_phone = normalize_contact_phone(phone)
    try:
        lock_user_ledger_write(session, user_id)
        _validate_relationship_type(session, user_id, relationship_type_id)
        identity_hash = ensure_contact_identity_available(
            session,
            user_id,
            name=normalized_name,
            location=normalized_location,
            remark=remark,
        )
        contact = Contact(
            user_id=user_id,
            name=normalized_name,
            relation_type_id=relationship_type_id,
            location=normalized_location,
            phone=normalized_phone,
            remark=remark,
            identity_hash=identity_hash,
            is_active=1,
        )
        session.add(contact)
        session.flush()
        session.commit()
        return contact
    except IntegrityError:
        session.rollback()
        raise_identity_conflict_after_integrity_error(
            session,
            user_id,
            name=normalized_name,
            location=normalized_location,
            remark=remark,
        )
        raise
    except Exception:
        session.rollback()
        raise


def update_contact(
    session: Session,
    user_id: int,
    contact_id: int,
    *,
    name: str | None,
    relationship_type_id: int | None,
    location: str | None,
    phone: str | None,
    remark: str | None,
    fields: set[str],
) -> Contact:
    allowed_fields = {"name", "relationship_type_id", "location", "phone", "remark"}
    if not fields:
        raise ContactValidationError("CONTACT_UPDATE_EMPTY", "至少需要提交一个要修改的字段")
    if fields - allowed_fields:
        raise ContactValidationError("CONTACT_FIELD_FORBIDDEN", "提交了不允许修改的联系人字段")
    if "name" in fields and name is None:
        raise ContactValidationError("CONTACT_NAME_REQUIRED", "联系人姓名不能为空")
    if "relationship_type_id" in fields and relationship_type_id is None:
        raise ContactValidationError("CONTACT_RELATIONSHIP_REQUIRED", "关系类型不能为空")

    normalized_name = (
        normalize_contact_name(name) if "name" in fields and name is not None else None
    )
    normalized_location = normalize_contact_location(location) if "location" in fields else None
    normalized_phone = normalize_contact_phone(phone) if "phone" in fields else None

    candidate_identity: tuple[str, str | None, str | None] | None = None
    try:
        lock_user_ledger_write(session, user_id)
        contact = _locked_contact(session, user_id, contact_id)
        _ensure_ordinary_contact(session, contact)
        if "relationship_type_id" in fields:
            assert relationship_type_id is not None
            _validate_relationship_type(session, user_id, relationship_type_id)

        candidate_name = normalized_name if "name" in fields else contact.name
        candidate_location = normalized_location if "location" in fields else contact.location
        candidate_remark = remark if "remark" in fields else contact.remark
        candidate_identity = (candidate_name, candidate_location, candidate_remark)
        identity_hash = ensure_contact_identity_available(
            session,
            user_id,
            name=candidate_name,
            location=candidate_location,
            remark=candidate_remark,
            exclude_contact_id=contact.id,
        )

        if "relationship_type_id" in fields:
            contact.relation_type_id = relationship_type_id
        if "name" in fields:
            assert normalized_name is not None
            contact.name = normalized_name
        if "location" in fields:
            contact.location = normalized_location
        if "phone" in fields:
            contact.phone = normalized_phone
        if "remark" in fields:
            contact.remark = remark
        contact.identity_hash = identity_hash
        session.flush()
        session.commit()
        return contact
    except IntegrityError:
        session.rollback()
        if candidate_identity is not None:
            candidate_name, candidate_location, candidate_remark = candidate_identity
            raise_identity_conflict_after_integrity_error(
                session,
                user_id,
                name=candidate_name,
                location=candidate_location,
                remark=candidate_remark,
                exclude_contact_id=contact_id,
            )
        raise
    except Exception:
        session.rollback()
        raise


def set_contact_active(
    session: Session,
    user_id: int,
    contact_id: int,
    *,
    is_active: bool,
) -> Contact:
    try:
        lock_user_ledger_write(session, user_id)
        contact = _locked_contact(session, user_id, contact_id)
        _ensure_ordinary_contact(session, contact)
        contact.is_active = int(is_active)
        session.flush()
        session.commit()
        return contact
    except Exception:
        session.rollback()
        raise


def delete_contact(session: Session, user_id: int, contact_id: int) -> None:
    try:
        lock_user_ledger_write(session, user_id)
        contact = _locked_contact(session, user_id, contact_id)
        _ensure_ordinary_contact(session, contact)
        event_reference = session.scalar(
            select(EventRecord.id).where(
                EventRecord.user_id == user_id,
                EventRecord.contact_id == contact_id,
            )
        )
        gift_reference = session.scalar(
            select(GiftRecord.id).where(
                GiftRecord.user_id == user_id,
                GiftRecord.contact_id == contact_id,
            )
        )
        if event_reference is not None or gift_reference is not None:
            raise ContactInUseError
        session.delete(contact)
        session.flush()
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise ContactInUseError from exc
    except Exception:
        session.rollback()
        raise


def contact_response(view: ContactView) -> ContactResponse:
    contact = view.contact
    return ContactResponse(
        id=contact.id,
        name=contact.name,
        relationship_type_id=contact.relation_type_id,
        relationship_type_name=view.relationship_type_name,
        location=contact.location,
        phone=contact.phone,
        remark=contact.remark,
        is_active=bool(contact.is_active),
        is_self=view.relationship_system_code == SELF_RELATIONSHIP_SYSTEM_CODE,
        created_at=contact.created_at,
        has_references=view.has_references,
    )
