from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Select, and_, or_, select
from sqlalchemy.orm import Session

from app.db.models import Contact, EventRecord, EventType, KinshipType, RelationshipType
from app.schemas.exports import QueryExportFilters, QueryExportScope, QueryExportView
from app.schemas.ledger import LedgerDirection
from app.schemas.ledger_queries import (
    EventsQueryResponse,
    GiftsQueryFilters,
    GiftsQueryResponse,
    InteractionStatus,
    LedgerQueryFilters,
    PeopleQueryFilters,
    PeopleQueryResponse,
    PeopleSortField,
    SortOrder,
)
from app.services.event_dates import date_value, display_event_date
from app.services.ledger_queries import query_events, query_gifts, query_people

EXPORT_PAGE_SIZE = 100


@dataclass(frozen=True, slots=True)
class ExportTable:
    name: str
    columns: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]


@dataclass(frozen=True, slots=True)
class ExportPackage:
    filename: str
    title: str
    metadata: tuple[tuple[str, str], ...]
    tables: tuple[ExportTable, ...]


_VIEW_LABELS = {
    QueryExportView.PEOPLE: "按人汇总",
    QueryExportView.EVENTS: "按事件",
    QueryExportView.GIFTS: "流水明细",
}
_SCOPE_LABELS = {
    QueryExportScope.PAGE: "本页",
    QueryExportScope.ALL: "全部匹配结果",
}
_DIRECTION_LABELS = {
    LedgerDirection.EXPENSE: "我随出",
    LedgerDirection.INCOME: "我收到",
}
_INTERACTION_STATUS_LABELS = {
    InteractionStatus.SELF_UNRETURNED: "我未回礼",
    InteractionStatus.COUNTERPARTY_UNRETURNED: "对方未回礼",
    InteractionStatus.MUTUAL: "礼尚往来",
}
_PEOPLE_SORT_LABELS = {
    PeopleSortField.CONTACT_NAME: "联系人姓名",
    PeopleSortField.EXPENSE_AMOUNT: "我随出金额",
    PeopleSortField.INCOME_AMOUNT: "我收到金额",
    PeopleSortField.BALANCE: "礼金差额",
    PeopleSortField.LAST_EVENT_DATE: "最近往来日期",
    PeopleSortField.INTERACTION_STATUS: "来往状态",
}
_SORT_ORDER_LABELS = {SortOrder.ASC: "正序", SortOrder.DESC: "倒序"}


def _plain_filters(filters: QueryExportFilters) -> dict[str, Any]:
    values = filters.model_dump(
        exclude={"view", "scope", "sort_by", "sort_order", "limit", "offset"}
    )
    if filters.view != QueryExportView.GIFTS:
        values = {
            key: value for key, value in values.items() if key in LedgerQueryFilters.model_fields
        }
    return values


def _ledger_filters(filters: QueryExportFilters, *, limit: int, offset: int) -> LedgerQueryFilters:
    model = GiftsQueryFilters if filters.view == QueryExportView.GIFTS else LedgerQueryFilters
    return model(**_plain_filters(filters), limit=limit, offset=offset)


def _people_filters(filters: QueryExportFilters, *, limit: int, offset: int) -> PeopleQueryFilters:
    return PeopleQueryFilters(
        **_plain_filters(filters),
        sort_by=filters.sort_by,
        sort_order=filters.sort_order,
        limit=limit,
        offset=offset,
    )


def _collect_query_pages[
    QueryResponseT: (PeopleQueryResponse, EventsQueryResponse, GiftsQueryResponse)
](
    fetch: Callable[[int, int], QueryResponseT],
    filters: QueryExportFilters,
) -> tuple[list[Any], QueryResponseT]:
    if filters.scope == QueryExportScope.PAGE:
        response = fetch(filters.limit, filters.offset)
        return list(response.items), response

    offset = 0
    items: list[Any] = []
    first_response: QueryResponseT | None = None
    while True:
        response = fetch(EXPORT_PAGE_SIZE, offset)
        if first_response is None:
            first_response = response
        page_items = list(response.items)
        items.extend(page_items)
        offset += len(page_items)
        if not page_items or offset >= response.page.total:
            return items, first_response


def _filter_metadata(filters: QueryExportFilters) -> list[tuple[str, str]]:
    metadata = [
        ("查询视图", _VIEW_LABELS[filters.view]),
        ("导出范围", _SCOPE_LABELS[filters.scope]),
    ]
    labels = {
        "direction": "收支方向",
        "contact_id": "联系人编号",
        "relationship_type_id": "关系类型编号",
        "kinship_type_id": "亲属称谓编号",
        "event_type_id": "事件类型编号",
        "date_from": "开始日期",
        "date_to": "结束日期",
        "keyword": "关键词",
        "interaction_status": "来往状态",
        "date_basis": "日期口径",
        "relationship_type_ids": "事主关系类型编号",
        "kinship_type_ids": "亲属称谓编号",
        "event_type_ids": "事件类型编号",
        "counterparty_relationship_type_ids": "往来人与我的关系编号",
        "statistics_event_type_ids": "来自统计的事件类型编号",
        "interaction_statuses": "历史来往状态",
        "event_id": "具体事件编号",
    }
    for key, value in _plain_filters(filters).items():
        if value is not None:
            if key == "direction":
                display_value = _DIRECTION_LABELS[value]
            elif key == "interaction_status":
                display_value = _INTERACTION_STATUS_LABELS[value]
            elif key == "interaction_statuses":
                display_value = "、".join(
                    _INTERACTION_STATUS_LABELS.get(item, "暂无往来") for item in value
                )
            elif key == "date_basis":
                display_value = "登记日期" if value == "registered" else "办事日期"
            elif isinstance(value, list):
                display_value = "、".join(str(item) for item in value)
            else:
                display_value = str(value)
            metadata.append((labels[key], display_value))
    if filters.view == QueryExportView.PEOPLE:
        metadata.extend(
            [
                (
                    "排序项目",
                    _PEOPLE_SORT_LABELS[filters.sort_by] if filters.sort_by else "默认排序",
                ),
                ("排序方向", _SORT_ORDER_LABELS[filters.sort_order]),
            ]
        )
    if filters.scope == QueryExportScope.PAGE:
        metadata.extend([("每页数量", str(filters.limit)), ("起始位置", str(filters.offset))])
    return metadata


def _people_table(items: list[Any]) -> ExportTable:
    return ExportTable(
        name="按人汇总",
        columns=(
            "联系人姓名",
            "常住地",
            "联系人备注",
            "关系类型",
            "我随出金额",
            "我随出笔数",
            "我收到金额",
            "我收到笔数",
            "实物份数",
            "礼金差额",
            "差额说明",
            "最近往来日期",
            "来往状态",
        ),
        rows=tuple(
            (
                item.contact.name,
                item.contact.location,
                item.contact.remark,
                item.contact.relationship_type.name,
                item.expense_amount,
                item.expense_count,
                item.income_amount,
                item.income_count,
                item.goods_count,
                item.balance.amount,
                item.balance.label,
                item.last_event_date_display
                if getattr(item, "incomplete_date_count", 0)
                else item.last_event_date,
                item.interaction_status.label,
            )
            for item in items
        ),
    )


def _events_table(items: list[Any]) -> ExportTable:
    return ExportTable(
        name="按事件",
        columns=(
            "办事日期",
            "收支方向",
            "事主姓名",
            "事主常住地",
            "事主备注",
            "事主关系类型",
            "亲属称谓",
            "事件类型",
            "事件备注",
            "相关联系人",
            "流水笔数",
            "现金合计",
            "实物份数",
        ),
        rows=tuple(
            (
                item.event.event_date
                if date_value(item.event).precision == "day"
                else display_event_date(item.event),
                _DIRECTION_LABELS[item.direction],
                item.event.host.name,
                item.event.host.location,
                item.event.host.remark,
                item.event.host.relationship_type.name,
                item.event.kinship_type.name,
                item.event.event_type.name,
                item.event.remark,
                "、".join(contact.name for contact in item.related_contacts),
                item.gift_count,
                item.cash_amount,
                item.goods_count,
            )
            for item in items
        ),
    )


def _gifts_table(items: list[Any], *, name: str = "流水明细") -> ExportTable:
    return ExportTable(
        name=name,
        columns=(
            "办事日期",
            "收支方向",
            "事主姓名",
            "事主常住地",
            "事主备注",
            "事主关系类型",
            "亲属称谓",
            "事件类型",
            "事件备注",
            "随礼人姓名",
            "随礼人常住地",
            "随礼人备注",
            "随礼人关系类型",
            "往来联系人",
            "礼金金额",
            "实物明细",
            "流水备注",
            "登记时间",
        ),
        rows=tuple(
            (
                item.event.event_date
                if date_value(item.event).precision == "day"
                else display_event_date(item.event),
                _DIRECTION_LABELS[item.direction],
                item.event.host.name,
                item.event.host.location,
                item.event.host.remark,
                item.event.host.relationship_type.name,
                item.event.kinship_type.name,
                item.event.event_type.name,
                item.event.remark,
                item.giver.name,
                item.giver.location,
                item.giver.remark,
                item.giver.relationship_type.name,
                item.counterparty.name,
                item.amount,
                item.gift_details,
                item.remark,
                item.created_at,
            )
            for item in items
        ),
    )


def query_export_package(
    session: Session,
    user_id: int,
    filters: QueryExportFilters,
    *,
    generated_at: datetime | None = None,
) -> ExportPackage:
    if filters.view == QueryExportView.PEOPLE:
        items, response = _collect_query_pages(
            lambda limit, offset: query_people(
                session, user_id, _people_filters(filters, limit=limit, offset=offset)
            ),
            filters,
        )
        table = _people_table(items)
    elif filters.view == QueryExportView.EVENTS:
        items, response = _collect_query_pages(
            lambda limit, offset: query_events(
                session, user_id, _ledger_filters(filters, limit=limit, offset=offset)
            ),
            filters,
        )
        table = _events_table(items)
    else:
        items, response = _collect_query_pages(
            lambda limit, offset: query_gifts(
                session, user_id, _ledger_filters(filters, limit=limit, offset=offset)
            ),
            filters,
        )
        table = _gifts_table(items)

    now = generated_at or datetime.now()
    metadata = _filter_metadata(filters)
    metadata.extend(
        [
            ("生成时间", now.strftime("%Y-%m-%d %H:%M:%S")),
            ("结果数量", str(len(items))),
            ("我随出金额合计", f"{response.totals.expense_amount:.2f}"),
            ("我收到金额合计", f"{response.totals.income_amount:.2f}"),
            ("流水总数", str(response.totals.gift_count)),
            ("实物份数", str(response.totals.goods_count)),
        ]
    )
    return ExportPackage(
        filename=f"gift-ledger-query-{filters.view.value}-{now:%Y%m%d-%H%M%S}.xlsx",
        title="往来有礼查询结果导出",
        metadata=tuple(metadata),
        tables=(table,),
    )


def contacts_export_statement(user_id: int) -> Select:
    return (
        select(Contact, RelationshipType.type_name.label("relationship_type_name"))
        .join(
            RelationshipType,
            and_(
                RelationshipType.id == Contact.relation_type_id,
                or_(RelationshipType.user_id.is_(None), RelationshipType.user_id == user_id),
            ),
        )
        .where(Contact.user_id == user_id)
        .order_by(Contact.created_at.asc(), Contact.id.asc())
    )


def events_export_statement(user_id: int) -> Select:
    return (
        select(
            EventRecord,
            Contact.name.label("host_name"),
            Contact.location.label("host_location"),
            Contact.remark.label("host_remark"),
            RelationshipType.type_name.label("host_relationship_name"),
            KinshipType.type_name.label("kinship_name"),
            EventType.type_name.label("event_type_name"),
        )
        .join(
            Contact,
            and_(Contact.id == EventRecord.contact_id, Contact.user_id == user_id),
        )
        .join(
            RelationshipType,
            and_(
                RelationshipType.id == Contact.relation_type_id,
                or_(RelationshipType.user_id.is_(None), RelationshipType.user_id == user_id),
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
        .where(EventRecord.user_id == user_id)
        .order_by(EventRecord.event_date.asc(), EventRecord.id.asc())
    )


def custom_dictionary_statement(model: type[Any], user_id: int) -> Select:
    return (
        select(model)
        .where(model.user_id == user_id)
        .order_by(model.sort_order.asc(), model.id.asc())
    )


def _contacts_table(rows: list[Any]) -> ExportTable:
    return ExportTable(
        name="联系人",
        columns=("姓名", "常住地", "电话", "联系人备注", "关系类型", "状态", "创建时间"),
        rows=tuple(
            (
                row.Contact.name,
                row.Contact.location,
                row.Contact.phone,
                row.Contact.remark,
                row.relationship_type_name,
                "启用" if row.Contact.is_active else "停用",
                row.Contact.created_at,
            )
            for row in rows
        ),
    )


def _all_events_table(rows: list[Any]) -> ExportTable:
    return ExportTable(
        name="事件",
        columns=(
            "办事日期",
            "事主姓名",
            "事主常住地",
            "事主备注",
            "事主关系类型",
            "亲属称谓",
            "事件类型",
            "事件备注",
            "创建时间",
        ),
        rows=tuple(
            (
                row.EventRecord.event_date
                if date_value(row.EventRecord).precision == "day"
                else display_event_date(row.EventRecord),
                row.host_name,
                row.host_location,
                row.host_remark,
                row.host_relationship_name,
                row.kinship_name,
                row.event_type_name,
                row.EventRecord.remark,
                row.EventRecord.created_at,
            )
            for row in rows
        ),
    )


def _dictionary_table(name: str, rows: list[Any]) -> ExportTable:
    return ExportTable(
        name=name,
        columns=("名称", "排序", "状态"),
        rows=tuple(
            (row.type_name, row.sort_order, "启用" if row.is_active else "停用") for row in rows
        ),
    )


def all_data_export_package(
    session: Session,
    user_id: int,
    *,
    generated_at: datetime | None = None,
) -> ExportPackage:
    contacts = session.execute(contacts_export_statement(user_id)).all()
    events = session.execute(events_export_statement(user_id)).all()
    gifts_filter = QueryExportFilters(
        view=QueryExportView.GIFTS,
        scope=QueryExportScope.ALL,
    )
    gifts, _response = _collect_query_pages(
        lambda limit, offset: query_gifts(
            session,
            user_id,
            _ledger_filters(gifts_filter, limit=limit, offset=offset),
        ),
        gifts_filter,
    )
    relationships = session.scalars(custom_dictionary_statement(RelationshipType, user_id)).all()
    kinships = session.scalars(custom_dictionary_statement(KinshipType, user_id)).all()
    event_types = session.scalars(custom_dictionary_statement(EventType, user_id)).all()
    now = generated_at or datetime.now()
    tables = (
        _contacts_table(contacts),
        _all_events_table(events),
        _gifts_table(gifts, name="礼金流水"),
        _dictionary_table("自定义关系类型", relationships),
        _dictionary_table("自定义亲属称谓", kinships),
        _dictionary_table("自定义事件类型", event_types),
    )
    metadata = (
        ("生成时间", now.strftime("%Y-%m-%d %H:%M:%S")),
        ("用途", "本人全部可读数据留档；不能作为个人无损恢复文件"),
        ("联系人数量", str(len(contacts))),
        ("事件数量", str(len(events))),
        ("流水数量", str(len(gifts))),
    )
    return ExportPackage(
        filename=f"gift-ledger-all-{now:%Y%m%d-%H%M%S}.xlsx",
        title="往来有礼全部数据导出",
        metadata=metadata,
        tables=tables,
    )


__all__ = [
    "EXPORT_PAGE_SIZE",
    "ExportPackage",
    "ExportTable",
    "all_data_export_package",
    "contacts_export_statement",
    "custom_dictionary_statement",
    "events_export_statement",
    "query_export_package",
]
