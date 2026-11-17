from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.ledger import LedgerDirection


class InteractionStatus(StrEnum):
    NONE = "none"
    COUNTERPARTY_UNRETURNED = "counterparty_unreturned"
    SELF_UNRETURNED = "self_unreturned"
    MUTUAL = "mutual"


class PeopleSortField(StrEnum):
    CONTACT_NAME = "contact_name"
    EXPENSE_AMOUNT = "expense_amount"
    INCOME_AMOUNT = "income_amount"
    BALANCE = "balance"
    LAST_EVENT_DATE = "last_event_date"
    INTERACTION_STATUS = "interaction_status"


class SortOrder(StrEnum):
    ASC = "asc"
    DESC = "desc"


class GiftsSortField(StrEnum):
    GIFT_CREATED_AT = "gift_created_at"
    EVENT_DATE = "event_date"
    HOST_NAME = "host_name"
    GIVER_NAME = "giver_name"
    AMOUNT = "amount"


class LedgerQueryFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    direction: LedgerDirection | None = None
    contact_id: int | None = Field(default=None, gt=0)
    relationship_type_id: int | None = Field(default=None, gt=0)
    kinship_type_id: int | None = Field(default=None, gt=0)
    event_type_id: int | None = Field(default=None, gt=0)
    date_from: date | None = Field(default=None, ge=date(1000, 1, 1))
    date_to: date | None = Field(default=None, ge=date(1000, 1, 1))
    keyword: str | None = Field(default=None, max_length=100)
    interaction_status: InteractionStatus | None = None
    limit: int = Field(default=50, ge=1, le=100)
    offset: int = Field(default=0, ge=0)

    @field_validator("keyword", mode="before")
    @classmethod
    def normalize_keyword(cls, value):
        if value is None:
            return None
        if not isinstance(value, str):
            return value
        normalized = value.strip()
        return normalized or None

    @model_validator(mode="after")
    def validate_date_range(self) -> "LedgerQueryFilters":
        if self.date_from is not None and self.date_to is not None:
            if self.date_to < self.date_from:
                raise ValueError("结束日期不能早于开始日期")
        return self


class PeopleQueryFilters(LedgerQueryFilters):
    sort_by: PeopleSortField | None = None
    sort_order: SortOrder = SortOrder.DESC


class GiftsQueryFilters(LedgerQueryFilters):
    date_basis: str = Field(default="event", pattern="^(event|registered)$")
    relationship_type_ids: list[int] | None = None
    kinship_type_ids: list[int] | None = None
    event_type_ids: list[int] | None = None
    # 统计分类按往来对方取关系; 旧 relationship_type_ids 保持记账页事主关系语义。
    counterparty_relationship_type_ids: list[Annotated[int, Field(gt=0)]] | None = None
    statistics_event_type_ids: list[Annotated[int, Field(gt=0)]] | None = None
    interaction_statuses: list[InteractionStatus] | None = None
    event_id: int | None = Field(default=None, gt=0)
    sort_by: GiftsSortField = GiftsSortField.EVENT_DATE
    sort_order: SortOrder = SortOrder.DESC

    @field_validator(
        "counterparty_relationship_type_ids", "statistics_event_type_ids", "interaction_statuses"
    )
    @classmethod
    def unique_statistics_options(cls, value):
        return list(dict.fromkeys(value)) if value else None


class QueryDictionaryResponse(BaseModel):
    id: int
    name: str


class QueryContactResponse(BaseModel):
    id: int
    name: str
    location: str | None
    remark: str | None
    relationship_type: QueryDictionaryResponse


class InteractionStatusResponse(BaseModel):
    code: InteractionStatus
    label: str


class LedgerQueryTotalsResponse(BaseModel):
    expense_amount: Decimal
    income_amount: Decimal
    gift_count: int
    goods_count: int


class QueryPageMetaResponse(BaseModel):
    total: int
    limit: int
    offset: int


class QueryEventSummaryResponse(BaseModel):
    id: int
    event_date: date | None
    date_precision: str = "day"
    event_year: int | None = None
    event_month: int | None = None
    date_source: str = "web"
    event_date_display: str | None = None
    remark: str | None
    kinship_type: QueryDictionaryResponse
    event_type: QueryDictionaryResponse
    host: QueryContactResponse


class QueryBalanceResponse(BaseModel):
    label: str
    amount: Decimal


class PeopleQueryItemResponse(BaseModel):
    contact: QueryContactResponse
    expense_amount: Decimal
    expense_count: int
    income_amount: Decimal
    income_count: int
    goods_count: int
    balance: QueryBalanceResponse
    last_event_date: date | None
    incomplete_date_count: int = 0
    last_event_date_display: str | None = None
    interaction_status: InteractionStatusResponse


class PeopleQueryResponse(BaseModel):
    items: list[PeopleQueryItemResponse]
    totals: LedgerQueryTotalsResponse
    people_total: int
    page: QueryPageMetaResponse


class EventsQueryItemResponse(BaseModel):
    event: QueryEventSummaryResponse
    related_contacts: list[QueryContactResponse]
    direction: LedgerDirection
    gift_count: int
    cash_amount: Decimal
    goods_count: int


class EventsQueryResponse(BaseModel):
    items: list[EventsQueryItemResponse]
    totals: LedgerQueryTotalsResponse
    events_total: int
    gift_total: int
    page: QueryPageMetaResponse


class GiftsQueryItemResponse(BaseModel):
    id: int
    event: QueryEventSummaryResponse
    giver: QueryContactResponse
    counterparty: QueryContactResponse
    direction: LedgerDirection
    amount: Decimal
    gift_details: str | None
    remark: str | None
    created_at: datetime


class GiftsQueryResponse(BaseModel):
    items: list[GiftsQueryItemResponse]
    totals: LedgerQueryTotalsResponse
    page: QueryPageMetaResponse
