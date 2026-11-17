from dataclasses import dataclass
from typing import Any

from sqlalchemy import case, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import InstrumentedAttribute

from app.db.models import (
    Contact,
    EventRecord,
    EventType,
    KinshipType,
    RelationshipType,
)
from app.schemas.dictionaries import DictionaryCategory, DictionaryItemResponse
from app.services.user_ledger_lock import lock_user_ledger_write


class DictionaryValidationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class DictionaryItemNotFoundError(ValueError):
    pass


class SystemDictionaryItemReadOnlyError(ValueError):
    pass


class DictionaryNameConflictError(ValueError):
    pass


class DictionaryItemInUseError(ValueError):
    pass


@dataclass(frozen=True)
class DictionaryDefinition:
    model: type[Any]
    reference_model: type[Any]
    reference_user_id: InstrumentedAttribute[Any]
    reference_column: InstrumentedAttribute[Any]


DICTIONARY_DEFINITIONS = {
    DictionaryCategory.RELATIONSHIP: DictionaryDefinition(
        model=RelationshipType,
        reference_model=Contact,
        reference_user_id=Contact.user_id,
        reference_column=Contact.relation_type_id,
    ),
    DictionaryCategory.KINSHIP: DictionaryDefinition(
        model=KinshipType,
        reference_model=EventRecord,
        reference_user_id=EventRecord.user_id,
        reference_column=EventRecord.relative_type_id,
    ),
    DictionaryCategory.EVENT: DictionaryDefinition(
        model=EventType,
        reference_model=EventRecord,
        reference_user_id=EventRecord.user_id,
        reference_column=EventRecord.event_type_id,
    ),
}


def normalize_dictionary_name(name: str) -> str:
    normalized = name.strip()
    if not normalized:
        raise DictionaryValidationError("DICTIONARY_NAME_REQUIRED", "名称不能为空")
    if len(normalized) > 50:
        raise DictionaryValidationError(
            "DICTIONARY_NAME_TOO_LONG",
            "名称最多允许 50 个字符",
        )
    return normalized


def _definition(category: DictionaryCategory) -> DictionaryDefinition:
    return DICTIONARY_DEFINITIONS[category]


def _visible_condition(model: type[Any], user_id: int):
    return or_(model.user_id.is_(None), model.user_id == user_id)


def is_system_dictionary_item(item: Any) -> bool:
    return item.system_code is not None


def get_system_dictionary_item(
    session: Session,
    category: DictionaryCategory,
    system_code: str,
    *,
    require_active: bool,
) -> Any | None:
    model = _definition(category).model
    statement = select(model).where(
        model.user_id.is_(None),
        model.system_code == system_code,
    )
    if require_active:
        statement = statement.where(model.is_active == 1)
    return session.scalar(statement)


def list_dictionary_items(
    session: Session,
    category: DictionaryCategory,
    user_id: int,
    *,
    include_inactive: bool,
) -> list[Any]:
    model = _definition(category).model
    statement = select(model).where(_visible_condition(model, user_id))
    if not include_inactive:
        statement = statement.where(model.is_active == 1)
    statement = statement.order_by(
        case((model.system_code.is_not(None), 0), else_=1),
        case((model.system_code.is_not(None), model.sort_order), else_=0),
        model.id,
    )
    return list(session.scalars(statement).all())


def get_visible_dictionary_item(
    session: Session,
    category: DictionaryCategory,
    user_id: int,
    item_id: int,
    *,
    require_active: bool,
) -> Any | None:
    model = _definition(category).model
    statement = select(model).where(
        model.id == item_id,
        _visible_condition(model, user_id),
    )
    if require_active:
        statement = statement.where(model.is_active == 1)
    return session.scalar(statement)


def _locked_visible_item(
    session: Session,
    category: DictionaryCategory,
    user_id: int,
    item_id: int,
) -> Any:
    model = _definition(category).model
    item = session.scalar(
        select(model)
        .where(
            model.id == item_id,
            _visible_condition(model, user_id),
        )
        .with_for_update()
    )
    if item is None:
        raise DictionaryItemNotFoundError
    return item


def _ensure_custom_item(item: Any, user_id: int) -> None:
    if item.user_id is None or is_system_dictionary_item(item):
        raise SystemDictionaryItemReadOnlyError
    if item.user_id != user_id:
        raise DictionaryItemNotFoundError


def _visible_name_exists(
    session: Session,
    category: DictionaryCategory,
    user_id: int,
    normalized_name: str,
    *,
    exclude_item_id: int | None = None,
) -> bool:
    model = _definition(category).model
    statement = select(model.id).where(
        _visible_condition(model, user_id),
        model.name_key == normalized_name,
    )
    if exclude_item_id is not None:
        statement = statement.where(model.id != exclude_item_id)
    return session.scalar(statement) is not None


def create_dictionary_item(
    session: Session,
    category: DictionaryCategory,
    user_id: int,
    name: str,
) -> Any:
    normalized_name = normalize_dictionary_name(name)
    model = _definition(category).model
    try:
        lock_user_ledger_write(session, user_id)
        if _visible_name_exists(session, category, user_id, normalized_name):
            raise DictionaryNameConflictError
        item = model(
            user_id=user_id,
            system_code=None,
            type_name=normalized_name,
            sort_order=0,
            is_active=1,
        )
        session.add(item)
        session.flush()
        session.commit()
        return item
    except IntegrityError as exc:
        session.rollback()
        if _visible_name_exists(session, category, user_id, normalized_name):
            raise DictionaryNameConflictError from exc
        raise
    except Exception:
        session.rollback()
        raise


def rename_dictionary_item(
    session: Session,
    category: DictionaryCategory,
    user_id: int,
    item_id: int,
    name: str,
) -> Any:
    normalized_name = normalize_dictionary_name(name)
    try:
        lock_user_ledger_write(session, user_id)
        item = _locked_visible_item(session, category, user_id, item_id)
        _ensure_custom_item(item, user_id)
        if _visible_name_exists(
            session,
            category,
            user_id,
            normalized_name,
            exclude_item_id=item_id,
        ):
            raise DictionaryNameConflictError
        item.type_name = normalized_name
        session.flush()
        session.commit()
        return item
    except IntegrityError as exc:
        session.rollback()
        if _visible_name_exists(
            session,
            category,
            user_id,
            normalized_name,
            exclude_item_id=item_id,
        ):
            raise DictionaryNameConflictError from exc
        raise
    except Exception:
        session.rollback()
        raise


def set_dictionary_item_active(
    session: Session,
    category: DictionaryCategory,
    user_id: int,
    item_id: int,
    *,
    is_active: bool,
) -> Any:
    try:
        lock_user_ledger_write(session, user_id)
        item = _locked_visible_item(session, category, user_id, item_id)
        _ensure_custom_item(item, user_id)
        item.is_active = int(is_active)
        session.flush()
        session.commit()
        return item
    except Exception:
        session.rollback()
        raise


def delete_dictionary_item(
    session: Session,
    category: DictionaryCategory,
    user_id: int,
    item_id: int,
) -> None:
    definition = _definition(category)
    try:
        lock_user_ledger_write(session, user_id)
        item = _locked_visible_item(session, category, user_id, item_id)
        _ensure_custom_item(item, user_id)
        reference_id = session.scalar(
            select(definition.reference_column).where(
                definition.reference_user_id == user_id,
                definition.reference_column == item_id,
            )
        )
        if reference_id is not None:
            raise DictionaryItemInUseError
        session.delete(item)
        session.flush()
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise DictionaryItemInUseError from exc
    except Exception:
        session.rollback()
        raise


def dictionary_item_response(
    category: DictionaryCategory,
    item: Any,
) -> DictionaryItemResponse:
    is_system = is_system_dictionary_item(item)
    return DictionaryItemResponse(
        id=item.id,
        category=category,
        name=item.type_name,
        is_system=is_system,
        is_active=bool(item.is_active),
        editable=not is_system,
    )
