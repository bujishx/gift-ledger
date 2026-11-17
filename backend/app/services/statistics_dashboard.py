"""统计筛选复用记账查询; 金额按 Decimal 计算; 状态取全量历史。"""

import calendar
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import and_, or_, select

from app.db.models import Contact, RelationshipType
from app.schemas.ledger_queries import GiftsQueryFilters
from app.services.event_dates import (
    date_period_uncertain_condition,
    date_value,
    event_date_response_fields,
    known_date_bounds,
)
from app.services.ledger_queries import gifts_page_statement
from app.services.ledger_query_common import (
    INTERACTION_STATUS_LABELS,
    interaction_history_subquery,
    validate_query_context,
)


def empty_amounts():
    return dict(
        expense_amount=Decimal(0),
        income_amount=Decimal(0),
        expense_count=0,
        income_count=0,
        gift_count=0,
        goods_count=0,
        pure_goods_count=0,
    )


def add_amounts(target, row):
    direction = row["direction"]
    amount = Decimal(str(row["gift_amount"]))
    target[direction + "_amount"] += amount
    target[direction + "_count"] += 1
    target["gift_count"] += 1
    if row["gift_details"] and row["gift_details"].strip():
        target["goods_count"] += 1
        if amount == 0:
            target["pure_goods_count"] += 1


def bucket_key(value, grain):
    if grain == "year":
        return str(value.year)
    if grain == "quarter":
        return f"{value.year}-Q{(value.month - 1) // 3 + 1}"
    if grain == "month":
        return value.strftime("%Y-%m")
    return value.isoformat()


def bucket_range(key, grain):
    if grain == "year":
        return date(int(key), 1, 1), date(int(key), 12, 31)
    if grain == "day":
        return date.fromisoformat(key), date.fromisoformat(key)
    year = int(key[:4])
    month = (int(key[-1]) - 1) * 3 + 1 if grain == "quarter" else int(key[-2:])
    first = date(year, month, 1)
    end_month = month + (2 if grain == "quarter" else 0)
    last = date(year, end_month, calendar.monthrange(year, end_month)[1])
    return first, last


def matched_date_span(rows, filters):
    first = last = None
    for row in rows:
        if filters.date_basis == "registered":
            lo = hi = row["gift_created_at"].date()
        else:
            lo, hi = known_date_bounds(date_value(SimpleNamespace(**row)))
        if lo is not None:
            first = min(first or lo, lo)
            last = max(last or hi, hi)
    return filters.date_from or first, filters.date_to or last


def recommended_grain(rows, filters):
    first, last = matched_date_span(rows, filters)
    if first is None or last is None or first > last:
        return "month"
    months = (last.year - first.year) * 12 + last.month - first.month + 1
    return "month" if months <= 12 else "quarter" if months <= 36 else "year"


def aggregate_rows(rows, *, contacts, history, filters, grain):
    summary = empty_amounts()
    events = {}
    relations = {}
    trends = {}
    trend_unassigned = empty_amounts()
    people = {}
    status_counts = {str(code): 0 for code in INTERACTION_STATUS_LABELS}
    for person in contacts:
        person_id = person["id"]
        state = history.get(person_id, "none")
        if filters.contact_id and person_id != filters.contact_id:
            continue
        if (
            filters.relationship_type_id
            and person["relationship_type_id"] != filters.relationship_type_id
        ):
            continue
        if filters.interaction_status and state != filters.interaction_status:
            continue
        if filters.counterparty_relationship_type_ids and (
            person["relationship_type_id"] not in filters.counterparty_relationship_type_ids
        ):
            continue
        if filters.interaction_statuses and state not in filters.interaction_statuses:
            continue
        people[person_id] = {
            **person,
            **empty_amounts(),
            "interaction_status": state,
            "interaction_label": INTERACTION_STATUS_LABELS[state],
            "last_date": None,
            "incomplete_date_count": 0,
        }
        status_counts[state] += 1
    seen_people = set()
    seen_events = set()
    first = None
    last = None
    for row in rows:
        stamp = (
            row["gift_created_at"].date()
            if filters.date_basis == "registered"
            else row["event_date"]
        )
        value = date_value(SimpleNamespace(**row))
        if stamp is not None:
            first = min(first or stamp, stamp)
            last = max(last or stamp, stamp)
        else:
            lo, hi = known_date_bounds(value)
            if lo is not None:
                first = min(first or lo, lo)
                last = max(last or hi, hi)
        pid = row["counterparty_contact_id"]
        eid = row["event_id"]
        seen_people.add(pid)
        seen_events.add(eid)
        add_amounts(summary, row)
        if pid in people:
            add_amounts(people[pid], row)
            if stamp is not None:
                people[pid]["last_date"] = max(people[pid]["last_date"] or stamp, stamp)
            elif filters.date_basis == "event":
                people[pid]["incomplete_date_count"] += 1
        event = events.setdefault(
            eid,
            {
                **empty_amounts(),
                "id": eid,
                "host": row["host_contact_name"],
                "host_id": row["host_contact_id"],
                "host_relation": row["host_relation_type_name"],
                "host_remark": row["host_contact_remark"],
                "kinship": row["kinship_type_name"],
                "event_type": row["event_type_name"],
                "event_type_id": row["event_type_id"],
                **event_date_response_fields(SimpleNamespace(**row)),
                "remark": row["event_remark"],
            },
        )
        add_amounts(event, row)
        rid = row["counterparty_relation_type_id"]
        rel = relations.setdefault(
            rid,
            {
                **empty_amounts(),
                "id": rid,
                "name": row["counterparty_relation_type_name"],
                "people": set(),
            },
        )
        rel["people"].add(pid)
        add_amounts(rel, row)
        if stamp is not None:
            key = bucket_key(stamp, grain)
        elif value.year is not None and grain == "year":
            key = str(value.year)
        elif value.month is not None and grain in ("month", "quarter"):
            key = (
                f"{value.year:04d}-{value.month:02d}"
                if grain == "month"
                else f"{value.year}-Q{(value.month - 1) // 3 + 1}"
            )
        else:
            key = None
        if key is None:
            add_amounts(trend_unassigned, row)
        else:
            trend = trends.setdefault(key, {**empty_amounts(), "key": key})
            add_amounts(trend, row)
    # Include quiet periods so a chart never compresses an empty month out of the timeline.
    start = filters.date_from or first
    end = filters.date_to or last
    if start and end:
        cursor = start
        while cursor <= end:
            key = bucket_key(cursor, grain)
            trends.setdefault(key, {**empty_amounts(), "key": key})
            _, finish = bucket_range(key, grain)
            if finish == date.max:
                break
            cursor = finish + timedelta(days=1)
    for target in [
        summary,
        *events.values(),
        *relations.values(),
        *people.values(),
        *trends.values(),
    ]:
        target["net_amount"] = target["income_amount"] - target["expense_amount"]
    summary.update(people_count=len(seen_people), event_count=len(seen_events))
    for person in people.values():
        last_date = person["last_date"]
        person["last_date_display"] = last_date.isoformat() if last_date else "—"
        if person["incomplete_date_count"]:
            person["last_date_display"] = (
                f"{last_date.isoformat()}（含日期不完整旧账）" if last_date else "日期信息不足"
            )
    for rel in relations.values():
        rel["people_count"] = len(rel.pop("people"))
    trend_list = []
    for key in sorted(trends):
        lo, hi = bucket_range(key, grain)
        trend_list.append(
            {
                **trends[key],
                "date_from": max(lo, start) if start else lo,
                "date_to": min(hi, end) if end else hi,
            }
        )
    types = {}
    for event in events.values():
        item = types.setdefault(
            event["event_type_id"],
            {
                **empty_amounts(),
                "id": event["event_type_id"],
                "name": event["event_type"],
                "event_count": 0,
            },
        )
        for field in empty_amounts():
            item[field] += event[field]
        item["event_count"] += 1
    return dict(
        summary=summary,
        people=list(people.values()),
        events=list(events.values()),
        relationships=list(relations.values()),
        trends=trend_list,
        event_types=list(types.values()),
        status_counts=status_counts,
        grain=grain,
        trend_date_information_insufficient=trend_unassigned,
    )


def dashboard_statistics(session, user_id, filters):
    query = GiftsQueryFilters(
        **filters.model_dump(exclude={"comparison_from", "comparison_to", "grain"})
    )
    context = validate_query_context(session, user_id, query)
    history_table = interaction_history_subquery(user_id, context.self_contact_id)
    history = {
        row.counterparty_id: row.interaction_status
        for row in session.execute(select(history_table))
    }
    contacts = [
        dict(row)
        for row in session.execute(
            select(
                Contact.id,
                Contact.name,
                Contact.remark,
                Contact.relation_type_id.label("relationship_type_id"),
                RelationshipType.type_name.label("relationship_type_name"),
                Contact.is_active,
            )
            .join(
                RelationshipType,
                and_(
                    RelationshipType.id == Contact.relation_type_id,
                    or_(RelationshipType.user_id.is_(None), RelationshipType.user_id == user_id),
                ),
            )
            .where(Contact.user_id == user_id, Contact.id != context.self_contact_id)
        ).mappings()
    ]
    rows = list(session.execute(
        gifts_page_statement(context).limit(None).offset(None).order_by(None),
        execution_options={"stream_results": True},
    ).mappings())
    recommendation = recommended_grain(rows, filters)
    grain = recommendation if filters.grain == "auto" else filters.grain
    data = aggregate_rows(rows, contacts=contacts, history=history, filters=filters, grain=grain)
    data["recommended_grain"] = recommendation
    insufficient = empty_amounts()
    if query.date_basis == "event" and (query.date_from or query.date_to):
        unrestricted = replace(
            context, filters=query.model_copy(update={"date_from": None, "date_to": None})
        )
        statement = (
            gifts_page_statement(unrestricted)
            .limit(None)
            .offset(None)
            .order_by(None)
            .where(
                date_period_uncertain_condition(query.date_from, query.date_to),
            )
        )
        for row in session.execute(statement).mappings():
            add_amounts(insufficient, row)
    data["date_information_insufficient"] = insufficient
    data["comparison"] = None
    if filters.comparison_from:
        previous = query.model_copy(
            update={"date_from": filters.comparison_from, "date_to": filters.comparison_to}
        )
        previous_context = validate_query_context(session, user_id, previous)
        totals = empty_amounts()
        for row in session.execute(
            gifts_page_statement(previous_context).limit(None).offset(None).order_by(None)
        ).mappings():
            add_amounts(totals, row)
        totals["net_amount"] = totals["income_amount"] - totals["expense_amount"]
        data["comparison"] = totals
    return data
