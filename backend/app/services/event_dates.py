"""Known event date components; missing legacy dates are never invented."""

import calendar
import math
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

DatePrecision = Literal["day", "month", "year", "unknown"]
DATE_PRECISIONS = ("day", "month", "year", "unknown")
DATE_SOURCES = ("web", "legacy_import")

# Shared by ORM metadata, incremental migration and the new-database SQL.
EVENT_DATE_CHECK = (
    "`date_source` IN ('web', 'legacy_import') AND "
    "(`date_source` = 'legacy_import' OR `date_precision` = 'day') AND ("
    "(`date_precision` = 'day' AND `event_date` IS NOT NULL "
    "AND `event_date` >= '1000-01-01' AND LAST_DAY(`event_date`) IS NOT NULL "
    "AND DAYOFMONTH(`event_date`) BETWEEN 1 AND DAYOFMONTH(LAST_DAY(`event_date`)) "
    "AND `event_year` IS NOT NULL AND `event_year` = YEAR(`event_date`) "
    "AND `event_month` IS NOT NULL AND `event_month` = MONTH(`event_date`)) OR "
    "(`date_precision` = 'month' AND `event_date` IS NULL "
    "AND `event_year` IS NOT NULL AND `event_year` BETWEEN 1000 AND 9999 "
    "AND `event_month` IS NOT NULL AND `event_month` BETWEEN 1 AND 12) OR "
    "(`date_precision` = 'year' AND `event_date` IS NULL "
    "AND `event_year` IS NOT NULL AND `event_year` BETWEEN 1000 AND 9999 "
    "AND `event_month` IS NULL) OR "
    "(`date_precision` = 'unknown' AND `event_date` IS NULL "
    "AND `event_year` IS NULL AND `event_month` IS NULL))"
)


@dataclass(frozen=True, slots=True)
class EventDateValue:
    event_date: date | None
    date_precision: DatePrecision
    event_year: int | None = None
    event_month: int | None = None

    @property
    def precision(self):
        return self.date_precision

    @property
    def year(self):
        return self.event_year

    @property
    def month(self):
        return self.event_month

    @property
    def day(self):
        return self.event_date

    def __post_init__(self):
        if self.date_precision == "day":
            if not isinstance(self.event_date, date) or isinstance(self.event_date, datetime):
                raise ValueError("完整日期必须有真实年月日")
            if self.event_date.year < 1000:
                raise ValueError("年份必须在1000到9999之间")
            for key, actual in (
                ("event_year", self.event_date.year),
                ("event_month", self.event_date.month),
            ):
                given = getattr(self, key)
                if given is not None and (type(given) is not int or given != actual):
                    raise ValueError("日期已知部分不一致")
                object.__setattr__(self, key, actual)
        elif self.date_precision in ("year", "month"):
            if (
                self.event_date is not None
                or type(self.event_year) is not int
                or not 1000 <= self.event_year <= 9999
            ):
                raise ValueError("旧账日期年份不正确")
            if self.date_precision == "month":
                if type(self.event_month) is not int or not 1 <= self.event_month <= 12:
                    raise ValueError("旧账日期月份不正确")
            elif self.event_month is not None:
                raise ValueError("仅年份不能包含月份")
        elif self.date_precision != "unknown" or any(
            value is not None for value in (self.event_date, self.event_year, self.event_month)
        ):
            raise ValueError("日期精度或组成不正确")


def parse_event_date(value: object) -> EventDateValue:
    if type(value) in (int, float):
        if math.isfinite(value) and value == int(value) and 1000 <= value <= 9999:
            return EventDateValue(None, "year", int(value))
        raise ValueError("数字年份必须为1000到9999的整数，不能猜测Excel日期序号")
    if isinstance(value, datetime):
        value = value.date()
    if isinstance(value, date):
        return EventDateValue(value, "day")
    if not isinstance(value, str):
        raise ValueError("办事日期须填写完整日期、年月、年份或不详")
    value = value.strip()
    if value == "不详":
        return EventDateValue(None, "unknown")
    if re.fullmatch(r"[1-9][0-9]{3}", value):
        return EventDateValue(None, "year", int(value))
    if re.fullmatch(r"[1-9][0-9]{3}-[0-9]{2}", value):
        return EventDateValue(None, "month", int(value[:4]), int(value[5:]))
    if re.fullmatch(r"[1-9][0-9]{3}-[0-9]{2}-[0-9]{2}", value):
        return EventDateValue(date.fromisoformat(value), "day")
    raise ValueError("办事日期只接受YYYY-MM-DD、YYYY-MM、YYYY或不详")


def date_value(record: object) -> EventDateValue:
    precision = getattr(record, "date_precision", None) or "day"
    return EventDateValue(
        getattr(record, "event_date", None),
        precision,
        getattr(record, "event_year", None),
        getattr(record, "event_month", None),
    )


def display_event_date(value: EventDateValue | object) -> str:
    if not isinstance(value, EventDateValue):
        value = date_value(value)
    if value.date_precision == "day":
        return value.event_date.isoformat()
    if value.date_precision == "month":
        return f"{value.event_year:04d}-{value.event_month:02d}"
    if value.date_precision == "year":
        return f"{value.event_year:04d}"
    return "不详"


def merge_event_dates(current: EventDateValue, incoming: EventDateValue) -> EventDateValue:
    """Keep the most complete compatible information, reject contradictory known parts."""
    if current.year is not None and incoming.year is not None and current.year != incoming.year:
        raise ValueError("同一事件的已知年份不一致")
    if current.month is not None and incoming.month is not None and current.month != incoming.month:
        raise ValueError("同一事件的已知月份不一致")
    if (
        current.event_date is not None
        and incoming.event_date is not None
        and current.event_date != incoming.event_date
    ):
        raise ValueError("同一事件的办事日期不一致")
    rank = {"unknown": 0, "year": 1, "month": 2, "day": 3}
    return incoming if rank[incoming.precision] > rank[current.precision] else current


def event_date_fields(value: EventDateValue, *, source: str = "legacy_import") -> dict:
    if source not in DATE_SOURCES or (source == "web" and value.precision != "day"):
        raise ValueError("普通网页录入必须完整办事日期")
    return {
        "event_date": value.event_date,
        "date_precision": value.precision,
        "event_year": value.year,
        "event_month": value.month,
        "date_source": source,
    }


def event_date_response_fields(record: object) -> dict:
    value = date_value(record)
    return {
        **event_date_fields(value, source=getattr(record, "date_source", None) or "web"),
        "event_date_display": display_event_date(value),
    }


def known_date_bounds(value: EventDateValue) -> tuple[date | None, date | None]:
    """Bounds used for determining period membership, never stored as event dates."""
    if value.precision == "unknown":
        return None, None
    if value.precision == "day":
        return value.event_date, value.event_date
    if value.precision == "year":
        return date(value.year, 1, 1), date(value.year, 12, 31)
    return date(value.year, value.month, 1), date(
        value.year, value.month, calendar.monthrange(value.year, value.month)[1]
    )


def date_period_conditions(date_from, date_to):
    """A partial date counts only when its entire possible range is inside the period."""
    from sqlalchemy import and_, func, or_

    from app.db.models import EventRecord

    if date_from is None and date_to is None:
        return []
    first = func.str_to_date(
        func.concat(EventRecord.event_year, "-", func.coalesce(EventRecord.event_month, 1), "-01"),
        "%Y-%m-%d",
    )
    last = func.last_day(
        func.str_to_date(
            func.concat(
                EventRecord.event_year, "-", func.coalesce(EventRecord.event_month, 12), "-01"
            ),
            "%Y-%m-%d",
        )
    )
    day = [EventRecord.date_precision == "day"]
    partial = [EventRecord.date_precision.in_(("month", "year"))]
    if date_from is not None:
        day.append(EventRecord.event_date >= date_from)
        partial.append(first >= date_from)
    if date_to is not None:
        day.append(EventRecord.event_date <= date_to)
        partial.append(last <= date_to)
    return [or_(and_(*day), and_(*partial))]


def date_period_uncertain_condition(date_from, date_to):
    """Only overlapping partial ranges are unresolved; known outside dates are excluded."""
    from sqlalchemy import and_, func, or_

    from app.db.models import EventRecord

    first = func.str_to_date(
        func.concat(EventRecord.event_year, "-", func.coalesce(EventRecord.event_month, 1), "-01"),
        "%Y-%m-%d",
    )
    last = func.last_day(
        func.str_to_date(
            func.concat(
                EventRecord.event_year, "-", func.coalesce(EventRecord.event_month, 12), "-01"
            ),
            "%Y-%m-%d",
        )
    )
    overlap = [EventRecord.date_precision.in_(("month", "year"))]
    if date_from is not None:
        overlap.append(last >= date_from)
    if date_to is not None:
        overlap.append(first <= date_to)
    return and_(
        or_(EventRecord.date_precision == "unknown", and_(*overlap)),
        ~date_period_conditions(date_from, date_to)[0],
    )


def date_sort_expression():
    """Order the known components without creating a made-up date."""
    from sqlalchemy import func

    from app.db.models import EventRecord

    return (
        func.coalesce(EventRecord.event_year, 0) * 10000
        + func.coalesce(EventRecord.event_month, 0) * 100
        + func.coalesce(func.dayofmonth(EventRecord.event_date), 0)
    )


# Short aliases for the import planner.
parse = parse_event_date
display = display_event_date
merge = merge_event_dates
date_fields = event_date_fields
