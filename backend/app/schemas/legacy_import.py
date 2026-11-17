from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.dictionaries import DictionaryCategory


class LegacyImportIssueResponse(BaseModel):
    row_number: int | None
    field: str | None
    code: str
    message: str
    severity: Literal["error", "confirmation", "info"] = "error"
    raw_value: str | None = None
    remedy: str = "请根据提示修正模板后重新预检。"
    confirmation_key: str | None = None
    options: list[dict[str, str]] = Field(default_factory=list)
    existing_event_id: int | None = None
    existing_gift_id: int | None = None


class LegacyImportRowSummary(BaseModel):
    row_number: int
    event_name: str
    event_date: str
    date_precision: str
    host_name: str
    host_relationship: str
    giver_name: str
    giver_relationship: str
    amount: str
    gift_details: str | None
    gift_remark: str | None
    direction: str
    event_action: str
    host_action: str
    giver_action: str


class LegacyImportTotals(BaseModel):
    income_count: int = 0
    income_amount: str = "0.00"
    expense_count: int = 0
    expense_amount: str = "0.00"
    pure_goods_count: int = 0
    date_precision_counts: dict[str, int] = Field(default_factory=dict)


class LegacyImportContactSummary(BaseModel):
    name: str
    location: str | None
    remark: str | None
    relationship_type: str


class LegacyImportDictionarySummary(BaseModel):
    category: DictionaryCategory
    name: str


class LegacyImportPreviewResponse(BaseModel):
    preview_token: str | None
    expires_at: datetime | None
    total_rows: int
    valid_rows: int
    error_rows: int
    errors: list[LegacyImportIssueResponse]
    errors_truncated: bool
    new_contacts: list[LegacyImportContactSummary]
    reused_contact_count: int
    new_dictionary_items: list[LegacyImportDictionarySummary]
    new_event_count: int
    reused_event_count: int
    new_gift_count: int
    can_import: bool = False
    issues: list[LegacyImportIssueResponse] = Field(default_factory=list)
    rows: list[LegacyImportRowSummary] = Field(default_factory=list)
    summary: LegacyImportTotals = Field(default_factory=LegacyImportTotals)
    completed_date_event_count: int = 0


class LegacyImportConfirmResponse(BaseModel):
    imported_contacts: int
    imported_dictionary_items: int
    imported_events: int
    imported_gifts: int
    reused_contacts: int
    reused_events: int
    completed_date_events: int = 0
    batch_id: str | None = None
    completed_at: datetime | None = None
    summary: LegacyImportTotals = Field(default_factory=LegacyImportTotals)


class LegacyImportBatchListResponse(BaseModel):
    batches: list[LegacyImportConfirmResponse]


__all__ = [
    "LegacyImportConfirmResponse",
    "LegacyImportContactSummary",
    "LegacyImportDictionarySummary",
    "LegacyImportIssueResponse",
    "LegacyImportPreviewResponse",
]
