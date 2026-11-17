"""与记账列表共用筛选口径的统计请求。"""

from datetime import date

from pydantic import Field, model_validator

from app.schemas.ledger_queries import GiftsQueryFilters


class DashboardFilters(GiftsQueryFilters):
    comparison_from: date | None = Field(default=None, ge=date(1000, 1, 1))
    comparison_to: date | None = Field(default=None, ge=date(1000, 1, 1))
    grain: str = Field(default="auto", pattern="^(auto|day|month|quarter|year)$")

    @model_validator(mode="after")
    def comparison_range(self):
        if (self.comparison_from is None) != (self.comparison_to is None):
            raise ValueError("对比起止日期需要同时填写")
        if self.comparison_from and self.comparison_to < self.comparison_from:
            raise ValueError("对比结束日期不能早于开始日期")
        return self
