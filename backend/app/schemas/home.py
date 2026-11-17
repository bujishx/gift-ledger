from decimal import Decimal

from pydantic import BaseModel

from app.schemas.ledger_queries import (
    GiftsQueryItemResponse,
    PeopleQueryItemResponse,
    QueryContactResponse,
)


class HomeAnnualSummaryResponse(BaseModel):
    year: int
    expense_amount: Decimal
    income_amount: Decimal
    net_amount: Decimal
    expense_gift_count: int
    expense_people_count: int


class HomeReminderResponse(BaseModel):
    self_unreturned_count: int
    items: list[PeopleQueryItemResponse]


class HomeSummaryResponse(BaseModel):
    annual: HomeAnnualSummaryResponse
    reminders: HomeReminderResponse
    recent_gifts: list[GiftsQueryItemResponse]
    frequent_contacts: list[QueryContactResponse]
