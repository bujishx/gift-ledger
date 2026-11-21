"""Independent event registration and atomic gifts attached to an existing event."""

from datetime import date
from decimal import Decimal
from typing import Literal

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import Contact, EventRecord, EventType, GiftRecord, KinshipType
from app.schemas.dictionaries import DictionaryCategory
from app.schemas.ledger import (
    EventGiftCreateItem,
    EventListItemResponse,
    EventListResponse,
    EventResponse,
    LedgerContactResponse,
    LedgerDictionaryResponse,
)
from app.services.contacts import get_contact
from app.services.dictionaries import get_visible_dictionary_item
from app.services.event_dates import (
    date_period_conditions,
    date_sort_expression,
    date_value,
    event_date_response_fields,
)
from app.services.ledger import (
    EventAlreadyExistsError,
    GiftAlreadyExistsError,
    LedgerValidationError,
    _active_contact,
    _create_or_reuse_event,
    _lock_active_user,
    _locked_gift,
    derive_direction,
    validate_gift_value,
)
from app.services.ledger_records import _locked_event_by_id, event_revision
from app.services.profile import get_self_contact


def create_event(
    session: Session,
    user_id: int,
    *,
    host_contact_id: int,
    kinship_type_id: int,
    event_type_id: int,
    event_date: date,
    remark: str | None,
) -> EventRecord:
    if event_date < date(1000, 1, 1):
        raise LedgerValidationError("EVENT_DATE_INVALID", "办事日期格式不正确")
    try:
        _lock_active_user(session, user_id)
        self_contact = get_self_contact(session, user_id)
        if self_contact is None or not self_contact.is_active:
            raise LedgerValidationError("SELF_CONTACT_REQUIRED", "请先完成本人信息建档")
        _active_contact(session, user_id, host_contact_id, role="host")
        for category, item_id, code, message in (
            (
                DictionaryCategory.KINSHIP,
                kinship_type_id,
                "KINSHIP_TYPE_UNAVAILABLE",
                "所选亲属称谓不可用",
            ),
            (
                DictionaryCategory.EVENT,
                event_type_id,
                "EVENT_TYPE_UNAVAILABLE",
                "所选事件类型不可用",
            ),
        ):
            if (
                get_visible_dictionary_item(
                    session, category, user_id, item_id, require_active=True
                )
                is None
            ):
                raise LedgerValidationError(code, message)
        event, reused = _create_or_reuse_event(
            session,
            user_id,
            host_contact_id=host_contact_id,
            kinship_type_id=kinship_type_id,
            event_type_id=event_type_id,
            event_date=event_date,
            event_remark=remark,
        )
        if reused:
            raise EventAlreadyExistsError(event.id)
        session.commit()
        return event
    except Exception:
        session.rollback()
        raise


def create_event_gifts(
    session: Session,
    user_id: int,
    event_id: int,
    gifts: list[EventGiftCreateItem],
) -> list[GiftRecord]:
    """Validate every card, then commit all gifts together or none."""
    if not 1 <= len(gifts) <= 5:
        raise LedgerValidationError("GIFT_CARD_COUNT_INVALID", "每次须填写 1 至 5 位随礼人")
    try:
        _lock_active_user(session, user_id)
        event = _locked_event_by_id(session, user_id, event_id)
        if date_value(event).precision != "day":
            raise LedgerValidationError(
                "EVENT_DATE_INCOMPLETE", "此旧账事件的办事日期不完整，请先补齐日期再新增记账"
            )
        self_contact = get_self_contact(session, user_id)
        if self_contact is None or not self_contact.is_active:
            raise LedgerValidationError("SELF_CONTACT_REQUIRED", "请先完成本人信息建档")
        is_home_event = event.contact_id == self_contact.id
        if not is_home_event and len(gifts) != 1:
            raise LedgerValidationError("EXPENSE_SINGLE_GIVER", "别人办事时只能登记本人一笔流水")
        prepared: list[tuple[int, Decimal, str | None, str | None]] = []
        used_ids: set[int] = set()
        for item in gifts:
            giver_id = item.giver_contact_id
            if is_home_event:
                if giver_id is None:
                    raise LedgerValidationError(
                        "GIVER_CONTACT_REQUIRED", "我家办事时必须选择随礼人"
                    )
                _active_contact(session, user_id, giver_id, role="giver")
            else:
                if giver_id is not None and giver_id != self_contact.id:
                    raise LedgerValidationError(
                        "EXPENSE_GIVER_MUST_BE_SELF", "别人办事时随礼人必须是本人"
                    )
                giver_id = self_contact.id
            derive_direction(
                self_contact_id=self_contact.id,
                host_contact_id=event.contact_id,
                giver_contact_id=giver_id,
            )
            if giver_id in used_ids:
                raise LedgerValidationError("DUPLICATE_GIVER_CARD", "同一随礼人在本次登记中重复")
            used_ids.add(giver_id)
            amount, gift_details = validate_gift_value(item.amount, item.gift_details)
            existing = _locked_gift(session, user_id, event_id, giver_id)
            if existing is not None:
                raise GiftAlreadyExistsError(existing.id)
            prepared.append((giver_id, amount, gift_details, item.remark))
        records = [
            GiftRecord(
                user_id=user_id,
                event_record_id=event_id,
                contact_id=giver_id,
                amount=amount,
                gift_details=gift_details,
                remark=remark,
            )
            for giver_id, amount, gift_details, remark in prepared
        ]
        session.add_all(records)
        session.flush()
        session.commit()
        return records
    except IntegrityError as exc:
        session.rollback()
        # A concurrent writer may win the unique event/giver constraint.
        self_contact = get_self_contact(session, user_id)
        for item in gifts:
            giver_id = item.giver_contact_id or (self_contact.id if self_contact else None)
            if giver_id is None:
                continue
            existing = _locked_gift(session, user_id, event_id, giver_id)
            if existing is not None:
                session.rollback()
                raise GiftAlreadyExistsError(existing.id) from exc
        session.rollback()
        raise
    except Exception:
        session.rollback()
        raise


def list_events(
    session: Session,
    user_id: int,
    *,
    keyword: str | None = None,
    relationship_type_ids: list[int] | None = None,
    kinship_type_ids: list[int] | None = None,
    event_type_ids: list[int] | None = None,
    host_contact_id: int | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    sort_by: Literal[
        "registered_at", "event_date", "host_name", "gift_count", "cash_amount"
    ] = "registered_at",
    sort_order: Literal["asc", "desc"] = "desc",
    limit: int = 50,
    offset: int = 0,
) -> EventListResponse:
    if host_contact_id is not None:
        if get_contact(session, user_id, host_contact_id) is None:
            raise LedgerValidationError("HOST_CONTACT_FILTER_UNAVAILABLE", "所选事主不可用")
    conditions = [EventRecord.user_id == user_id, Contact.user_id == user_id]
    if host_contact_id is not None:
        conditions.append(EventRecord.contact_id == host_contact_id)
    if relationship_type_ids:
        conditions.append(Contact.relation_type_id.in_(relationship_type_ids))
    if kinship_type_ids:
        conditions.append(EventRecord.relative_type_id.in_(kinship_type_ids))
    if event_type_ids:
        conditions.append(EventRecord.event_type_id.in_(event_type_ids))
    conditions.extend(date_period_conditions(date_from, date_to))
    keyword_conditions = []
    for term in (keyword or "").split():
        pattern = "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        keyword_conditions.extend(
            (
                Contact.name.like(pattern, escape="\\"),
                Contact.remark.like(pattern, escape="\\"),
                EventRecord.remark.like(pattern, escape="\\"),
            )
        )
    if keyword_conditions:
        conditions.append(or_(*keyword_conditions))
    base = (
        select(EventRecord)
        .join(
            Contact,
            (Contact.id == EventRecord.contact_id) & (Contact.user_id == EventRecord.user_id),
        )
        .where(*conditions)
    )
    total = session.scalar(select(func.count()).select_from(base.subquery())) or 0
    gift_count = (
        select(func.count(GiftRecord.id))
        .where(GiftRecord.user_id == user_id, GiftRecord.event_record_id == EventRecord.id)
        .correlate(EventRecord)
        .scalar_subquery()
    )
    cash_amount = (
        select(func.coalesce(func.sum(GiftRecord.amount), 0))
        .where(GiftRecord.user_id == user_id, GiftRecord.event_record_id == EventRecord.id)
        .correlate(EventRecord)
        .scalar_subquery()
    )
    sort_column = {
        "registered_at": EventRecord.created_at,
        "event_date": date_sort_expression(),
        "host_name": Contact.name,
        "gift_count": gift_count,
        "cash_amount": cash_amount,
    }[sort_by]
    order = sort_column.asc() if sort_order == "asc" else sort_column.desc()
    rows = session.execute(
        select(EventRecord, Contact, KinshipType, EventType)
        .join(
            Contact,
            (Contact.id == EventRecord.contact_id) & (Contact.user_id == EventRecord.user_id),
        )
        .join(
            KinshipType,
            (KinshipType.id == EventRecord.relative_type_id)
            & or_(KinshipType.user_id.is_(None), KinshipType.user_id == user_id),
        )
        .join(
            EventType,
            (EventType.id == EventRecord.event_type_id)
            & or_(EventType.user_id.is_(None), EventType.user_id == user_id),
        )
        .where(*conditions)
        .order_by(
            *([(EventRecord.date_precision == "unknown").asc()] if sort_by == "event_date" else []),
            order,
            EventRecord.id.desc(),
        )
        .limit(limit)
        .offset(offset)
    ).all()
    ids = [event.id for event, *_ in rows]
    totals = {}
    if ids:
        totals = {
            event_id: (count, amount or Decimal("0.00"))
            for event_id, count, amount in session.execute(
                select(
                    GiftRecord.event_record_id,
                    func.count(GiftRecord.id),
                    func.sum(GiftRecord.amount),
                )
                .where(GiftRecord.user_id == user_id, GiftRecord.event_record_id.in_(ids))
                .group_by(GiftRecord.event_record_id)
            )
        }
    items = []
    for event, host, kinship, event_type in rows:
        count, amount = totals.get(event.id, (0, Decimal("0.00")))
        items.append(
            EventListItemResponse(
                event=EventResponse(
                    id=event.id,
                    revision=event_revision(event),
                    host=LedgerContactResponse(id=host.id, name=host.name),
                    kinship_type=LedgerDictionaryResponse(id=kinship.id, name=kinship.type_name),
                    event_type=LedgerDictionaryResponse(
                        id=event_type.id, name=event_type.type_name
                    ),
                    **event_date_response_fields(event),
                    remark=event.remark,
                    created_at=event.created_at,
                ),
                gift_count=count,
                cash_amount=amount,
            )
        )
    return EventListResponse(items=items, total=total, limit=limit, offset=offset)
