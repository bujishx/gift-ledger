from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import Session

from app.db.models import EventRecord, _set_event_identity_hash
from app.schemas.ledger import EventCreateRequest, EventGiftCreateItem
from app.schemas.statistics_dashboard import DashboardFilters
from app.services.event_dates import (
    date_period_conditions,
    date_value,
    display_event_date,
    event_date_fields,
    known_date_bounds,
    merge_event_dates,
    parse_event_date,
)
from app.services.event_registration import create_event_gifts
from app.services.ledger import LedgerValidationError
from app.services.ledger_records import update_event
from app.services.statistics_dashboard import aggregate_rows


@pytest.mark.parametrize(
    "raw,precision,expected",
    [
        ("2000-02-29", "day", "2000-02-29"),
        ("1900-02", "month", "1900-02"),
        ("1000", "year", "1000"),
        ("9999-12-31", "day", "9999-12-31"),
        ("不详", "unknown", "不详"),
        (date(2020, 3, 4), "day", "2020-03-04"),
        (2020, "year", "2020"),
        (2020.0, "year", "2020"),
    ],
)
def test_date_values_preserve_only_known_components(raw, precision, expected):
    value = parse_event_date(raw)
    assert value.precision == precision
    assert display_event_date(value) == expected
    if precision != "day":
        assert value.event_date is None


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "2020/05",
        "2020-5",
        "2020-02-30",
        "1900-02-29",
        "2020-13",
        "999",
        "10000",
        "2020-01-01 00:00:00",
        True,
        2020.5,
        45000,
    ],
)
def test_invalid_or_missing_legacy_date_rejected(raw):
    with pytest.raises(ValueError):
        parse_event_date(raw)


@pytest.mark.parametrize(
    "old,new,expected",
    [
        ("不详", "2020", "2020"),
        ("2020", "2020-03", "2020-03"),
        ("2020-03", "2020-03-04", "2020-03-04"),
        ("2020-03-04", "2020", "2020-03-04"),
    ],
)
def test_merge_only_increases_known_precision(old, new, expected):
    assert (
        display_event_date(merge_event_dates(parse_event_date(old), parse_event_date(new)))
        == expected
    )


@pytest.mark.parametrize(
    "old,new", [("2020", "2021"), ("2020-03", "2020-04-01"), ("2020-03-04", "2020-03-05")]
)
def test_conflicting_known_date_parts_rejected(old, new):
    with pytest.raises(ValueError):
        merge_event_dates(parse_event_date(old), parse_event_date(new))


def test_bounds_are_for_membership_and_not_persisted_dates():
    value = parse_event_date("2020-02")
    assert known_date_bounds(value) == (date(2020, 2, 1), date(2020, 2, 29))
    assert event_date_fields(value)["event_date"] is None
    assert known_date_bounds(parse_event_date("不详")) == (None, None)


def test_orm_defaults_complete_dates_and_rejects_partial_web():
    event = EventRecord(
        contact_id=1, relative_type_id=1, event_type_id=1, event_date=date(2020, 1, 2)
    )
    _set_event_identity_hash(None, None, event)
    assert (event.date_precision, event.event_year, event.event_month, event.date_source) == (
        "day",
        2020,
        1,
        "web",
    )
    partial = EventRecord(
        contact_id=1,
        relative_type_id=1,
        event_type_id=1,
        **event_date_fields(parse_event_date("2020")),
    )
    _set_event_identity_hash(None, None, partial)
    assert partial.event_date is None
    partial.date_source = "web"
    with pytest.raises(ValueError):
        _set_event_identity_hash(None, None, partial)


def test_http_new_event_contract_rejects_partial_and_spoofed_source():
    base = dict(host_contact_id=1, kinship_type_id=1, event_type_id=1, event_date="2020-01-02")
    for updates in (
        {"event_date": "2020"},
        {"event_date": None},
        {"date_source": "legacy_import"},
        {"date_precision": "year"},
    ):
        with pytest.raises(ValidationError):
            EventCreateRequest(**(base | updates))


def test_new_gift_on_partial_event_requires_date_completion(monkeypatch):
    session = MagicMock(spec=Session)
    event = EventRecord(id=10, contact_id=1, **event_date_fields(parse_event_date("2020")))
    monkeypatch.setattr("app.services.event_registration._lock_active_user", lambda *_: None)
    monkeypatch.setattr("app.services.event_registration._locked_event_by_id", lambda *_: event)
    with pytest.raises(LedgerValidationError) as error:
        create_event_gifts(
            session, 1, 10, [EventGiftCreateItem(giver_contact_id=2, amount=Decimal(100))]
        )
    assert error.value.code == "EVENT_DATE_INCOMPLETE"
    session.add_all.assert_not_called()
    session.commit.assert_not_called()


def test_partial_event_can_edit_remark_and_complete_same_known_parts(monkeypatch):
    session = MagicMock(spec=Session)
    event = EventRecord(
        id=10,
        user_id=1,
        contact_id=1,
        relative_type_id=1,
        event_type_id=1,
        **event_date_fields(parse_event_date("2020-03")),
    )
    monkeypatch.setattr("app.services.ledger_records._lock_active_user", lambda *_: None)
    monkeypatch.setattr("app.services.ledger_records._locked_event_by_id", lambda *_: event)
    monkeypatch.setattr(
        "app.services.ledger_records._event_identity_conflict", lambda *_a, **_k: None
    )
    base = dict(
        host_contact_id=None,
        kinship_type_id=None,
        event_type_id=None,
        event_date=None,
        remark="旧账",
    )
    update_event(session, 1, 10, **base, fields={"remark"})
    assert date_value(event).precision == "month" and event.event_date is None
    with pytest.raises(LedgerValidationError) as error:
        update_event(
            session, 1, 10, **(base | {"event_date": date(2021, 3, 1)}), fields={"event_date"}
        )
    assert error.value.code == "EVENT_DATE_KNOWN_PART_CONFLICT"
    update_event(session, 1, 10, **(base | {"event_date": date(2020, 3, 4)}), fields={"event_date"})
    assert event.id == 10 and event.date_source == "legacy_import"
    assert display_event_date(event) == "2020-03-04"


def test_period_condition_includes_partial_only_when_all_possible_dates_fit():
    assert date_period_conditions(None, None) == []
    sql = str(
        date_period_conditions(date(2020, 1, 1), date(2020, 12, 31))[0].compile(
            dialect=mysql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert "date_precision IN ('month', 'year')" in sql
    assert "last_day" in sql and "str_to_date" in sql


def test_unknown_old_dates_remain_in_overall_totals_but_not_month_trend():
    from tests.test_statistics_dashboard import CONTACTS, row

    rows = [
        row(**event_date_fields(parse_event_date("不详"))),
        row(gift_id=2, **event_date_fields(parse_event_date("2020"))),
    ]
    data = aggregate_rows(
        rows, contacts=CONTACTS, history={2: "mutual"}, filters=DashboardFilters(), grain="month"
    )
    assert data["summary"]["gift_count"] == 2
    assert data["summary"]["expense_amount"] == Decimal("0.20")
    assert sum(item["gift_count"] for item in data["trends"]) == 0
    assert data["trend_date_information_insufficient"]["gift_count"] == 2
    assert data["people"][0]["incomplete_date_count"] == 2
    assert data["people"][0]["last_date_display"] == "日期信息不足"
    annual = aggregate_rows(
        rows, contacts=CONTACTS, history={2: "mutual"}, filters=DashboardFilters(), grain="year"
    )
    assert [item["key"] for item in annual["trends"]] == ["2020"]
    assert annual["trend_date_information_insufficient"]["gift_count"] == 1


@pytest.mark.parametrize("raw", ["9999-12-31", "9999-12", "9999"])
@pytest.mark.parametrize("grain", ["day", "month", "quarter", "year"])
def test_maximum_legal_year_has_real_period_bounds_without_year_10000(raw, grain):
    from tests.test_statistics_dashboard import CONTACTS, row

    data = aggregate_rows(
        [row(**event_date_fields(parse_event_date(raw)))],
        contacts=CONTACTS,
        history={2: "mutual"},
        filters=DashboardFilters(),
        grain=grain,
    )
    assert data["summary"]["gift_count"] == 1
    assert data["events"][0]["event_date_display"] == raw
    assert data["trends"][-1]["date_to"] == date.max
