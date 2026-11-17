from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class LedgerDirection(StrEnum):
    INCOME = "income"
    EXPENSE = "expense"


class LedgerEntryCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host_contact_id: int = Field(strict=True, gt=0, description="事主联系人编号")
    kinship_type_id: int = Field(strict=True, gt=0, description="亲属称谓编号")
    event_type_id: int = Field(strict=True, gt=0, description="事件类型编号")
    event_date: date = Field(
        ge=date(1000, 1, 1),
        description="办事日期，也是收支统计日期",
    )
    event_remark: str | None = Field(default=None, strict=True, description="事件备注")
    giver_contact_id: int | None = Field(
        default=None,
        strict=True,
        gt=0,
        description="随礼人编号；别人办事时可省略，由服务端固定为本人",
    )
    amount: Decimal = Field(
        default=Decimal("0.00"),
        ge=0,
        max_digits=10,
        decimal_places=2,
        allow_inf_nan=False,
        description="礼金金额",
    )
    gift_details: str | None = Field(
        default=None,
        strict=True,
        max_length=255,
        description="实物明细",
    )
    gift_remark: str | None = Field(default=None, strict=True, description="流水备注")


class EventCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host_contact_id: int = Field(strict=True, gt=0)
    kinship_type_id: int = Field(strict=True, gt=0)
    event_type_id: int = Field(strict=True, gt=0)
    event_date: date = Field(ge=date(1000, 1, 1))
    remark: str | None = Field(default=None, strict=True)


class EventGiftCreateItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    giver_contact_id: int | None = Field(default=None, strict=True, gt=0)
    amount: Decimal = Field(
        default=Decimal("0.00"), ge=0, max_digits=10, decimal_places=2, allow_inf_nan=False
    )
    gift_details: str | None = Field(default=None, strict=True, max_length=255)
    remark: str | None = Field(default=None, strict=True)


class EventIdentityCheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host_contact_id: int = Field(strict=True, gt=0)
    kinship_type_id: int = Field(strict=True, gt=0)
    event_type_id: int = Field(strict=True, gt=0)
    remark: str | None = Field(default=None, strict=True)
    exclude_event_id: int | None = Field(default=None, strict=True, gt=0)


class EventGiftsCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    gifts: list[EventGiftCreateItem] = Field(min_length=1, max_length=5)


class EventUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host_contact_id: int | None = Field(default=None, strict=True, gt=0)
    kinship_type_id: int | None = Field(default=None, strict=True, gt=0)
    event_type_id: int | None = Field(default=None, strict=True, gt=0)
    event_date: date | None = Field(default=None, ge=date(1000, 1, 1))
    remark: str | None = Field(default=None, strict=True)

    @model_validator(mode="after")
    def require_at_least_one_field(self) -> "EventUpdateRequest":
        if not self.model_fields_set:
            raise ValueError("至少需要提交一个要修改的事件字段")
        required_fields = {
            "host_contact_id",
            "kinship_type_id",
            "event_type_id",
            "event_date",
        }
        if any(
            field in self.model_fields_set and getattr(self, field) is None
            for field in required_fields
        ):
            raise ValueError("事主、亲属称谓、事件类型和办事日期不能设为空")
        return self


class GiftUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    giver_contact_id: int | None = Field(default=None, strict=True, gt=0)
    amount: Decimal | None = Field(
        default=None,
        ge=0,
        max_digits=10,
        decimal_places=2,
        allow_inf_nan=False,
    )
    gift_details: str | None = Field(default=None, strict=True, max_length=255)
    remark: str | None = Field(default=None, strict=True)

    @model_validator(mode="after")
    def require_at_least_one_field(self) -> "GiftUpdateRequest":
        if not self.model_fields_set:
            raise ValueError("至少需要提交一个要修改的流水字段")
        if "giver_contact_id" in self.model_fields_set and self.giver_contact_id is None:
            raise ValueError("随礼人不能设为空")
        if "amount" in self.model_fields_set and self.amount is None:
            raise ValueError("金额不能设为空")
        return self


class LedgerContactResponse(BaseModel):
    id: int
    name: str


class LedgerDictionaryResponse(BaseModel):
    id: int
    name: str


class EventResponse(BaseModel):
    id: int
    host: LedgerContactResponse
    kinship_type: LedgerDictionaryResponse
    event_type: LedgerDictionaryResponse
    event_date: date | None
    date_precision: str = "day"
    event_year: int | None = None
    event_month: int | None = None
    date_source: str = "web"
    event_date_display: str | None = None
    remark: str | None
    created_at: datetime


class GiftResponse(BaseModel):
    id: int
    giver: LedgerContactResponse
    amount: Decimal
    gift_details: str | None
    remark: str | None
    direction: LedgerDirection
    created_at: datetime


class LedgerEntryResponse(BaseModel):
    event: EventResponse
    gift: GiftResponse
    event_reused: bool


class EventDetailResponse(BaseModel):
    event: EventResponse
    gifts: list[GiftResponse]


class EventIdentityCheckResponse(BaseModel):
    existing_event: EventDetailResponse | None


class EventListItemResponse(BaseModel):
    event: EventResponse
    gift_count: int
    cash_amount: Decimal


class EventListResponse(BaseModel):
    items: list[EventListItemResponse]
    total: int
    limit: int
    offset: int


class GiftDetailResponse(BaseModel):
    event: EventResponse
    gift: GiftResponse
