from decimal import Decimal

import pytest
from pydantic import ValidationError

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
    StatisticsQueryParameters,
    StatisticsYearQueryParameters,
    TimelineMonthResponse,
    TimelineResponse,
)

BALANCE_STATUS_LABELS = {
    BalanceStatus.EXPENSE_HEAVIER: "我多随出",
    BalanceStatus.INCOME_HEAVIER: "我多收到",
    BalanceStatus.BALANCED: "礼金持平",
}


def balance_status(code: BalanceStatus) -> BalanceStatusResponse:
    return BalanceStatusResponse(code=code, label=BALANCE_STATUS_LABELS[code])


def reciprocity_item() -> ReciprocityItemResponse:
    return ReciprocityItemResponse(
        contact=LedgerContactResponse(id=12, name="张伟"),
        expense_amount=Decimal("1000.00"),
        expense_count=2,
        income_amount=Decimal("600.00"),
        income_count=1,
        goods_count=1,
        balance_amount=Decimal("-400.00"),
        balance_status=balance_status(BalanceStatus.EXPENSE_HEAVIER),
    )


def timeline_months(
    *,
    expense_amount: Decimal = Decimal("0.00"),
    income_amount: Decimal = Decimal("0.00"),
) -> list[TimelineMonthResponse]:
    return [
        TimelineMonthResponse(
            month=month,
            expense_amount=expense_amount,
            income_amount=income_amount,
            gift_count=0,
            goods_count=0,
        )
        for month in range(1, 13)
    ]


def timeline_response(**overrides) -> TimelineResponse:
    payload = {
        "year": 2026,
        "months": timeline_months(),
        "year_expense_amount": Decimal("0.00"),
        "year_income_amount": Decimal("0.00"),
        "net_amount": Decimal("0.00"),
        "available_years": [2026],
    }
    payload.update(overrides)
    return TimelineResponse.model_validate(payload)


def test_year_range_matches_database_date_range() -> None:
    assert STATISTICS_MIN_YEAR == 1000
    assert STATISTICS_MAX_YEAR == 9999


def test_statistics_query_parameters_reject_untrusted_fields() -> None:
    StatisticsQueryParameters.model_validate({})
    assert StatisticsYearQueryParameters.model_validate({"year": 2026}).year == 2026

    with pytest.raises(ValidationError):
        StatisticsQueryParameters.model_validate({"user_id": 8})
    with pytest.raises(ValidationError):
        StatisticsYearQueryParameters.model_validate({"year": 2026, "user_id": 8})


def test_balance_status_uses_three_mutually_exclusive_confirmed_texts() -> None:
    assert BalanceStatus.EXPENSE_HEAVIER == "expense_heavier"
    assert BalanceStatus.INCOME_HEAVIER == "income_heavier"
    assert BalanceStatus.BALANCED == "balanced"
    assert balance_status(BalanceStatus.EXPENSE_HEAVIER).label == "我多随出"
    assert balance_status(BalanceStatus.INCOME_HEAVIER).label == "我多收到"
    assert balance_status(BalanceStatus.BALANCED).label == "礼金持平"


def test_reciprocity_item_reports_amounts_and_hides_phone_and_user_id() -> None:
    payload = reciprocity_item().model_dump()

    assert payload["contact"] == {"id": 12, "name": "张伟"}
    assert payload["expense_amount"] == Decimal("1000.00")
    assert payload["income_amount"] == Decimal("600.00")
    assert payload["balance_amount"] == Decimal("-400.00")
    assert payload["balance_status"]["code"] == BalanceStatus.EXPENSE_HEAVIER
    assert payload["balance_status"]["label"] == "我多随出"
    assert "phone" not in str(payload)
    assert "user_id" not in str(payload)


def test_reciprocity_response_wraps_items() -> None:
    response = ReciprocityResponse(items=[reciprocity_item()])

    assert response.items[0].expense_count == 2
    assert response.items[0].goods_count == 1


def test_timeline_response_requires_twelve_ordered_months() -> None:
    response = timeline_response()

    assert [month.month for month in response.months] == list(range(1, 13))

    with pytest.raises(ValidationError):
        timeline_response(months=timeline_months()[:-1])

    with pytest.raises(ValidationError):
        timeline_response(months=list(reversed(timeline_months())))


def test_timeline_response_holds_year_totals_net_and_year_list() -> None:
    response = timeline_response(
        year_expense_amount=Decimal("1200.00"),
        year_income_amount=Decimal("800.00"),
        net_amount=Decimal("-400.00"),
        available_years=[2026, 2024],
    )

    assert response.year_expense_amount == Decimal("1200.00")
    assert response.year_income_amount == Decimal("800.00")
    assert response.net_amount == Decimal("-400.00")
    assert response.available_years == [2026, 2024]


def test_timeline_month_and_year_fields_stay_in_confirmed_bounds() -> None:
    month = TimelineMonthResponse(
        month=12,
        expense_amount=Decimal("99999999.99"),
        income_amount=Decimal("0.00"),
        gift_count=3,
        goods_count=1,
    )
    assert month.month == 12
    assert month.expense_amount == Decimal("99999999.99")

    with pytest.raises(ValidationError):
        TimelineMonthResponse(
            month=13,
            expense_amount=Decimal("0.00"),
            income_amount=Decimal("0.00"),
            gift_count=0,
            goods_count=0,
        )

    with pytest.raises(ValidationError):
        timeline_response(year=10000, available_years=[10000])

    with pytest.raises(ValidationError):
        timeline_response(year=999, available_years=[999])


def test_event_distribution_item_keeps_ratio_precision_and_type_name() -> None:
    item = EventDistributionItemResponse(
        event_type=QueryDictionaryResponse(id=4, name="婚礼"),
        cash_amount=Decimal("300.00"),
        cash_ratio_percent=Decimal("75.00"),
        gift_count=4,
        goods_count=2,
    )

    payload = item.model_dump()

    assert payload["event_type"]["name"] == "婚礼"
    assert payload["cash_amount"] == Decimal("300.00")
    assert payload["cash_ratio_percent"] == Decimal("75.00")


def test_event_distribution_response_echoes_year() -> None:
    response = EventDistributionResponse(year=2026, items=[])

    assert response.year == 2026

    with pytest.raises(ValidationError):
        EventDistributionResponse(year=999, items=[])


def test_relationship_circle_item_uses_current_relationship_dictionary() -> None:
    item = RelationshipCircleItemResponse(
        relationship_type=QueryDictionaryResponse(id=2, name="亲戚"),
        expense_amount=Decimal("500.00"),
        income_amount=Decimal("800.00"),
        gift_count=5,
        goods_count=2,
    )

    response = RelationshipCircleResponse(items=[item])

    assert response.items[0].relationship_type.id == 2
    assert response.items[0].relationship_type.name == "亲戚"
    assert response.items[0].expense_amount == Decimal("500.00")
    assert "user_id" not in str(response.model_dump())
    assert "phone" not in str(response.model_dump())
