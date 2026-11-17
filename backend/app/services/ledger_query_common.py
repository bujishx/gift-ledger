from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, case, func, literal, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.db.models import EventRecord, GiftRecord
from app.schemas.dictionaries import DictionaryCategory
from app.schemas.ledger import LedgerDirection
from app.schemas.ledger_queries import (
    GiftsQueryFilters,
    InteractionStatus,
    InteractionStatusResponse,
    LedgerQueryFilters,
)
from app.services.contacts import get_contact
from app.services.dictionaries import get_visible_dictionary_item
from app.services.profile import get_self_contact


class LedgerQueryValidationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class LedgerQueryContext:
    user_id: int
    self_contact_id: int
    filters: LedgerQueryFilters


INTERACTION_STATUS_LABELS = {
    InteractionStatus.NONE: "暂无往来",
    InteractionStatus.COUNTERPARTY_UNRETURNED: "对方未回礼",
    InteractionStatus.SELF_UNRETURNED: "我未回礼",
    InteractionStatus.MUTUAL: "礼尚往来",
}

INTERACTION_STATUS_SORT_RANKS = {
    InteractionStatus.NONE: 3,
    InteractionStatus.SELF_UNRETURNED: 0,
    InteractionStatus.COUNTERPARTY_UNRETURNED: 1,
    InteractionStatus.MUTUAL: 2,
}


def derive_interaction_status(
    *,
    has_expense: bool,
    has_income: bool,
) -> InteractionStatus:
    if has_expense and has_income:
        return InteractionStatus.MUTUAL
    if has_expense:
        return InteractionStatus.COUNTERPARTY_UNRETURNED
    if has_income:
        return InteractionStatus.SELF_UNRETURNED
    raise ValueError("没有往来流水时不能生成来往状态")


def interaction_status_response(status: InteractionStatus) -> InteractionStatusResponse:
    return InteractionStatusResponse(code=status, label=INTERACTION_STATUS_LABELS[status])


def direction_expression(self_contact_id: int) -> ColumnElement[str]:
    return case(
        (
            EventRecord.contact_id == self_contact_id,
            literal(LedgerDirection.INCOME.value),
        ),
        else_=literal(LedgerDirection.EXPENSE.value),
    ).label("direction")


def counterparty_id_expression(self_contact_id: int) -> ColumnElement[int]:
    return case(
        (EventRecord.contact_id == self_contact_id, GiftRecord.contact_id),
        else_=EventRecord.contact_id,
    ).label("counterparty_id")


def valid_participation_expression(self_contact_id: int) -> ColumnElement[bool]:
    return or_(
        and_(
            EventRecord.contact_id == self_contact_id,
            GiftRecord.contact_id != self_contact_id,
        ),
        and_(
            EventRecord.contact_id != self_contact_id,
            GiftRecord.contact_id == self_contact_id,
        ),
    )


def interaction_status_expression(
    has_expense: ColumnElement[Any],
    has_income: ColumnElement[Any],
) -> ColumnElement[str]:
    return case(
        (
            and_(has_expense == 1, has_income == 1),
            literal(InteractionStatus.MUTUAL.value),
        ),
        (
            has_expense == 1,
            literal(InteractionStatus.COUNTERPARTY_UNRETURNED.value),
        ),
        else_=literal(InteractionStatus.SELF_UNRETURNED.value),
    ).label("interaction_status")


def interaction_status_rank_expression(
    status: ColumnElement[Any],
) -> ColumnElement[int]:
    return case(
        (
            status == InteractionStatus.SELF_UNRETURNED.value,
            INTERACTION_STATUS_SORT_RANKS[InteractionStatus.SELF_UNRETURNED],
        ),
        (
            status == InteractionStatus.COUNTERPARTY_UNRETURNED.value,
            INTERACTION_STATUS_SORT_RANKS[InteractionStatus.COUNTERPARTY_UNRETURNED],
        ),
        else_=INTERACTION_STATUS_SORT_RANKS[InteractionStatus.MUTUAL],
    ).label("interaction_status_rank")


def interaction_history_subquery(user_id: int, self_contact_id: int):
    counterparty_id = counterparty_id_expression(self_contact_id)
    has_expense = func.max(case((EventRecord.contact_id != self_contact_id, 1), else_=0)).label(
        "has_expense"
    )
    has_income = func.max(case((EventRecord.contact_id == self_contact_id, 1), else_=0)).label(
        "has_income"
    )
    history = (
        select(
            counterparty_id,
            has_expense,
            has_income,
        )
        .select_from(GiftRecord)
        .join(
            EventRecord,
            and_(
                EventRecord.id == GiftRecord.event_record_id,
                EventRecord.user_id == GiftRecord.user_id,
            ),
        )
        .where(
            GiftRecord.user_id == user_id,
            EventRecord.user_id == user_id,
            valid_participation_expression(self_contact_id),
        )
        .group_by(counterparty_id)
        .subquery("interaction_history_base")
    )
    status = interaction_status_expression(history.c.has_expense, history.c.has_income)
    return select(
        history.c.counterparty_id,
        history.c.has_expense,
        history.c.has_income,
        status,
        interaction_status_rank_expression(status),
    ).subquery("interaction_history")


def validate_query_context(
    session: Session,
    user_id: int,
    filters: LedgerQueryFilters,
) -> LedgerQueryContext:
    self_contact = get_self_contact(session, user_id)
    if self_contact is None or not self_contact.is_active:
        raise LedgerQueryValidationError("SELF_CONTACT_REQUIRED", "请先完成本人信息建档")

    if filters.contact_id is not None:
        contact = get_contact(session, user_id, filters.contact_id)
        if contact is None:
            raise LedgerQueryValidationError(
                "CONTACT_FILTER_UNAVAILABLE",
                "所选联系人不可用",
            )

    if isinstance(filters, GiftsQueryFilters) and filters.event_id is not None:
        event = session.scalar(
            select(EventRecord.id).where(
                EventRecord.id == filters.event_id,
                EventRecord.user_id == user_id,
            )
        )
        if event is None:
            raise LedgerQueryValidationError(
                "EVENT_FILTER_UNAVAILABLE",
                "所选事件不可用",
            )

    dictionary_filters = (
        (
            DictionaryCategory.RELATIONSHIP,
            filters.relationship_type_id,
            "RELATIONSHIP_FILTER_UNAVAILABLE",
            "所选关系类型不可用",
        ),
        (
            DictionaryCategory.KINSHIP,
            filters.kinship_type_id,
            "KINSHIP_FILTER_UNAVAILABLE",
            "所选亲属称谓不可用",
        ),
        (
            DictionaryCategory.EVENT,
            filters.event_type_id,
            "EVENT_TYPE_FILTER_UNAVAILABLE",
            "所选事件类型不可用",
        ),
    )
    for category, item_id, code, message in dictionary_filters:
        if item_id is None:
            continue
        item = get_visible_dictionary_item(
            session,
            category,
            user_id,
            item_id,
            require_active=False,
        )
        if item is None:
            raise LedgerQueryValidationError(code, message)

    if isinstance(filters, GiftsQueryFilters):
        for category, ids, code, message in (
            (
                DictionaryCategory.RELATIONSHIP,
                filters.counterparty_relationship_type_ids,
                "RELATIONSHIP_FILTER_UNAVAILABLE",
                "所选往来关系不可用",
            ),
            (
                DictionaryCategory.EVENT,
                filters.event_type_ids,
                "EVENT_TYPE_FILTER_UNAVAILABLE",
                "所选事件类型不可用",
            ),
            (
                DictionaryCategory.EVENT,
                filters.statistics_event_type_ids,
                "EVENT_TYPE_FILTER_UNAVAILABLE",
                "所选统计事件类型不可用",
            ),
        ):
            for item_id in set(ids or []):
                if get_visible_dictionary_item(
                    session, category, user_id, item_id, require_active=False
                ) is None:
                    raise LedgerQueryValidationError(code, message)

    return LedgerQueryContext(
        user_id=user_id,
        self_contact_id=self_contact.id,
        filters=filters,
    )
