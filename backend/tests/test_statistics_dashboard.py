from datetime import date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy.dialects import mysql

from app.schemas.ledger_queries import GiftsQueryFilters
from app.schemas.statistics_dashboard import DashboardFilters
from app.services.ledger_queries import gifts_page_statement
from app.services.ledger_query_common import LedgerQueryContext
from app.services.statistics_dashboard import aggregate_rows


def row(**updates):
    return (
        dict(
            gift_id=1,
            gift_amount=Decimal("0.10"),
            gift_details=None,
            gift_created_at=datetime(2026, 3, 2),
            event_date=date(2026, 1, 2),
            direction="expense",
            counterparty_contact_id=2,
            counterparty_relation_type_id=3,
            counterparty_relation_type_name="朋友",
            event_id=10,
            host_contact_id=2,
            host_contact_name="同名",
            host_relation_type_name="朋友",
            host_contact_remark="甲",
            kinship_type_name="本人",
            event_type_id=1,
            event_type_name="婚礼",
            event_remark=None,
        )
        | updates
    )


CONTACTS = [
    dict(
        id=2,
        name="同名",
        remark="甲",
        relationship_type_id=3,
        relationship_type_name="朋友",
        is_active=True,
    ),
    dict(
        id=3,
        name="同名",
        remark="乙",
        relationship_type_id=3,
        relationship_type_name="朋友",
        is_active=False,
    ),
    dict(
        id=4,
        name="无往来",
        remark=None,
        relationship_type_id=3,
        relationship_type_name="朋友",
        is_active=True,
    ),
]


def test_decimal_cash_items_full_history_and_same_name_event_ids():
    data = aggregate_rows(
        [
            row(),
            row(gift_id=2, gift_amount=Decimal("0.20"), event_id=11),
            row(
                gift_id=3,
                gift_amount=Decimal("0"),
                direction="income",
                gift_details="水果",
                counterparty_contact_id=3,
            ),
        ],
        contacts=CONTACTS,
        history={2: "mutual", 3: "self_unreturned"},
        filters=DashboardFilters(),
        grain="month",
    )
    assert data["summary"]["expense_amount"] == Decimal("0.30")
    assert data["summary"]["income_amount"] == 0
    assert data["summary"]["income_count"] == 1
    assert data["summary"]["pure_goods_count"] == 1
    assert data["summary"]["goods_count"] == 1
    assert data["summary"]["people_count"] == 2
    assert {event["id"] for event in data["events"]} == {10, 11}
    assert data["status_counts"] == dict(
        none=1, mutual=1, self_unreturned=1, counterparty_unreturned=0
    )
    assert data["people"][0]["interaction_label"] == "礼尚往来"


def test_registered_date_and_empty_months_do_not_shift_business_date():
    filters = DashboardFilters(
        date_basis="registered", date_from=date(2026, 1, 1), date_to=date(2026, 4, 30)
    )
    data = aggregate_rows([row()], contacts=CONTACTS, history={}, filters=filters, grain="month")
    assert [item["key"] for item in data["trends"]] == ["2026-01", "2026-02", "2026-03", "2026-04"]
    assert [item["gift_count"] for item in data["trends"]] == [0, 0, 1, 0]
    assert data["events"][0]["event_date"] == date(2026, 1, 2)
    assert data["trends"][2]["date_to"] == date(2026, 3, 31)


def test_zero_current_period_contact_keeps_full_history_status():
    data = aggregate_rows(
        [],
        contacts=CONTACTS,
        history={2: "mutual"},
        filters=DashboardFilters(contact_id=2),
        grain="month",
    )
    assert len(data["people"]) == 1
    assert data["people"][0]["gift_count"] == 0
    assert data["people"][0]["interaction_status"] == "mutual"
    assert data["summary"]["gift_count"] == 0


def test_shared_query_enforces_user_ids_and_registered_date_exact_event():
    query = GiftsQueryFilters(
        date_basis="registered",
        event_id=10,
        date_from=date(2026, 3, 1),
        date_to=date(2026, 3, 31),
        relationship_type_id=3,
        interaction_status="mutual",
    )
    sql = str(
        gifts_page_statement(LedgerQueryContext(7, 1, query)).compile(
            dialect=mysql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert "gift_records.user_id = 7" in sql
    assert "event_records.user_id = 7" in sql
    assert "event_records.id = 10" in sql
    assert "date(gift_records.created_at)" in sql
    assert "interaction_history.interaction_status" in sql


@pytest.mark.parametrize(
    "values",
    [
        {"comparison_from": "2026-01-01"},
        {"comparison_from": "2026-03-01", "comparison_to": "2026-02-01"},
        {"date_basis": "arbitrary"},
        {"date_from": "2026-02-02", "date_to": "2026-02-01"},
    ],
)
def test_invalid_ranges_are_rejected(values):
    with pytest.raises(ValidationError):
        DashboardFilters(**values)
