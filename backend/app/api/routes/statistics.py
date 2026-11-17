"""四类统计的只读路由。

沿用现有登录与本人建档门禁和中文错误结构。
"""

from datetime import date
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Query, status
from fastapi.encoders import jsonable_encoder

from app.api.dependencies import (
    CompletedProfileDependency,
    CurrentUserDependency,
    DatabaseSessionDependency,
)
from app.api.errors import ApiError, ErrorResponse
from app.schemas.statistics import (
    EventDistributionResponse,
    ReciprocityResponse,
    RelationshipCircleResponse,
    StatisticsQueryParameters,
    StatisticsYearQueryParameters,
    TimelineResponse,
)
from app.schemas.statistics_dashboard import DashboardFilters
from app.services.ledger_query_common import LedgerQueryValidationError
from app.services.statistics import (
    StatisticsValidationError,
    event_distribution,
    reciprocity_summary,
    relationship_circles,
    timeline_statistics,
)
from app.services.statistics_dashboard import dashboard_statistics

router = APIRouter(prefix="/statistics", tags=["统计"])


@router.get("/dashboard", summary="按时间段与条件统计收支、联系人、事件和关系")
def read_dashboard(
    filters: Annotated[DashboardFilters, Query()],
    current_user: CurrentUserDependency,
    _profile: CompletedProfileDependency,
    session: DatabaseSessionDependency,
):
    try:
        return jsonable_encoder(
            dashboard_statistics(session, current_user.id, filters), custom_encoder={Decimal: str}
        )
    except LedgerQueryValidationError as exc:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, exc.code, exc.message) from exc


ERROR_RESPONSES = {
    401: {"model": ErrorResponse},
    403: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
}


def _translate_statistics_error(exc: Exception) -> ApiError:
    if isinstance(exc, StatisticsValidationError):
        if exc.code == "SELF_CONTACT_REQUIRED":
            return ApiError(status.HTTP_403_FORBIDDEN, exc.code, exc.message)
        return ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, exc.code, exc.message)
    raise exc


@router.get(
    "/reciprocity",
    response_model=ReciprocityResponse,
    summary="按普通联系人汇总全部历史的收支金额、笔数与现金差额",
    responses=ERROR_RESPONSES,
)
def read_reciprocity(
    _filters: Annotated[StatisticsQueryParameters, Query()],
    current_user: CurrentUserDependency,
    _profile: CompletedProfileDependency,
    session: DatabaseSessionDependency,
) -> ReciprocityResponse:
    try:
        return reciprocity_summary(session, current_user.id)
    except StatisticsValidationError as exc:
        raise _translate_statistics_error(exc) from exc


@router.get(
    "/timeline",
    response_model=TimelineResponse,
    summary="统计所选年份的 1—12 月收支与年度合计",
    responses=ERROR_RESPONSES,
)
def read_timeline(
    filters: Annotated[StatisticsYearQueryParameters, Query()],
    current_user: CurrentUserDependency,
    _profile: CompletedProfileDependency,
    session: DatabaseSessionDependency,
) -> TimelineResponse:
    effective_year = filters.year if filters.year is not None else date.today().year
    try:
        return timeline_statistics(session, current_user.id, effective_year)
    except StatisticsValidationError as exc:
        raise _translate_statistics_error(exc) from exc


@router.get(
    "/event-distribution",
    response_model=EventDistributionResponse,
    summary="统计所选年份支出按事件类型的现金分布",
    responses=ERROR_RESPONSES,
)
def read_event_distribution(
    filters: Annotated[StatisticsYearQueryParameters, Query()],
    current_user: CurrentUserDependency,
    _profile: CompletedProfileDependency,
    session: DatabaseSessionDependency,
) -> EventDistributionResponse:
    effective_year = filters.year if filters.year is not None else date.today().year
    try:
        return event_distribution(session, current_user.id, effective_year)
    except StatisticsValidationError as exc:
        raise _translate_statistics_error(exc) from exc


@router.get(
    "/relationships",
    response_model=RelationshipCircleResponse,
    summary="按对方联系人当前关系汇总全部历史收支",
    responses=ERROR_RESPONSES,
)
def read_relationships(
    _filters: Annotated[StatisticsQueryParameters, Query()],
    current_user: CurrentUserDependency,
    _profile: CompletedProfileDependency,
    session: DatabaseSessionDependency,
) -> RelationshipCircleResponse:
    try:
        return relationship_circles(session, current_user.id)
    except StatisticsValidationError as exc:
        raise _translate_statistics_error(exc) from exc
