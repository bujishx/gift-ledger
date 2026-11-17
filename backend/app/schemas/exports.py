from enum import StrEnum
from typing import Annotated

from pydantic import Field, field_validator, model_validator

from app.schemas.ledger_queries import InteractionStatus, PeopleQueryFilters


class QueryExportView(StrEnum):
    PEOPLE = "people"
    EVENTS = "events"
    GIFTS = "gifts"


class QueryExportScope(StrEnum):
    PAGE = "page"
    ALL = "all"


class QueryExportFilters(PeopleQueryFilters):
    view: QueryExportView
    scope: QueryExportScope = QueryExportScope.PAGE
    date_basis: str = Field(default="event", pattern="^(event|registered)$")
    relationship_type_ids: list[Annotated[int, Field(gt=0)]] | None = None
    kinship_type_ids: list[Annotated[int, Field(gt=0)]] | None = None
    event_type_ids: list[Annotated[int, Field(gt=0)]] | None = None
    counterparty_relationship_type_ids: list[Annotated[int, Field(gt=0)]] | None = None
    statistics_event_type_ids: list[Annotated[int, Field(gt=0)]] | None = None
    interaction_statuses: list[InteractionStatus] | None = None
    event_id: int | None = Field(default=None, gt=0)

    @field_validator("relationship_type_ids", "kinship_type_ids", "event_type_ids",
                     "counterparty_relationship_type_ids", "statistics_event_type_ids",
                     "interaction_statuses")
    @classmethod
    def unique_options(cls, value):
        return list(dict.fromkeys(value)) if value else None

    @model_validator(mode="after")
    def validate_view_sort(self) -> "QueryExportFilters":
        if self.view != QueryExportView.PEOPLE and self.sort_by is not None:
            raise ValueError("只有按人汇总导出支持 sort_by 排序")
        if self.view != QueryExportView.GIFTS and (
            self.date_basis != "event" or self.event_id is not None or any(
                getattr(self, key) for key in (
                    "relationship_type_ids", "kinship_type_ids", "event_type_ids",
                    "counterparty_relationship_type_ids", "statistics_event_type_ids",
                    "interaction_statuses",
                )
            )
        ):
            raise ValueError("多选统计条件和登记日期口径仅支持流水明细导出")
        return self


__all__ = ["QueryExportFilters", "QueryExportScope", "QueryExportView"]
