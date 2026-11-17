from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import and_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import Contact, EventRecord, EventType, GiftRecord, KinshipType, User
from app.schemas.dictionaries import DictionaryCategory
from app.schemas.ledger import LedgerDirection
from app.services.dictionaries import get_visible_dictionary_item
from app.services.event_dates import date_value
from app.services.event_identity import event_identity_components, event_identity_hash
from app.services.profile import get_self_contact

MAX_GIFT_AMOUNT = Decimal("99999999.99")


class LedgerValidationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class GiftAlreadyExistsError(ValueError):
    def __init__(self, gift_id: int) -> None:
        super().__init__("该随礼人在这个事件中已有流水，请编辑原流水")
        self.gift_id = gift_id


class EventAlreadyExistsError(ValueError):
    def __init__(self, event_id: int) -> None:
        super().__init__("这个事件已经登记，请查看或修改原事件")
        self.event_id = event_id


@dataclass(frozen=True)
class LedgerValidationContext:
    self_contact: Contact
    host_contact: Contact
    giver_contact: Contact
    kinship_type: KinshipType
    event_type: EventType
    direction: LedgerDirection


@dataclass(frozen=True)
class LedgerEntryCreation:
    event: EventRecord
    gift: GiftRecord
    direction: LedgerDirection
    event_reused: bool


def normalize_gift_details(gift_details: str | None) -> str | None:
    if gift_details is None:
        return None
    normalized = gift_details.strip()
    if not normalized:
        return None
    if len(normalized) > 255:
        raise LedgerValidationError(
            "GIFT_DETAILS_TOO_LONG",
            "实物明细最多允许 255 个字符",
        )
    return normalized


def validate_gift_value(
    amount: Decimal,
    gift_details: str | None,
) -> tuple[Decimal, str | None]:
    if not amount.is_finite():
        raise LedgerValidationError("GIFT_AMOUNT_INVALID", "礼金金额格式不正确")
    if amount < 0:
        raise LedgerValidationError("GIFT_AMOUNT_NEGATIVE", "礼金金额不能为负数")
    if amount > MAX_GIFT_AMOUNT:
        raise LedgerValidationError(
            "GIFT_AMOUNT_TOO_LARGE",
            "礼金金额不能超过 99999999.99 元",
        )
    if amount != amount.quantize(Decimal("0.01")):
        raise LedgerValidationError(
            "GIFT_AMOUNT_DECIMAL_PLACES",
            "礼金金额最多保留两位小数",
        )
    normalized_details = normalize_gift_details(gift_details)
    if amount == 0 and normalized_details is None:
        raise LedgerValidationError(
            "GIFT_VALUE_REQUIRED",
            "礼金金额为 0 时必须填写实物明细",
        )
    return amount, normalized_details


def derive_direction(
    *,
    self_contact_id: int,
    host_contact_id: int,
    giver_contact_id: int,
) -> LedgerDirection:
    if host_contact_id == giver_contact_id:
        raise LedgerValidationError("HOST_GIVER_SAME", "事主和随礼人不能是同一个人")
    if host_contact_id == self_contact_id:
        return LedgerDirection.INCOME
    if giver_contact_id == self_contact_id:
        return LedgerDirection.EXPENSE
    raise LedgerValidationError(
        "THIRD_PARTY_LEDGER_FORBIDDEN",
        "只能记录本家庭参与的礼金往来",
    )


def _active_contact(
    session: Session,
    user_id: int,
    contact_id: int,
    *,
    role: str,
) -> Contact:
    contact = session.scalar(
        select(Contact).where(
            Contact.id == contact_id,
            Contact.user_id == user_id,
            Contact.is_active == 1,
        )
    )
    if contact is None:
        raise LedgerValidationError(
            f"{role.upper()}_CONTACT_UNAVAILABLE",
            "所选事主不可用" if role == "host" else "所选随礼人不可用",
        )
    return contact


def validate_ledger_context(
    session: Session,
    user_id: int,
    *,
    host_contact_id: int,
    giver_contact_id: int | None,
    kinship_type_id: int,
    event_type_id: int,
) -> LedgerValidationContext:
    self_contact = get_self_contact(session, user_id)
    if self_contact is None or not self_contact.is_active:
        raise LedgerValidationError("SELF_CONTACT_REQUIRED", "请先完成本人信息建档")

    host_contact = _active_contact(
        session,
        user_id,
        host_contact_id,
        role="host",
    )
    if host_contact.id == self_contact.id:
        if giver_contact_id is None:
            raise LedgerValidationError(
                "GIVER_CONTACT_REQUIRED",
                "我家办事时必须选择随礼人",
            )
        giver_contact = _active_contact(
            session,
            user_id,
            giver_contact_id,
            role="giver",
        )
    else:
        if giver_contact_id is not None and giver_contact_id != self_contact.id:
            raise LedgerValidationError(
                "EXPENSE_GIVER_MUST_BE_SELF",
                "别人办事时随礼人必须是本人",
            )
        giver_contact = self_contact

    direction = derive_direction(
        self_contact_id=self_contact.id,
        host_contact_id=host_contact.id,
        giver_contact_id=giver_contact.id,
    )
    kinship_type = get_visible_dictionary_item(
        session,
        DictionaryCategory.KINSHIP,
        user_id,
        kinship_type_id,
        require_active=True,
    )
    if kinship_type is None:
        raise LedgerValidationError("KINSHIP_TYPE_UNAVAILABLE", "所选亲属称谓不可用")
    event_type = get_visible_dictionary_item(
        session,
        DictionaryCategory.EVENT,
        user_id,
        event_type_id,
        require_active=True,
    )
    if event_type is None:
        raise LedgerValidationError("EVENT_TYPE_UNAVAILABLE", "所选事件类型不可用")

    return LedgerValidationContext(
        self_contact=self_contact,
        host_contact=host_contact,
        giver_contact=giver_contact,
        kinship_type=kinship_type,
        event_type=event_type,
        direction=direction,
    )


def _lock_active_user(session: Session, user_id: int) -> User:
    user = session.scalar(
        select(User).where(User.id == user_id, User.is_active == 1).with_for_update()
    )
    if user is None:
        raise LedgerValidationError("ACCOUNT_UNAVAILABLE", "账号不可用，请重新登录")
    return user


def _event_statement(
    user_id: int,
    *,
    host_contact_id: int,
    kinship_type_id: int,
    event_type_id: int,
    event_remark: str | None,
):
    return select(EventRecord).where(
        EventRecord.user_id == user_id,
        EventRecord.identity_hash
        == event_identity_hash(host_contact_id, kinship_type_id, event_type_id, event_remark),
    )


def find_event_by_identity(
    session: Session,
    user_id: int,
    *,
    host_contact_id: int,
    kinship_type_id: int,
    event_type_id: int,
    event_remark: str | None,
    exclude_event_id: int | None = None,
    lock: bool = False,
) -> EventRecord | None:
    statement = _event_statement(
        user_id,
        host_contact_id=host_contact_id,
        kinship_type_id=kinship_type_id,
        event_type_id=event_type_id,
        event_remark=event_remark,
    )
    if exclude_event_id is not None:
        statement = statement.where(EventRecord.id != exclude_event_id)
    result = session.scalar(statement.with_for_update() if lock else statement)
    if result is not None and event_identity_components(
        result.contact_id, result.relative_type_id, result.event_type_id, result.remark
    ) != event_identity_components(host_contact_id, kinship_type_id, event_type_id, event_remark):
        raise LedgerValidationError("EVENT_IDENTITY_COLLISION", "事件身份核对失败，请联系管理员")
    return result


def _locked_event(session: Session, user_id: int, **identity) -> EventRecord | None:
    return find_event_by_identity(session, user_id, **identity, lock=True)


def _create_or_reuse_event(
    session: Session,
    user_id: int,
    *,
    host_contact_id: int,
    kinship_type_id: int,
    event_type_id: int,
    event_date: date,
    event_remark: str | None,
) -> tuple[EventRecord, bool]:
    lookup = {
        "host_contact_id": host_contact_id,
        "kinship_type_id": kinship_type_id,
        "event_type_id": event_type_id,
        "event_remark": event_remark,
    }
    existing = _locked_event(session, user_id, **lookup)
    if existing is not None:
        return existing, True

    event = EventRecord(
        user_id=user_id,
        contact_id=host_contact_id,
        relative_type_id=kinship_type_id,
        event_type_id=event_type_id,
        event_date=event_date,
        remark=event_remark,
        identity_hash=event_identity_hash(
            host_contact_id, kinship_type_id, event_type_id, event_remark
        ),
    )
    try:
        with session.begin_nested():
            session.add(event)
            session.flush()
    except IntegrityError:
        concurrent = _locked_event(session, user_id, **lookup)
        if concurrent is None:
            raise
        return concurrent, True
    return event, False


def _locked_gift(
    session: Session,
    user_id: int,
    event_id: int,
    giver_contact_id: int,
) -> GiftRecord | None:
    return session.scalar(
        select(GiftRecord)
        .where(
            GiftRecord.user_id == user_id,
            GiftRecord.event_record_id == event_id,
            GiftRecord.contact_id == giver_contact_id,
        )
        .with_for_update()
    )


def _gift_for_event_identity(
    session: Session,
    user_id: int,
    *,
    host_contact_id: int,
    kinship_type_id: int,
    event_type_id: int,
    event_remark: str | None,
    giver_contact_id: int,
) -> GiftRecord | None:
    return session.scalar(
        select(GiftRecord)
        .join(
            EventRecord,
            and_(
                EventRecord.user_id == GiftRecord.user_id,
                EventRecord.id == GiftRecord.event_record_id,
            ),
        )
        .where(
            GiftRecord.user_id == user_id,
            GiftRecord.contact_id == giver_contact_id,
            EventRecord.identity_hash
            == event_identity_hash(host_contact_id, kinship_type_id, event_type_id, event_remark),
        )
    )


def create_ledger_entry(
    session: Session,
    user_id: int,
    *,
    host_contact_id: int,
    kinship_type_id: int,
    event_type_id: int,
    event_date: date,
    event_remark: str | None,
    giver_contact_id: int | None,
    amount: Decimal,
    gift_details: str | None,
    gift_remark: str | None,
) -> LedgerEntryCreation:
    if event_date < date(1000, 1, 1):
        raise LedgerValidationError("EVENT_DATE_INVALID", "办事日期格式不正确")
    validated_amount, normalized_gift_details = validate_gift_value(amount, gift_details)
    identity = {
        "host_contact_id": host_contact_id,
        "kinship_type_id": kinship_type_id,
        "event_type_id": event_type_id,
        "event_remark": event_remark,
    }
    resolved_giver_id: int | None = None
    try:
        _lock_active_user(session, user_id)
        context = validate_ledger_context(
            session,
            user_id,
            host_contact_id=host_contact_id,
            giver_contact_id=giver_contact_id,
            kinship_type_id=kinship_type_id,
            event_type_id=event_type_id,
        )
        resolved_giver_id = context.giver_contact.id
        event, event_reused = _create_or_reuse_event(
            session,
            user_id,
            **identity,
            event_date=event_date,
        )
        if date_value(event).precision != "day":
            raise LedgerValidationError(
                "EVENT_DATE_INCOMPLETE", "此旧账事件的办事日期不完整，请先补齐日期再新增记账"
            )
        existing_gift = _locked_gift(
            session,
            user_id,
            event.id,
            resolved_giver_id,
        )
        if existing_gift is not None:
            raise GiftAlreadyExistsError(existing_gift.id)

        gift = GiftRecord(
            user_id=user_id,
            event_record_id=event.id,
            contact_id=resolved_giver_id,
            amount=validated_amount,
            gift_details=normalized_gift_details,
            remark=gift_remark,
        )
        session.add(gift)
        session.flush()
        session.commit()
        return LedgerEntryCreation(
            event=event,
            gift=gift,
            direction=context.direction,
            event_reused=event_reused,
        )
    except IntegrityError as exc:
        session.rollback()
        if resolved_giver_id is None:
            raise
        existing_gift = _gift_for_event_identity(
            session,
            user_id,
            **identity,
            giver_contact_id=resolved_giver_id,
        )
        session.rollback()
        if existing_gift is not None:
            raise GiftAlreadyExistsError(existing_gift.id) from exc
        raise
    except Exception:
        session.rollback()
        raise
