"""四类统计接口的请求与响应模型。"""

from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.ledger import LedgerContactResponse
from app.schemas.ledger_queries import QueryDictionaryResponse

STATISTICS_MIN_YEAR = 1000
STATISTICS_MAX_YEAR = 9999


class StatisticsQueryParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StatisticsYearQueryParameters(StatisticsQueryParameters):
    year: int | None = Field(
        default=None,
        ge=STATISTICS_MIN_YEAR,
        le=STATISTICS_MAX_YEAR,
    )


class BalanceStatus(StrEnum):
    EXPENSE_HEAVIER = "expense_heavier"
    INCOME_HEAVIER = "income_heavier"
    BALANCED = "balanced"


class BalanceStatusResponse(BaseModel):
    code: BalanceStatus
    label: str


class ReciprocityItemResponse(BaseModel):
    contact: LedgerContactResponse
    expense_amount: Decimal
    expense_count: int
    income_amount: Decimal
    income_count: int
    goods_count: int
    balance_amount: Decimal
    balance_status: BalanceStatusResponse


class ReciprocityResponse(BaseModel):
    items: list[ReciprocityItemResponse]


class TimelineMonthResponse(BaseModel):
    month: int = Field(ge=1, le=12)
    expense_amount: Decimal
    income_amount: Decimal
    gift_count: int
    goods_count: int


class TimelineResponse(BaseModel):
    year: int = Field(ge=STATISTICS_MIN_YEAR, le=STATISTICS_MAX_YEAR)
    months: list[TimelineMonthResponse]
    year_expense_amount: Decimal
    year_income_amount: Decimal
    net_amount: Decimal
    available_years: list[int]
    month_unassigned_expense_amount: Decimal = Decimal("0.00")
    month_unassigned_income_amount: Decimal = Decimal("0.00")
    month_unassigned_gift_count: int = 0

    @model_validator(mode="after")
    def require_full_year_months(self) -> "TimelineResponse":
        month_numbers = [month.month for month in self.months]
        if month_numbers != list(range(1, 13)):
            raise ValueError("时间段统计必须固定返回 1 至 12 月")
        return self


class EventDistributionItemResponse(BaseModel):
    event_type: QueryDictionaryResponse
    cash_amount: Decimal
    cash_ratio_percent: Decimal
    gift_count: int
    goods_count: int


class EventDistributionResponse(BaseModel):
    year: int = Field(ge=STATISTICS_MIN_YEAR, le=STATISTICS_MAX_YEAR)
    items: list[EventDistributionItemResponse]


class RelationshipCircleItemResponse(BaseModel):
    relationship_type: QueryDictionaryResponse
    expense_amount: Decimal
    income_amount: Decimal
    gift_count: int
    goods_count: int


class RelationshipCircleResponse(BaseModel):
    items: list[RelationshipCircleItemResponse]
