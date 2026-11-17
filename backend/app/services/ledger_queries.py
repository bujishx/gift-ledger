from decimal import Decimal
from typing import Any

from sqlalchemy import Select, and_, case, distinct, func, or_, select
from sqlalchemy.orm import Session, aliased

from app.db.models import (
    Contact,
    EventRecord,
    EventType,
    GiftRecord,
    KinshipType,
    RelationshipType,
)
from app.schemas.ledger import LedgerDirection
from app.schemas.ledger_queries import (
    EventsQueryItemResponse,
    EventsQueryResponse,
    GiftsQueryFilters,
    GiftsQueryItemResponse,
    GiftsQueryResponse,
    GiftsSortField,
    InteractionStatus,
    LedgerQueryFilters,
    LedgerQueryTotalsResponse,
    PeopleQueryFilters,
    PeopleQueryItemResponse,
    PeopleQueryResponse,
    PeopleSortField,
    QueryBalanceResponse,
    QueryContactResponse,
    QueryDictionaryResponse,
    QueryEventSummaryResponse,
    QueryPageMetaResponse,
    SortOrder,
)
from app.services.event_dates import (
    date_period_conditions,
    date_sort_expression,
    event_date_response_fields,
)
from app.services.ledger_query_common import (
    LedgerQueryContext,
    counterparty_id_expression,
    direction_expression,
    interaction_history_subquery,
    interaction_status_response,
    valid_participation_expression,
    validate_query_context,
)

BALANCE_INCOME_LABEL = "我多收到"
BALANCE_EXPENSE_LABEL = "我多随出"
BALANCE_EVEN_LABEL = "礼金持平"

_HOST_CONTACT = aliased(Contact, name="host_contact")
_COUNTERPARTY_CONTACT = aliased(Contact, name="counterparty_contact")
_GIVER_CONTACT = aliased(Contact, name="giver_contact")
_HOST_RELATION = aliased(RelationshipType, name="host_relation")
_COUNTERPARTY_RELATION = aliased(RelationshipType, name="counterparty_relation")
_GIVER_RELATION = aliased(RelationshipType, name="giver_relation")


def balance_label(income_amount: Decimal, expense_amount: Decimal) -> str:
    if income_amount > expense_amount:
        return BALANCE_INCOME_LABEL
    if income_amount < expense_amount:
        return BALANCE_EXPENSE_LABEL
    return BALANCE_EVEN_LABEL


def build_balance_response(
    income_amount: Decimal,
    expense_amount: Decimal,
) -> QueryBalanceResponse:
    return QueryBalanceResponse(
        label=balance_label(income_amount, expense_amount),
        amount=income_amount - expense_amount,
    )


def _keyword_pattern(keyword: str) -> str:
    escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _keyword_condition(keyword: str) -> Any:
    pattern = _keyword_pattern(keyword)

    def contains(column: Any) -> Any:
        return column.like(pattern, escape="\\")

    return or_(
        contains(_HOST_CONTACT.name),
        contains(_HOST_CONTACT.location),
        contains(_HOST_CONTACT.remark),
        contains(_COUNTERPARTY_CONTACT.name),
        contains(_COUNTERPARTY_CONTACT.location),
        contains(_COUNTERPARTY_CONTACT.remark),
        contains(EventRecord.remark),
        contains(GiftRecord.remark),
        contains(GiftRecord.gift_details),
        contains(KinshipType.type_name),
        contains(EventType.type_name),
    )


def filter_conditions(filters: LedgerQueryFilters, self_contact_id: int) -> list[Any]:
    conditions: list[Any] = []
    if filters.direction is not None:
        if filters.direction == LedgerDirection.INCOME:
            conditions.append(EventRecord.contact_id == self_contact_id)
        else:
            conditions.append(EventRecord.contact_id != self_contact_id)
    if filters.contact_id is not None:
        if filters.contact_id == self_contact_id:
            conditions.append(EventRecord.contact_id == self_contact_id)
        else:
            conditions.append(
                or_(
                    EventRecord.contact_id == filters.contact_id,
                    GiftRecord.contact_id == filters.contact_id,
                )
            )
    if filters.relationship_type_id is not None:
        conditions.append(_COUNTERPARTY_CONTACT.relation_type_id == filters.relationship_type_id)
    if filters.kinship_type_id is not None:
        conditions.append(EventRecord.relative_type_id == filters.kinship_type_id)
    if filters.event_type_id is not None:
        conditions.append(EventRecord.event_type_id == filters.event_type_id)
    if isinstance(filters, GiftsQueryFilters):
        if filters.relationship_type_ids:
            conditions.append(_HOST_CONTACT.relation_type_id.in_(filters.relationship_type_ids))
        if filters.kinship_type_ids:
            conditions.append(EventRecord.relative_type_id.in_(filters.kinship_type_ids))
        if filters.event_type_ids:
            conditions.append(EventRecord.event_type_id.in_(filters.event_type_ids))
        if filters.statistics_event_type_ids:
            conditions.append(EventRecord.event_type_id.in_(filters.statistics_event_type_ids))
        if filters.counterparty_relationship_type_ids:
            conditions.append(
                _COUNTERPARTY_CONTACT.relation_type_id.in_(
                    filters.counterparty_relationship_type_ids
                )
            )
        if filters.event_id is not None:
            conditions.append(EventRecord.id == filters.event_id)
    if getattr(filters, "date_basis", "event") == "registered":
        if filters.date_from is not None:
            conditions.append(func.date(GiftRecord.created_at) >= filters.date_from)
        if filters.date_to is not None:
            conditions.append(func.date(GiftRecord.created_at) <= filters.date_to)
    else:
        conditions.extend(date_period_conditions(filters.date_from, filters.date_to))
    if filters.keyword is not None:
        if isinstance(filters, GiftsQueryFilters):
            keyword_conditions = []
            for term in filters.keyword.split():
                pattern = _keyword_pattern(term)
                giver_ids = select(Contact.id).where(
                    Contact.user_id == _HOST_CONTACT.user_id,
                    or_(
                        Contact.name.like(pattern, escape="\\"),
                        Contact.remark.like(pattern, escape="\\"),
                    ),
                )
                keyword_conditions.extend(
                    (
                        _HOST_CONTACT.name.like(pattern, escape="\\"),
                        _HOST_CONTACT.remark.like(pattern, escape="\\"),
                        EventRecord.remark.like(pattern, escape="\\"),
                        GiftRecord.contact_id.in_(giver_ids),
                    )
                )
            if keyword_conditions:
                conditions.append(or_(*keyword_conditions))
        else:
            conditions.append(_keyword_condition(filters.keyword))
    return conditions


def filtered_gift_select(context: LedgerQueryContext, *columns: Any) -> Select:
    user_id = context.user_id
    self_contact_id = context.self_contact_id
    return (
        select(*columns)
        .select_from(GiftRecord)
        .join(
            EventRecord,
            and_(
                EventRecord.id == GiftRecord.event_record_id,
                EventRecord.user_id == GiftRecord.user_id,
            ),
        )
        .join(
            _HOST_CONTACT,
            and_(
                _HOST_CONTACT.id == EventRecord.contact_id,
                _HOST_CONTACT.user_id == user_id,
            ),
        )
        .join(
            _COUNTERPARTY_CONTACT,
            and_(
                _COUNTERPARTY_CONTACT.id == counterparty_id_expression(self_contact_id),
                _COUNTERPARTY_CONTACT.user_id == user_id,
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
        .where(
            GiftRecord.user_id == user_id,
            EventRecord.user_id == user_id,
            valid_participation_expression(self_contact_id),
            *filter_conditions(context.filters, self_contact_id),
        )
    )


def _relation_join_condition(relation: Any, contact: Any, user_id: int) -> Any:
    return and_(
        relation.id == contact.relation_type_id,
        or_(relation.user_id.is_(None), relation.user_id == user_id),
    )


def _joined_history(statement: Select, context: LedgerQueryContext) -> tuple[Any, Select]:
    history = interaction_history_subquery(context.user_id, context.self_contact_id)
    joined = statement.join(history, history.c.counterparty_id == _COUNTERPARTY_CONTACT.id)
    return history, joined


def _with_status_filter(statement: Select, context: LedgerQueryContext) -> Select:
    history, joined = _joined_history(statement, context)
    if context.filters.interaction_status is not None:
        joined = joined.where(
            history.c.interaction_status == context.filters.interaction_status.value
        )
    statuses = getattr(context.filters, "interaction_statuses", None)
    if statuses:
        joined = joined.where(history.c.interaction_status.in_([item.value for item in statuses]))
    return joined


def _has_status_filter(filters: LedgerQueryFilters) -> bool:
    return filters.interaction_status is not None or bool(
        getattr(filters, "interaction_statuses", None)
    )


def _expense_case(self_contact_id: int) -> Any:
    return case((EventRecord.contact_id != self_contact_id, GiftRecord.amount), else_=0)


def _income_case(self_contact_id: int) -> Any:
    return case((EventRecord.contact_id == self_contact_id, GiftRecord.amount), else_=0)


def _expense_amount_expression(self_contact_id: int) -> Any:
    return func.coalesce(func.sum(_expense_case(self_contact_id)), 0)


def _income_amount_expression(self_contact_id: int) -> Any:
    return func.coalesce(func.sum(_income_case(self_contact_id)), 0)


def _expense_count_expression(self_contact_id: int) -> Any:
    return func.count(case((EventRecord.contact_id != self_contact_id, 1)))


def _income_count_expression(self_contact_id: int) -> Any:
    return func.count(case((EventRecord.contact_id == self_contact_id, 1)))


def _goods_count_expression() -> Any:
    return func.count(
        case(
            (
                and_(
                    GiftRecord.gift_details.is_not(None),
                    func.trim(GiftRecord.gift_details) != "",
                ),
                1,
            )
        )
    )


def people_totals_statement(context: LedgerQueryContext) -> Select:
    self_contact_id = context.self_contact_id
    statement = filtered_gift_select(
        context,
        _expense_amount_expression(self_contact_id).label("expense_amount"),
        _income_amount_expression(self_contact_id).label("income_amount"),
        func.count().label("gift_count"),
        _goods_count_expression().label("goods_count"),
        func.count(distinct(_COUNTERPARTY_CONTACT.id)).label("people_total"),
    )
    if _has_status_filter(context.filters):
        statement = _with_status_filter(statement, context)
    return statement


def _people_order_by(
    filters: PeopleQueryFilters,
    expense_amount: Any,
    income_amount: Any,
    last_event_date: Any,
    history: Any,
) -> list[Any]:
    if filters.sort_by is None:
        self_unreturned_priority = case(
            (
                history.c.interaction_status == InteractionStatus.SELF_UNRETURNED.value,
                0,
            ),
            else_=1,
        )
        return [
            self_unreturned_priority.asc(),
            expense_amount.desc(),
            last_event_date.desc(),
            _COUNTERPARTY_CONTACT.id.asc(),
        ]
    ascending = filters.sort_order == SortOrder.ASC
    primary_keys = {
        PeopleSortField.CONTACT_NAME: _COUNTERPARTY_CONTACT.name,
        PeopleSortField.EXPENSE_AMOUNT: expense_amount,
        PeopleSortField.INCOME_AMOUNT: income_amount,
        PeopleSortField.BALANCE: income_amount - expense_amount,
        PeopleSortField.LAST_EVENT_DATE: last_event_date,
        PeopleSortField.INTERACTION_STATUS: history.c.interaction_status_rank,
    }
    primary = primary_keys[filters.sort_by]
    return [
        primary.asc() if ascending else primary.desc(),
        _COUNTERPARTY_CONTACT.id.asc(),
    ]


def people_page_statement(context: LedgerQueryContext) -> Select:
    filters = context.filters
    self_contact_id = context.self_contact_id
    expense_amount = _expense_amount_expression(self_contact_id)
    income_amount = _income_amount_expression(self_contact_id)
    last_event_date = func.max(EventRecord.event_date)
    statement = filtered_gift_select(
        context,
        _COUNTERPARTY_CONTACT.id.label("contact_id"),
        _COUNTERPARTY_CONTACT.name.label("contact_name"),
        _COUNTERPARTY_CONTACT.location.label("contact_location"),
        _COUNTERPARTY_CONTACT.remark.label("contact_remark"),
        _COUNTERPARTY_RELATION.id.label("relation_type_id"),
        _COUNTERPARTY_RELATION.type_name.label("relation_type_name"),
        expense_amount.label("expense_amount"),
        income_amount.label("income_amount"),
        _expense_count_expression(self_contact_id).label("expense_count"),
        _income_count_expression(self_contact_id).label("income_count"),
        _goods_count_expression().label("goods_count"),
        last_event_date.label("last_event_date"),
        func.count(case((EventRecord.date_precision != "day", 1))).label("incomplete_date_count"),
    )
    statement = statement.join(
        _COUNTERPARTY_RELATION,
        _relation_join_condition(_COUNTERPARTY_RELATION, _COUNTERPARTY_CONTACT, context.user_id),
    )
    history, statement = _joined_history(statement, context)
    statement = statement.add_columns(
        history.c.interaction_status.label("interaction_status"),
        history.c.interaction_status_rank.label("status_rank"),
    )
    if context.filters.interaction_status is not None:
        statement = statement.where(
            history.c.interaction_status == context.filters.interaction_status.value
        )
    statement = statement.group_by(
        _COUNTERPARTY_CONTACT.id,
        _COUNTERPARTY_CONTACT.name,
        _COUNTERPARTY_CONTACT.location,
        _COUNTERPARTY_CONTACT.remark,
        _COUNTERPARTY_RELATION.id,
        _COUNTERPARTY_RELATION.type_name,
        history.c.interaction_status,
        history.c.interaction_status_rank,
    )
    statement = statement.order_by(
        *_people_order_by(filters, expense_amount, income_amount, last_event_date, history)
    )
    return statement.limit(filters.limit).offset(filters.offset)


def events_totals_statement(context: LedgerQueryContext) -> Select:
    self_contact_id = context.self_contact_id
    statement = filtered_gift_select(
        context,
        _expense_amount_expression(self_contact_id).label("expense_amount"),
        _income_amount_expression(self_contact_id).label("income_amount"),
        func.count().label("gift_count"),
        _goods_count_expression().label("goods_count"),
        func.count(distinct(EventRecord.id)).label("events_total"),
    )
    if _has_status_filter(context.filters):
        statement = _with_status_filter(statement, context)
    return statement


def events_page_statement(context: LedgerQueryContext) -> Select:
    self_contact_id = context.self_contact_id
    direction = direction_expression(self_contact_id)
    statement = filtered_gift_select(
        context,
        EventRecord.id.label("event_id"),
        EventRecord.event_date.label("event_date"),
        EventRecord.date_precision.label("date_precision"),
        EventRecord.event_year.label("event_year"),
        EventRecord.event_month.label("event_month"),
        EventRecord.date_source.label("date_source"),
        EventRecord.remark.label("event_remark"),
        KinshipType.id.label("kinship_type_id"),
        KinshipType.type_name.label("kinship_type_name"),
        EventType.id.label("event_type_id"),
        EventType.type_name.label("event_type_name"),
        _HOST_CONTACT.id.label("host_contact_id"),
        _HOST_CONTACT.name.label("host_contact_name"),
        _HOST_CONTACT.location.label("host_contact_location"),
        _HOST_CONTACT.remark.label("host_contact_remark"),
        _HOST_RELATION.id.label("host_relation_type_id"),
        _HOST_RELATION.type_name.label("host_relation_type_name"),
        direction,
        func.count().label("gift_count"),
        func.coalesce(func.sum(GiftRecord.amount), 0).label("cash_amount"),
        _goods_count_expression().label("goods_count"),
    )
    statement = statement.join(
        _HOST_RELATION,
        _relation_join_condition(_HOST_RELATION, _HOST_CONTACT, context.user_id),
    )
    if _has_status_filter(context.filters):
        statement = _with_status_filter(statement, context)
    statement = statement.group_by(
        EventRecord.id,
        EventRecord.event_date,
        EventRecord.date_precision,
        EventRecord.event_year,
        EventRecord.event_month,
        EventRecord.date_source,
        EventRecord.remark,
        KinshipType.id,
        KinshipType.type_name,
        EventType.id,
        EventType.type_name,
        _HOST_CONTACT.id,
        _HOST_CONTACT.name,
        _HOST_CONTACT.location,
        _HOST_CONTACT.remark,
        _HOST_RELATION.id,
        _HOST_RELATION.type_name,
        direction,
    )
    statement = statement.order_by(
        (EventRecord.date_precision == "unknown").asc(),
        date_sort_expression().desc(),
        EventRecord.id.desc(),
    )
    return statement.limit(context.filters.limit).offset(context.filters.offset)


def event_related_contacts_statement(
    context: LedgerQueryContext,
    event_ids: list[int],
) -> Select:
    statement = filtered_gift_select(
        context,
        EventRecord.id.label("event_id"),
        _COUNTERPARTY_CONTACT.id.label("contact_id"),
        _COUNTERPARTY_CONTACT.name.label("contact_name"),
        _COUNTERPARTY_CONTACT.location.label("contact_location"),
        _COUNTERPARTY_CONTACT.remark.label("contact_remark"),
        _COUNTERPARTY_RELATION.id.label("relation_type_id"),
        _COUNTERPARTY_RELATION.type_name.label("relation_type_name"),
    ).join(
        _COUNTERPARTY_RELATION,
        _relation_join_condition(
            _COUNTERPARTY_RELATION,
            _COUNTERPARTY_CONTACT,
            context.user_id,
        ),
    )
    if _has_status_filter(context.filters):
        statement = _with_status_filter(statement, context)
    return (
        statement.where(EventRecord.id.in_(event_ids))
        .distinct()
        .order_by(
            EventRecord.id.asc(),
            _COUNTERPARTY_CONTACT.name.asc(),
            _COUNTERPARTY_CONTACT.id.asc(),
        )
    )


def gifts_totals_statement(context: LedgerQueryContext) -> Select:
    self_contact_id = context.self_contact_id
    statement = filtered_gift_select(
        context,
        _expense_amount_expression(self_contact_id).label("expense_amount"),
        _income_amount_expression(self_contact_id).label("income_amount"),
        func.count().label("gift_count"),
        _goods_count_expression().label("goods_count"),
    )
    if _has_status_filter(context.filters):
        statement = _with_status_filter(statement, context)
    return statement


def gifts_page_statement(context: LedgerQueryContext) -> Select:
    self_contact_id = context.self_contact_id
    statement = filtered_gift_select(
        context,
        GiftRecord.id.label("gift_id"),
        GiftRecord.amount.label("gift_amount"),
        GiftRecord.gift_details.label("gift_details"),
        GiftRecord.remark.label("gift_remark"),
        GiftRecord.created_at.label("gift_created_at"),
        direction_expression(self_contact_id),
        EventRecord.id.label("event_id"),
        EventRecord.event_date.label("event_date"),
        EventRecord.date_precision.label("date_precision"),
        EventRecord.event_year.label("event_year"),
        EventRecord.event_month.label("event_month"),
        EventRecord.date_source.label("date_source"),
        EventRecord.remark.label("event_remark"),
        KinshipType.id.label("kinship_type_id"),
        KinshipType.type_name.label("kinship_type_name"),
        EventType.id.label("event_type_id"),
        EventType.type_name.label("event_type_name"),
        _HOST_CONTACT.id.label("host_contact_id"),
        _HOST_CONTACT.name.label("host_contact_name"),
        _HOST_CONTACT.location.label("host_contact_location"),
        _HOST_CONTACT.remark.label("host_contact_remark"),
        _HOST_RELATION.id.label("host_relation_type_id"),
        _HOST_RELATION.type_name.label("host_relation_type_name"),
        _GIVER_CONTACT.id.label("giver_contact_id"),
        _GIVER_CONTACT.name.label("giver_contact_name"),
        _GIVER_CONTACT.location.label("giver_contact_location"),
        _GIVER_CONTACT.remark.label("giver_contact_remark"),
        _GIVER_RELATION.id.label("giver_relation_type_id"),
        _GIVER_RELATION.type_name.label("giver_relation_type_name"),
        _COUNTERPARTY_CONTACT.id.label("counterparty_contact_id"),
        _COUNTERPARTY_CONTACT.name.label("counterparty_contact_name"),
        _COUNTERPARTY_CONTACT.location.label("counterparty_contact_location"),
        _COUNTERPARTY_CONTACT.remark.label("counterparty_contact_remark"),
        _COUNTERPARTY_RELATION.id.label("counterparty_relation_type_id"),
        _COUNTERPARTY_RELATION.type_name.label("counterparty_relation_type_name"),
    )
    statement = statement.join(
        _GIVER_CONTACT,
        and_(_GIVER_CONTACT.id == GiftRecord.contact_id, _GIVER_CONTACT.user_id == context.user_id),
    )
    statement = statement.join(
        _HOST_RELATION,
        _relation_join_condition(_HOST_RELATION, _HOST_CONTACT, context.user_id),
    )
    statement = statement.join(
        _GIVER_RELATION,
        _relation_join_condition(_GIVER_RELATION, _GIVER_CONTACT, context.user_id),
    )
    statement = statement.join(
        _COUNTERPARTY_RELATION,
        _relation_join_condition(_COUNTERPARTY_RELATION, _COUNTERPARTY_CONTACT, context.user_id),
    )
    if _has_status_filter(context.filters):
        statement = _with_status_filter(statement, context)
    sort_by = (
        context.filters.sort_by
        if isinstance(context.filters, GiftsQueryFilters)
        else GiftsSortField.EVENT_DATE
    )
    sort_order = (
        context.filters.sort_order
        if isinstance(context.filters, GiftsQueryFilters)
        else SortOrder.DESC
    )
    sort_column = {
        GiftsSortField.GIFT_CREATED_AT: GiftRecord.created_at,
        GiftsSortField.EVENT_DATE: date_sort_expression(),
        GiftsSortField.HOST_NAME: _HOST_CONTACT.name,
        GiftsSortField.GIVER_NAME: _GIVER_CONTACT.name,
        GiftsSortField.AMOUNT: GiftRecord.amount,
    }[sort_by]
    primary_order = sort_column.asc() if sort_order == SortOrder.ASC else sort_column.desc()
    secondary_orders = (
        [GiftRecord.created_at.desc()] if sort_by == GiftsSortField.EVENT_DATE else []
    )
    known_date_order = (
        [(EventRecord.date_precision == "unknown").asc()]
        if sort_by == GiftsSortField.EVENT_DATE
        else []
    )
    statement = statement.order_by(
        *known_date_order, primary_order, *secondary_orders, GiftRecord.id.desc()
    )
    return statement.limit(context.filters.limit).offset(context.filters.offset)


def _contact_response(
    contact_id: int,
    name: str,
    location: str | None,
    remark: str | None,
    relation_type_id: int,
    relation_type_name: str,
) -> QueryContactResponse:
    return QueryContactResponse(
        id=contact_id,
        name=name,
        location=location,
        remark=remark,
        relationship_type=QueryDictionaryResponse(id=relation_type_id, name=relation_type_name),
    )


def _event_summary_response(row: Any) -> QueryEventSummaryResponse:
    return QueryEventSummaryResponse(
        id=row.event_id,
        **event_date_response_fields(row),
        remark=row.event_remark,
        kinship_type=QueryDictionaryResponse(
            id=row.kinship_type_id,
            name=row.kinship_type_name,
        ),
        event_type=QueryDictionaryResponse(
            id=row.event_type_id,
            name=row.event_type_name,
        ),
        host=_contact_response(
            row.host_contact_id,
            row.host_contact_name,
            row.host_contact_location,
            row.host_contact_remark,
            row.host_relation_type_id,
            row.host_relation_type_name,
        ),
    )


def totals_response(row: Any) -> LedgerQueryTotalsResponse:
    return LedgerQueryTotalsResponse(
        expense_amount=row.expense_amount,
        income_amount=row.income_amount,
        gift_count=int(row.gift_count),
        goods_count=int(row.goods_count),
    )


def people_item_response(row: Any) -> PeopleQueryItemResponse:
    return PeopleQueryItemResponse(
        contact=_contact_response(
            row.contact_id,
            row.contact_name,
            row.contact_location,
            row.contact_remark,
            row.relation_type_id,
            row.relation_type_name,
        ),
        expense_amount=row.expense_amount,
        expense_count=int(row.expense_count),
        income_amount=row.income_amount,
        income_count=int(row.income_count),
        goods_count=int(row.goods_count),
        balance=build_balance_response(row.income_amount, row.expense_amount),
        last_event_date=row.last_event_date,
        incomplete_date_count=int(getattr(row, "incomplete_date_count", 0)),
        last_event_date_display=(
            (row.last_event_date.isoformat() + "（含日期不完整旧账）")
            if row.last_event_date
            else "日期信息不足"
        )
        if getattr(row, "incomplete_date_count", 0)
        else (row.last_event_date.isoformat() if row.last_event_date else None),
        interaction_status=interaction_status_response(InteractionStatus(row.interaction_status)),
    )


def events_item_response(
    row: Any,
    related_contacts: list[QueryContactResponse],
) -> EventsQueryItemResponse:
    return EventsQueryItemResponse(
        event=_event_summary_response(row),
        related_contacts=related_contacts,
        direction=LedgerDirection(row.direction),
        gift_count=int(row.gift_count),
        cash_amount=row.cash_amount,
        goods_count=int(row.goods_count),
    )


def gifts_item_response(row: Any) -> GiftsQueryItemResponse:
    return GiftsQueryItemResponse(
        id=row.gift_id,
        event=_event_summary_response(row),
        giver=_contact_response(
            row.giver_contact_id,
            row.giver_contact_name,
            row.giver_contact_location,
            row.giver_contact_remark,
            row.giver_relation_type_id,
            row.giver_relation_type_name,
        ),
        counterparty=_contact_response(
            row.counterparty_contact_id,
            row.counterparty_contact_name,
            row.counterparty_contact_location,
            row.counterparty_contact_remark,
            row.counterparty_relation_type_id,
            row.counterparty_relation_type_name,
        ),
        direction=LedgerDirection(row.direction),
        amount=row.gift_amount,
        gift_details=row.gift_details,
        remark=row.gift_remark,
        created_at=row.gift_created_at,
    )


def query_people(
    session: Session, user_id: int, filters: PeopleQueryFilters
) -> PeopleQueryResponse:
    context = validate_query_context(session, user_id, filters)
    totals_row = session.execute(people_totals_statement(context)).one()
    item_rows = session.execute(people_page_statement(context)).all()
    people_total = int(totals_row.people_total)
    return PeopleQueryResponse(
        items=[people_item_response(row) for row in item_rows],
        totals=totals_response(totals_row),
        people_total=people_total,
        page=QueryPageMetaResponse(total=people_total, limit=filters.limit, offset=filters.offset),
    )


def query_events(
    session: Session, user_id: int, filters: LedgerQueryFilters
) -> EventsQueryResponse:
    context = validate_query_context(session, user_id, filters)
    totals_row = session.execute(events_totals_statement(context)).one()
    item_rows = session.execute(events_page_statement(context)).all()
    event_ids = [int(row.event_id) for row in item_rows]
    related_by_event: dict[int, list[QueryContactResponse]] = {
        event_id: [] for event_id in event_ids
    }
    if event_ids:
        contact_rows = session.execute(event_related_contacts_statement(context, event_ids)).all()
        for contact_row in contact_rows:
            related_by_event[int(contact_row.event_id)].append(
                _contact_response(
                    contact_row.contact_id,
                    contact_row.contact_name,
                    contact_row.contact_location,
                    contact_row.contact_remark,
                    contact_row.relation_type_id,
                    contact_row.relation_type_name,
                )
            )
    events_total = int(totals_row.events_total)
    return EventsQueryResponse(
        items=[events_item_response(row, related_by_event[int(row.event_id)]) for row in item_rows],
        totals=totals_response(totals_row),
        events_total=events_total,
        gift_total=int(totals_row.gift_count),
        page=QueryPageMetaResponse(total=events_total, limit=filters.limit, offset=filters.offset),
    )


def query_gifts(session: Session, user_id: int, filters: LedgerQueryFilters) -> GiftsQueryResponse:
    context = validate_query_context(session, user_id, filters)
    totals_row = session.execute(gifts_totals_statement(context)).one()
    item_rows = session.execute(gifts_page_statement(context)).all()
    return GiftsQueryResponse(
        items=[gifts_item_response(row) for row in item_rows],
        totals=totals_response(totals_row),
        page=QueryPageMetaResponse(
            total=int(totals_row.gift_count),
            limit=filters.limit,
            offset=filters.offset,
        ),
    )
