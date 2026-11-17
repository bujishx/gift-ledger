"""四类统计的只读服务。

方向与对方联系人由事主、随礼人和本人联系人的相对关系推导。
全部统计强制当前用户隔离并按办事日期归属年份。
金额由数据库定点数汇总。
"""

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import Select, and_, case, cast, func, literal, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.sql.functions import Function

from app.db.models import Contact, EventRecord, EventType, GiftRecord, RelationshipType
from app.schemas.ledger import LedgerContactResponse
from app.schemas.ledger_queries import QueryDictionaryResponse
from app.schemas.statistics import (
    STATISTICS_MAX_YEAR,
    STATISTICS_MIN_YEAR,
    BalanceStatus,
    BalanceStatusResponse,
    EventDistributionItemResponse,
    EventDistributionResponse,
    ReciprocityItemResponse,
    ReciprocityResponse,
    RelationshipCircleItemResponse,
    RelationshipCircleResponse,
    TimelineMonthResponse,
    TimelineResponse,
)
from app.services.event_dates import date_period_conditions
from app.services.ledger_query_common import (
    counterparty_id_expression,
    valid_participation_expression,
)
from app.services.profile import get_self_contact
from app.services.system_dictionary_catalog import SELF_RELATIONSHIP_SYSTEM_CODE

BALANCE_STATUS_LABELS = {
    BalanceStatus.EXPENSE_HEAVIER: "我多随出",
    BalanceStatus.INCOME_HEAVIER: "我多收到",
    BalanceStatus.BALANCED: "礼金持平",
}

ZERO_AMOUNT = Decimal("0.00")
RATIO_QUANTUM = Decimal("0.01")
PERCENT_SCALE = Decimal("100")


class StatisticsValidationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class StatisticsContext:
    user_id: int
    self_contact_id: int


def statistics_context(session: Session, user_id: int) -> StatisticsContext:
    self_contact = get_self_contact(session, user_id)
    if self_contact is None or not self_contact.is_active:
        raise StatisticsValidationError("SELF_CONTACT_REQUIRED", "请先完成本人信息建档")
    return StatisticsContext(user_id=user_id, self_contact_id=self_contact.id)


def validate_statistics_year(year: int) -> int:
    if not STATISTICS_MIN_YEAR <= year <= STATISTICS_MAX_YEAR:
        raise StatisticsValidationError(
            "STATISTICS_YEAR_INVALID",
            f"统计年份必须在 {STATISTICS_MIN_YEAR} 到 {STATISTICS_MAX_YEAR} 之间",
        )
    return year


def _gift_event_join() -> ColumnElement[bool]:
    return and_(
        EventRecord.id == GiftRecord.event_record_id,
        EventRecord.user_id == GiftRecord.user_id,
    )


def _participation_conditions(user_id: int, self_contact_id: int) -> list[ColumnElement[bool]]:
    return [
        GiftRecord.user_id == user_id,
        EventRecord.user_id == user_id,
        valid_participation_expression(self_contact_id),
    ]


def _expense_condition(self_contact_id: int) -> ColumnElement[bool]:
    return and_(
        EventRecord.contact_id != self_contact_id,
        GiftRecord.contact_id == self_contact_id,
    )


def _income_condition(self_contact_id: int) -> ColumnElement[bool]:
    return and_(
        EventRecord.contact_id == self_contact_id,
        GiftRecord.contact_id != self_contact_id,
    )


def _money_sum(condition: ColumnElement[bool]) -> Function[Any]:
    zero = cast(0, GiftRecord.amount.type)
    return func.sum(case((condition, GiftRecord.amount), else_=zero))


def _count_sum(condition: ColumnElement[bool]) -> Function[Any]:
    return func.sum(case((condition, literal(1)), else_=literal(0)))


def _goods_count() -> Function[Any]:
    has_goods = and_(
        GiftRecord.gift_details.is_not(None),
        func.trim(GiftRecord.gift_details) != literal(""),
    )
    return _count_sum(has_goods)


def _year_date_range(year: int) -> list[ColumnElement[bool]]:
    return date_period_conditions(date(year, 1, 1), date(year, 12, 31))


def reciprocity_statement(user_id: int, self_contact_id: int) -> Select:
    counterparty_id = counterparty_id_expression(self_contact_id)
    expense_amount = _money_sum(_expense_condition(self_contact_id)).label("expense_amount")
    income_amount = _money_sum(_income_condition(self_contact_id)).label("income_amount")
    expense_count = _count_sum(_expense_condition(self_contact_id)).label("expense_count")
    income_count = _count_sum(_income_condition(self_contact_id)).label("income_count")
    goods_count = _goods_count().label("goods_count")
    balance = income_amount - expense_amount
    return (
        select(
            Contact.id.label("contact_id"),
            Contact.name.label("contact_name"),
            expense_amount,
            income_amount,
            expense_count,
            income_count,
            goods_count,
            balance.label("balance_amount"),
        )
        .select_from(GiftRecord)
        .join(EventRecord, _gift_event_join())
        .join(
            Contact,
            and_(
                Contact.id == counterparty_id,
                Contact.user_id == GiftRecord.user_id,
            ),
        )
        .where(*_participation_conditions(user_id, self_contact_id))
        .group_by(Contact.id, Contact.name)
        .order_by(func.abs(balance).desc(), Contact.id.asc())
    )


def timeline_month_statement(user_id: int, self_contact_id: int, year: int) -> Select:
    month = EventRecord.event_month
    return (
        select(
            month.label("month"),
            _money_sum(_expense_condition(self_contact_id)).label("expense_amount"),
            _money_sum(_income_condition(self_contact_id)).label("income_amount"),
            func.count(GiftRecord.id).label("gift_count"),
            _goods_count().label("goods_count"),
        )
        .select_from(GiftRecord)
        .join(EventRecord, _gift_event_join())
        .where(
            *_participation_conditions(user_id, self_contact_id),
            *_year_date_range(year),
        )
        .group_by(month)
        .order_by(month.asc())
    )


def available_years_statement(user_id: int, self_contact_id: int) -> Select:
    year = EventRecord.event_year
    return (
        select(year.label("year"))
        .select_from(GiftRecord)
        .join(EventRecord, _gift_event_join())
        .where(*_participation_conditions(user_id, self_contact_id), year.is_not(None))
        .group_by(year)
        .order_by(year.desc())
    )


def event_distribution_statement(user_id: int, self_contact_id: int, year: int) -> Select:
    cash_amount = func.sum(GiftRecord.amount).label("cash_amount")
    return (
        select(
            EventType.id.label("event_type_id"),
            EventType.type_name.label("event_type_name"),
            cash_amount,
            func.count(GiftRecord.id).label("gift_count"),
            _goods_count().label("goods_count"),
        )
        .select_from(GiftRecord)
        .join(EventRecord, _gift_event_join())
        .join(
            EventType,
            and_(
                EventType.id == EventRecord.event_type_id,
                or_(EventType.user_id.is_(None), EventType.user_id == user_id),
            ),
        )
        .where(
            *_participation_conditions(user_id, self_contact_id),
            _expense_condition(self_contact_id),
            *_year_date_range(year),
        )
        .group_by(EventType.id, EventType.type_name)
        .order_by(cash_amount.desc(), EventType.id.asc())
    )


def relationship_circle_statement(user_id: int, self_contact_id: int) -> Select:
    counterparty_id = counterparty_id_expression(self_contact_id)
    expense_amount = _money_sum(_expense_condition(self_contact_id)).label("expense_amount")
    income_amount = _money_sum(_income_condition(self_contact_id)).label("income_amount")
    combined_total = expense_amount + income_amount
    return (
        select(
            RelationshipType.id.label("relationship_type_id"),
            RelationshipType.type_name.label("relationship_type_name"),
            expense_amount,
            income_amount,
            func.count(GiftRecord.id).label("gift_count"),
            _goods_count().label("goods_count"),
        )
        .select_from(GiftRecord)
        .join(EventRecord, _gift_event_join())
        .join(
            Contact,
            and_(
                Contact.id == counterparty_id,
                Contact.user_id == GiftRecord.user_id,
            ),
        )
        .join(
            RelationshipType,
            and_(
                RelationshipType.id == Contact.relation_type_id,
                or_(
                    RelationshipType.system_code.is_(None),
                    RelationshipType.system_code != SELF_RELATIONSHIP_SYSTEM_CODE,
                ),
                or_(
                    RelationshipType.user_id.is_(None),
                    RelationshipType.user_id == user_id,
                ),
            ),
        )
        .where(*_participation_conditions(user_id, self_contact_id))
        .group_by(RelationshipType.id, RelationshipType.type_name)
        .order_by(combined_total.desc(), RelationshipType.id.asc())
    )


def _balance_status(balance_amount: Decimal) -> BalanceStatusResponse:
    if balance_amount > 0:
        status = BalanceStatus.INCOME_HEAVIER
    elif balance_amount < 0:
        status = BalanceStatus.EXPENSE_HEAVIER
    else:
        status = BalanceStatus.BALANCED
    return BalanceStatusResponse(code=status, label=BALANCE_STATUS_LABELS[status])


def reciprocity_summary(session: Session, user_id: int) -> ReciprocityResponse:
    context = statistics_context(session, user_id)
    rows = session.execute(reciprocity_statement(context.user_id, context.self_contact_id)).all()
    items = [
        ReciprocityItemResponse(
            contact=LedgerContactResponse(id=row.contact_id, name=row.contact_name),
            expense_amount=row.expense_amount,
            expense_count=row.expense_count,
            income_amount=row.income_amount,
            income_count=row.income_count,
            goods_count=row.goods_count,
            balance_amount=row.balance_amount,
            balance_status=_balance_status(row.balance_amount),
        )
        for row in rows
    ]
    return ReciprocityResponse(items=items)


def _timeline_months(rows: list[Any]) -> list[TimelineMonthResponse]:
    monthly = {int(row.month): row for row in rows if row.month is not None}
    months = []
    for month_number in range(1, 13):
        row = monthly.get(month_number)
        if row is None:
            months.append(
                TimelineMonthResponse(
                    month=month_number,
                    expense_amount=ZERO_AMOUNT,
                    income_amount=ZERO_AMOUNT,
                    gift_count=0,
                    goods_count=0,
                )
            )
        else:
            months.append(
                TimelineMonthResponse(
                    month=month_number,
                    expense_amount=row.expense_amount,
                    income_amount=row.income_amount,
                    gift_count=row.gift_count,
                    goods_count=row.goods_count,
                )
            )
    return months


def timeline_statistics(session: Session, user_id: int, year: int) -> TimelineResponse:
    validate_statistics_year(year)
    context = statistics_context(session, user_id)
    month_rows = session.execute(
        timeline_month_statement(context.user_id, context.self_contact_id, year)
    ).all()
    year_rows = session.execute(
        available_years_statement(context.user_id, context.self_contact_id)
    ).all()
    months = _timeline_months(month_rows)
    year_expense = sum((row.expense_amount for row in month_rows), ZERO_AMOUNT)
    year_income = sum((row.income_amount for row in month_rows), ZERO_AMOUNT)
    available_years = [int(row.year) for row in year_rows] or [date.today().year]
    return TimelineResponse(
        year=year,
        months=months,
        year_expense_amount=year_expense,
        year_income_amount=year_income,
        net_amount=year_income - year_expense,
        available_years=available_years,
        month_unassigned_expense_amount=sum(
            (row.expense_amount for row in month_rows if row.month is None), ZERO_AMOUNT
        ),
        month_unassigned_income_amount=sum(
            (row.income_amount for row in month_rows if row.month is None), ZERO_AMOUNT
        ),
        month_unassigned_gift_count=sum(row.gift_count for row in month_rows if row.month is None),
    )


def _cash_ratio_percent(cash_amount: Decimal, total_cash: Decimal) -> Decimal:
    if total_cash <= 0:
        return ZERO_AMOUNT
    ratio = cash_amount / total_cash * PERCENT_SCALE
    return ratio.quantize(RATIO_QUANTUM, rounding=ROUND_HALF_UP)


def event_distribution(session: Session, user_id: int, year: int) -> EventDistributionResponse:
    validate_statistics_year(year)
    context = statistics_context(session, user_id)
    rows = session.execute(
        event_distribution_statement(context.user_id, context.self_contact_id, year)
    ).all()
    total_cash = sum((Decimal(row.cash_amount) for row in rows), ZERO_AMOUNT)
    items = [
        EventDistributionItemResponse(
            event_type=QueryDictionaryResponse(
                id=row.event_type_id,
                name=row.event_type_name,
            ),
            cash_amount=row.cash_amount,
            cash_ratio_percent=_cash_ratio_percent(Decimal(row.cash_amount), total_cash),
            gift_count=row.gift_count,
            goods_count=row.goods_count,
        )
        for row in rows
    ]
    return EventDistributionResponse(year=year, items=items)


def relationship_circles(session: Session, user_id: int) -> RelationshipCircleResponse:
    context = statistics_context(session, user_id)
    rows = session.execute(
        relationship_circle_statement(context.user_id, context.self_contact_id)
    ).all()
    items = [
        RelationshipCircleItemResponse(
            relationship_type=QueryDictionaryResponse(
                id=row.relationship_type_id,
                name=row.relationship_type_name,
            ),
            expense_amount=row.expense_amount,
            income_amount=row.income_amount,
            gift_count=row.gift_count,
            goods_count=row.goods_count,
        )
        for row in rows
    ]
    return RelationshipCircleResponse(items=items)
