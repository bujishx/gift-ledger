from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import Contact, EventRecord, EventType, GiftRecord, KinshipType
from app.schemas.dictionaries import DictionaryCategory
from app.schemas.ledger import (
    EventDetailResponse,
    EventResponse,
    GiftDetailResponse,
    GiftResponse,
    LedgerContactResponse,
    LedgerDictionaryResponse,
    LedgerDirection,
)
from app.services.dictionaries import get_visible_dictionary_item
from app.services.event_dates import (
    date_value,
    event_date_response_fields,
    merge_event_dates,
    parse_event_date,
)
from app.services.event_identity import event_identity_hash
from app.services.ledger import (
    GiftAlreadyExistsError,
    LedgerValidationError,
    _active_contact,
    _lock_active_user,
    derive_direction,
    validate_gift_value,
)
from app.services.profile import get_self_contact


class EventNotFoundError(ValueError):
    pass


class GiftNotFoundError(ValueError):
    pass


class EventHasGiftsError(ValueError):
    pass


class EventIdentityConflictError(ValueError):
    def __init__(self, event_id: int) -> None:
        super().__init__("更正后的事件已经存在，不能自动合并")
        self.event_id = event_id


@dataclass(frozen=True)
class EventView:
    event: EventRecord
    host_contact: Contact
    kinship_type: KinshipType
    event_type: EventType


@dataclass(frozen=True)
class GiftView:
    gift: GiftRecord
    giver_contact: Contact
    direction: LedgerDirection


@dataclass(frozen=True)
class EventDetailView:
    event: EventView
    gifts: list[GiftView]


@dataclass(frozen=True)
class GiftDetailView:
    event: EventView
    gift: GiftView


@dataclass(frozen=True)
class GiftUpdateResult:
    event: EventRecord
    gift: GiftRecord
    direction: LedgerDirection


def _event_view_row(session: Session, user_id: int, event_id: int):
    return session.execute(
        select(EventRecord, Contact, KinshipType, EventType)
        .join(
            Contact,
            and_(
                Contact.id == EventRecord.contact_id,
                Contact.user_id == EventRecord.user_id,
            ),
        )
        .join(
            KinshipType,
            and_(
                KinshipType.id == EventRecord.relative_type_id,
                or_(KinshipType.user_id.is_(None), KinshipType.user_id == user_id),
            ),
        )
        .join(
            EventType,
            and_(
                EventType.id == EventRecord.event_type_id,
                or_(EventType.user_id.is_(None), EventType.user_id == user_id),
            ),
        )
        .where(EventRecord.id == event_id, EventRecord.user_id == user_id)
    ).one_or_none()


def _event_view(session: Session, user_id: int, event_id: int) -> EventView:
    row = _event_view_row(session, user_id, event_id)
    if row is None:
        raise EventNotFoundError
    return EventView(
        event=row[0],
        host_contact=row[1],
        kinship_type=row[2],
        event_type=row[3],
    )


def _self_contact(session: Session, user_id: int) -> Contact:
    self_contact = get_self_contact(session, user_id)
    if self_contact is None or not self_contact.is_active:
        raise LedgerValidationError("SELF_CONTACT_REQUIRED", "请先完成本人信息建档")
    return self_contact


def _gift_views(
    session: Session,
    user_id: int,
    event: EventRecord,
    self_contact_id: int,
) -> list[GiftView]:
    rows = session.execute(
        select(GiftRecord, Contact)
        .join(
            Contact,
            and_(
                Contact.id == GiftRecord.contact_id,
                Contact.user_id == GiftRecord.user_id,
            ),
        )
        .where(
            GiftRecord.user_id == user_id,
            GiftRecord.event_record_id == event.id,
        )
        .order_by(GiftRecord.created_at.desc(), GiftRecord.id.desc())
    ).all()
    return [
        GiftView(
            gift=row[0],
            giver_contact=row[1],
            direction=derive_direction(
                self_contact_id=self_contact_id,
                host_contact_id=event.contact_id,
                giver_contact_id=row[0].contact_id,
            ),
        )
        for row in rows
    ]


def get_event_detail(session: Session, user_id: int, event_id: int) -> EventDetailView:
    event = _event_view(session, user_id, event_id)
    self_contact = _self_contact(session, user_id)
    return EventDetailView(
        event=event,
        gifts=_gift_views(session, user_id, event.event, self_contact.id),
    )


def get_gift_detail(session: Session, user_id: int, gift_id: int) -> GiftDetailView:
    gift = session.scalar(
        select(GiftRecord).where(GiftRecord.id == gift_id, GiftRecord.user_id == user_id)
    )
    if gift is None:
        raise GiftNotFoundError
    event = _event_view(session, user_id, gift.event_record_id)
    giver = session.scalar(
        select(Contact).where(
            Contact.id == gift.contact_id,
            Contact.user_id == user_id,
        )
    )
    if giver is None:
        raise GiftNotFoundError
    self_contact = _self_contact(session, user_id)
    return GiftDetailView(
        event=event,
        gift=GiftView(
            gift=gift,
            giver_contact=giver,
            direction=derive_direction(
                self_contact_id=self_contact.id,
                host_contact_id=event.event.contact_id,
                giver_contact_id=gift.contact_id,
            ),
        ),
    )


def _locked_event_by_id(session: Session, user_id: int, event_id: int) -> EventRecord:
    event = session.scalar(
        select(EventRecord)
        .where(EventRecord.id == event_id, EventRecord.user_id == user_id)
        .with_for_update()
    )
    if event is None:
        raise EventNotFoundError
    return event


def _locked_gift_by_id(session: Session, user_id: int, gift_id: int) -> GiftRecord:
    gift = session.scalar(
        select(GiftRecord)
        .where(GiftRecord.id == gift_id, GiftRecord.user_id == user_id)
        .with_for_update()
    )
    if gift is None:
        raise GiftNotFoundError
    return gift


def _event_has_gifts(session: Session, user_id: int, event_id: int) -> bool:
    return (
        session.scalar(
            select(GiftRecord.id)
            .where(
                GiftRecord.user_id == user_id,
                GiftRecord.event_record_id == event_id,
            )
            .with_for_update()
        )
        is not None
    )


def _event_identity_conflict(
    session: Session,
    user_id: int,
    *,
    event_id: int,
    host_contact_id: int,
    kinship_type_id: int,
    event_type_id: int,
    remark: str | None,
) -> int | None:
    return session.scalar(
        select(EventRecord.id)
        .where(
            EventRecord.user_id == user_id,
            EventRecord.id != event_id,
            EventRecord.identity_hash
            == event_identity_hash(host_contact_id, kinship_type_id, event_type_id, remark),
        )
        .with_for_update()
    )


def update_event(
    session: Session,
    user_id: int,
    event_id: int,
    *,
    host_contact_id: int | None,
    kinship_type_id: int | None,
    event_type_id: int | None,
    event_date: date | None,
    remark: str | None,
    fields: set[str],
) -> EventRecord:
    allowed_fields = {
        "host_contact_id",
        "kinship_type_id",
        "event_type_id",
        "event_date",
        "remark",
    }
    if not fields:
        raise LedgerValidationError("EVENT_UPDATE_EMPTY", "至少需要提交一个要修改的事件字段")
    if fields - allowed_fields:
        raise LedgerValidationError("EVENT_FIELD_FORBIDDEN", "提交了不允许修改的事件字段")
    required_values = {
        "host_contact_id": host_contact_id,
        "kinship_type_id": kinship_type_id,
        "event_type_id": event_type_id,
        "event_date": event_date,
    }
    if any(field in fields and required_values[field] is None for field in required_values):
        raise LedgerValidationError("EVENT_FIELD_REQUIRED", "事件必填字段不能设为空")
    if "event_date" in fields and event_date < date(1000, 1, 1):
        raise LedgerValidationError("EVENT_DATE_INVALID", "办事日期格式不正确")

    target_identity: dict | None = None
    try:
        _lock_active_user(session, user_id)
        event = _locked_event_by_id(session, user_id, event_id)

        target_host_id = host_contact_id if "host_contact_id" in fields else event.contact_id
        target_kinship_id = (
            kinship_type_id if "kinship_type_id" in fields else event.relative_type_id
        )
        target_event_type_id = event_type_id if "event_type_id" in fields else event.event_type_id
        target_date = event_date if "event_date" in fields else event.event_date
        target_remark = remark if "remark" in fields else event.remark
        assert target_host_id is not None
        assert target_kinship_id is not None
        assert target_event_type_id is not None

        if target_host_id != event.contact_id:
            if _event_has_gifts(session, user_id, event.id):
                raise EventHasGiftsError
            _active_contact(session, user_id, target_host_id, role="host")
        if target_kinship_id != event.relative_type_id:
            if (
                get_visible_dictionary_item(
                    session,
                    DictionaryCategory.KINSHIP,
                    user_id,
                    target_kinship_id,
                    require_active=True,
                )
                is None
            ):
                raise LedgerValidationError(
                    "KINSHIP_TYPE_UNAVAILABLE",
                    "所选亲属称谓不可用",
                )
        if target_event_type_id != event.event_type_id:
            if (
                get_visible_dictionary_item(
                    session,
                    DictionaryCategory.EVENT,
                    user_id,
                    target_event_type_id,
                    require_active=True,
                )
                is None
            ):
                raise LedgerValidationError("EVENT_TYPE_UNAVAILABLE", "所选事件类型不可用")

        target_identity = {
            "host_contact_id": target_host_id,
            "kinship_type_id": target_kinship_id,
            "event_type_id": target_event_type_id,
            "remark": target_remark,
        }
        conflict_id = _event_identity_conflict(
            session,
            user_id,
            event_id=event.id,
            **target_identity,
        )
        if conflict_id is not None:
            raise EventIdentityConflictError(conflict_id)

        if "host_contact_id" in fields:
            event.contact_id = target_host_id
        if "kinship_type_id" in fields:
            event.relative_type_id = target_kinship_id
        if "event_type_id" in fields:
            event.event_type_id = target_event_type_id
        if "event_date" in fields:
            incoming_date = parse_event_date(target_date)
            current_date = date_value(event)
            if current_date.precision != "day":
                try:
                    merge_event_dates(current_date, incoming_date)
                except ValueError:
                    raise LedgerValidationError(
                        "EVENT_DATE_KNOWN_PART_CONFLICT", "补齐日期必须与原来已知的年份、月份一致"
                    ) from None
            event.event_date = target_date
            event.date_precision = "day"
            event.event_year = target_date.year
            event.event_month = target_date.month
        if "remark" in fields:
            event.remark = remark
        event.identity_hash = event_identity_hash(
            target_host_id, target_kinship_id, target_event_type_id, target_remark
        )
        session.flush()
        session.commit()
        return event
    except IntegrityError as exc:
        session.rollback()
        if target_identity is None:
            raise
        conflict_id = _event_identity_conflict(
            session,
            user_id,
            event_id=event_id,
            **target_identity,
        )
        session.rollback()
        if conflict_id is not None:
            raise EventIdentityConflictError(conflict_id) from exc
        raise
    except Exception:
        session.rollback()
        raise


def delete_event(session: Session, user_id: int, event_id: int) -> None:
    try:
        _lock_active_user(session, user_id)
        event = _locked_event_by_id(session, user_id, event_id)
        if _event_has_gifts(session, user_id, event.id):
            raise EventHasGiftsError
        session.delete(event)
        session.flush()
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise EventHasGiftsError from exc
    except Exception:
        session.rollback()
        raise


def _gift_conflict(
    session: Session,
    user_id: int,
    event_id: int,
    giver_contact_id: int,
    *,
    exclude_gift_id: int,
) -> int | None:
    return session.scalar(
        select(GiftRecord.id)
        .where(
            GiftRecord.user_id == user_id,
            GiftRecord.event_record_id == event_id,
            GiftRecord.contact_id == giver_contact_id,
            GiftRecord.id != exclude_gift_id,
        )
        .with_for_update()
    )


def update_gift(
    session: Session,
    user_id: int,
    gift_id: int,
    *,
    giver_contact_id: int | None,
    amount: Decimal | None,
    gift_details: str | None,
    remark: str | None,
    fields: set[str],
) -> GiftUpdateResult:
    allowed_fields = {"giver_contact_id", "amount", "gift_details", "remark"}
    if not fields:
        raise LedgerValidationError("GIFT_UPDATE_EMPTY", "至少需要提交一个要修改的流水字段")
    if fields - allowed_fields:
        raise LedgerValidationError("GIFT_FIELD_FORBIDDEN", "提交了不允许修改的流水字段")
    if "giver_contact_id" in fields and giver_contact_id is None:
        raise LedgerValidationError("GIVER_CONTACT_REQUIRED", "随礼人不能设为空")
    if "amount" in fields and amount is None:
        raise LedgerValidationError("GIFT_AMOUNT_REQUIRED", "礼金金额不能设为空")

    target_event_id: int | None = None
    target_giver_id: int | None = None
    try:
        _lock_active_user(session, user_id)
        gift = _locked_gift_by_id(session, user_id, gift_id)
        event = _locked_event_by_id(session, user_id, gift.event_record_id)
        self_contact = _self_contact(session, user_id)
        target_event_id = event.id
        target_giver_id = giver_contact_id if "giver_contact_id" in fields else gift.contact_id
        assert target_giver_id is not None

        if target_giver_id != gift.contact_id:
            if event.contact_id == self_contact.id:
                _active_contact(session, user_id, target_giver_id, role="giver")
            elif target_giver_id != self_contact.id:
                raise LedgerValidationError(
                    "EXPENSE_GIVER_MUST_BE_SELF",
                    "别人办事时随礼人必须是本人",
                )
            derive_direction(
                self_contact_id=self_contact.id,
                host_contact_id=event.contact_id,
                giver_contact_id=target_giver_id,
            )
            conflict_id = _gift_conflict(
                session,
                user_id,
                event.id,
                target_giver_id,
                exclude_gift_id=gift.id,
            )
            if conflict_id is not None:
                raise GiftAlreadyExistsError(conflict_id)

        if "amount" in fields or "gift_details" in fields:
            target_amount = amount if "amount" in fields else gift.amount
            target_details = gift_details if "gift_details" in fields else gift.gift_details
            assert target_amount is not None
            validated_amount, normalized_details = validate_gift_value(
                target_amount,
                target_details,
            )
            if "amount" in fields:
                gift.amount = validated_amount
            if "gift_details" in fields:
                gift.gift_details = normalized_details
        if "giver_contact_id" in fields:
            gift.contact_id = target_giver_id
        if "remark" in fields:
            gift.remark = remark

        direction = derive_direction(
            self_contact_id=self_contact.id,
            host_contact_id=event.contact_id,
            giver_contact_id=gift.contact_id,
        )
        session.flush()
        session.commit()
        return GiftUpdateResult(event=event, gift=gift, direction=direction)
    except IntegrityError as exc:
        session.rollback()
        if target_event_id is None or target_giver_id is None:
            raise
        conflict_id = _gift_conflict(
            session,
            user_id,
            target_event_id,
            target_giver_id,
            exclude_gift_id=gift_id,
        )
        session.rollback()
        if conflict_id is not None:
            raise GiftAlreadyExistsError(conflict_id) from exc
        raise
    except Exception:
        session.rollback()
        raise


def delete_gift(session: Session, user_id: int, gift_id: int) -> None:
    try:
        _lock_active_user(session, user_id)
        gift = _locked_gift_by_id(session, user_id, gift_id)
        session.delete(gift)
        session.flush()
        session.commit()
    except Exception:
        session.rollback()
        raise


def event_response(view: EventView) -> EventResponse:
    return EventResponse(
        id=view.event.id,
        host=LedgerContactResponse(id=view.host_contact.id, name=view.host_contact.name),
        kinship_type=LedgerDictionaryResponse(
            id=view.kinship_type.id,
            name=view.kinship_type.type_name,
        ),
        event_type=LedgerDictionaryResponse(
            id=view.event_type.id,
            name=view.event_type.type_name,
        ),
        **event_date_response_fields(view.event),
        remark=view.event.remark,
        created_at=view.event.created_at,
    )


def gift_response(view: GiftView) -> GiftResponse:
    return GiftResponse(
        id=view.gift.id,
        giver=LedgerContactResponse(id=view.giver_contact.id, name=view.giver_contact.name),
        amount=view.gift.amount,
        gift_details=view.gift.gift_details,
        remark=view.gift.remark,
        direction=view.direction,
        created_at=view.gift.created_at,
    )


def event_detail_response(view: EventDetailView) -> EventDetailResponse:
    return EventDetailResponse(
        event=event_response(view.event),
        gifts=[gift_response(gift) for gift in view.gifts],
    )


def gift_detail_response(view: GiftDetailView) -> GiftDetailResponse:
    return GiftDetailResponse(
        event=event_response(view.event),
        gift=gift_response(view.gift),
    )
