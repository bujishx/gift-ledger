"""首页年度汇总、回礼提醒、最近流水和常用人员。"""

from datetime import date
from typing import Any

from sqlalchemy import and_, case, distinct, func, literal, or_, select
from sqlalchemy.orm import Session, aliased

from app.db.models import Contact, EventRecord, GiftRecord, RelationshipType
from app.schemas.home import (
    HomeAnnualSummaryResponse,
    HomeReminderResponse,
    HomeSummaryResponse,
)
from app.schemas.ledger_queries import (
    InteractionStatus,
    LedgerQueryFilters,
    PeopleQueryFilters,
    QueryContactResponse,
    QueryDictionaryResponse,
)
from app.services.event_dates import date_period_conditions
from app.services.ledger_queries import (
    gifts_item_response,
    gifts_page_statement,
    people_item_response,
    people_page_statement,
    people_totals_statement,
)
from app.services.ledger_query_common import (
    LedgerQueryContext,
    counterparty_id_expression,
    valid_participation_expression,
)
from app.services.profile import get_self_contact
from app.services.statistics import STATISTICS_MAX_YEAR, STATISTICS_MIN_YEAR
from app.services.system_dictionary_catalog import SELF_RELATIONSHIP_SYSTEM_CODE

ZERO_AMOUNT = 0

_FREQUENT_CONTACT = aliased(Contact, name="frequent_contact")
_FREQUENT_RELATION = aliased(RelationshipType, name="frequent_relation")


class HomeValidationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _context(session: Session, user_id: int, year: int) -> LedgerQueryContext:
    if not STATISTICS_MIN_YEAR <= year <= STATISTICS_MAX_YEAR:
        raise HomeValidationError(
            "HOME_YEAR_INVALID",
            f"首页年份必须在 {STATISTICS_MIN_YEAR} 到 {STATISTICS_MAX_YEAR} 之间",
        )
    self_contact = get_self_contact(session, user_id)
    if self_contact is None or not self_contact.is_active:
        raise HomeValidationError("SELF_CONTACT_REQUIRED", "请先完成本人信息建档")
    return LedgerQueryContext(
        user_id=user_id,
        self_contact_id=self_contact.id,
        filters=LedgerQueryFilters(),
    )


def _gift_event_join() -> Any:
    return and_(
        EventRecord.id == GiftRecord.event_record_id,
        EventRecord.user_id == GiftRecord.user_id,
    )


def annual_summary_statement(context: LedgerQueryContext, year: int):
    expense = and_(
        EventRecord.contact_id != context.self_contact_id,
        GiftRecord.contact_id == context.self_contact_id,
    )
    income = and_(
        EventRecord.contact_id == context.self_contact_id,
        GiftRecord.contact_id != context.self_contact_id,
    )
    return (
        select(
            func.coalesce(func.sum(case((expense, GiftRecord.amount), else_=0)), 0).label(
                "expense_amount"
            ),
            func.coalesce(func.sum(case((income, GiftRecord.amount), else_=0)), 0).label(
                "income_amount"
            ),
            func.count(case((expense, 1))).label("expense_gift_count"),
            func.count(distinct(case((expense, EventRecord.contact_id)))).label(
                "expense_people_count"
            ),
        )
        .select_from(GiftRecord)
        .join(EventRecord, _gift_event_join())
        .where(
            GiftRecord.user_id == context.user_id,
            EventRecord.user_id == context.user_id,
            *date_period_conditions(date(year, 1, 1), date(year, 12, 31)),
            valid_participation_expression(context.self_contact_id),
        )
    )


def _twelve_month_cutoff(today: date) -> date:
    try:
        return today.replace(year=today.year - 1)
    except ValueError:
        return today.replace(year=today.year - 1, day=28)


def frequent_contacts_statement(
    context: LedgerQueryContext,
    *,
    today: date,
):
    recent_expense = (
        select(
            EventRecord.contact_id.label("contact_id"),
            func.count(distinct(EventRecord.id)).label("recent_event_count"),
            func.max(EventRecord.event_date).label("last_recent_event_date"),
        )
        .select_from(GiftRecord)
        .join(EventRecord, _gift_event_join())
        .where(
            GiftRecord.user_id == context.user_id,
            EventRecord.user_id == context.user_id,
            GiftRecord.contact_id == context.self_contact_id,
            EventRecord.contact_id != context.self_contact_id,
            EventRecord.event_date >= _twelve_month_cutoff(today),
            EventRecord.event_date <= today,
        )
        .group_by(EventRecord.contact_id)
        .subquery("recent_expense_contacts")
    )
    counterparty_id = counterparty_id_expression(context.self_contact_id)
    interaction = (
        select(
            counterparty_id,
            func.max(EventRecord.event_date).label("last_interaction_date"),
        )
        .select_from(GiftRecord)
        .join(EventRecord, _gift_event_join())
        .where(
            GiftRecord.user_id == context.user_id,
            EventRecord.user_id == context.user_id,
            valid_participation_expression(context.self_contact_id),
        )
        .group_by(counterparty_id)
        .subquery("frequent_interaction")
    )
    recent_count = func.coalesce(recent_expense.c.recent_event_count, 0)
    tier = case(
        (recent_count > 0, literal(0)),
        (interaction.c.last_interaction_date.is_not(None), literal(1)),
        else_=literal(2),
    )
    return (
        select(
            _FREQUENT_CONTACT.id.label("contact_id"),
            _FREQUENT_CONTACT.name.label("contact_name"),
            _FREQUENT_CONTACT.location.label("contact_location"),
            _FREQUENT_CONTACT.remark.label("contact_remark"),
            _FREQUENT_RELATION.id.label("relation_type_id"),
            _FREQUENT_RELATION.type_name.label("relation_type_name"),
        )
        .select_from(_FREQUENT_CONTACT)
        .join(
            _FREQUENT_RELATION,
            and_(
                _FREQUENT_RELATION.id == _FREQUENT_CONTACT.relation_type_id,
                or_(
                    _FREQUENT_RELATION.user_id.is_(None),
                    _FREQUENT_RELATION.user_id == context.user_id,
                ),
            ),
        )
        .outerjoin(recent_expense, recent_expense.c.contact_id == _FREQUENT_CONTACT.id)
        .outerjoin(interaction, interaction.c.counterparty_id == _FREQUENT_CONTACT.id)
        .where(
            _FREQUENT_CONTACT.user_id == context.user_id,
            _FREQUENT_CONTACT.is_active == 1,
            or_(
                _FREQUENT_RELATION.system_code.is_(None),
                _FREQUENT_RELATION.system_code != SELF_RELATIONSHIP_SYSTEM_CODE,
            ),
        )
        .order_by(
            tier.asc(),
            case((tier == 0, recent_count)).desc(),
            case((tier == 0, recent_expense.c.last_recent_event_date)).desc(),
            case((tier == 1, interaction.c.last_interaction_date)).desc(),
            case((tier == 2, _FREQUENT_CONTACT.created_at)).desc(),
            _FREQUENT_CONTACT.id.asc(),
        )
        .limit(8)
    )


def _contact_response(row: Any) -> QueryContactResponse:
    return QueryContactResponse(
        id=row.contact_id,
        name=row.contact_name,
        location=row.contact_location,
        remark=row.contact_remark,
        relationship_type=QueryDictionaryResponse(
            id=row.relation_type_id,
            name=row.relation_type_name,
        ),
    )


def home_summary(
    session: Session,
    user_id: int,
    year: int,
    *,
    today: date | None = None,
) -> HomeSummaryResponse:
    context = _context(session, user_id, year)
    effective_today = today or date.today()

    annual_row = session.execute(annual_summary_statement(context, year)).one()

    reminder_context = LedgerQueryContext(
        user_id=user_id,
        self_contact_id=context.self_contact_id,
        filters=PeopleQueryFilters(
            interaction_status=InteractionStatus.SELF_UNRETURNED,
            limit=5,
            offset=0,
        ),
    )
    reminder_totals = session.execute(people_totals_statement(reminder_context)).one()
    reminder_rows = session.execute(people_page_statement(reminder_context)).all()

    recent_context = LedgerQueryContext(
        user_id=user_id,
        self_contact_id=context.self_contact_id,
        filters=LedgerQueryFilters(limit=6, offset=0),
    )
    recent_rows = session.execute(gifts_page_statement(recent_context)).all()
    frequent_rows = session.execute(
        frequent_contacts_statement(context, today=effective_today)
    ).all()

    expense_amount = annual_row.expense_amount
    income_amount = annual_row.income_amount
    return HomeSummaryResponse(
        annual=HomeAnnualSummaryResponse(
            year=year,
            expense_amount=expense_amount,
            income_amount=income_amount,
            net_amount=income_amount - expense_amount,
            expense_gift_count=int(annual_row.expense_gift_count),
            expense_people_count=int(annual_row.expense_people_count),
        ),
        reminders=HomeReminderResponse(
            self_unreturned_count=int(reminder_totals.people_total),
            items=[people_item_response(row) for row in reminder_rows],
        ),
        recent_gifts=[gifts_item_response(row) for row in recent_rows],
        frequent_contacts=[_contact_response(row) for row in frequent_rows],
    )
