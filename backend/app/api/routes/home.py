from datetime import date
from typing import Annotated

from fastapi import APIRouter, Query, status

from app.api.dependencies import (
    CompletedProfileDependency,
    CurrentUserDependency,
    DatabaseSessionDependency,
)
from app.api.errors import ApiError, ErrorResponse
from app.schemas.home import HomeSummaryResponse
from app.schemas.statistics import StatisticsYearQueryParameters
from app.services.home import HomeValidationError, home_summary

router = APIRouter(prefix="/home", tags=["首页"])

ERROR_RESPONSES = {
    401: {"model": ErrorResponse},
    403: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
}


@router.get(
    "/summary",
    response_model=HomeSummaryResponse,
    summary="读取首页年度汇总、回礼提醒、最近流水和常用人员",
    responses=ERROR_RESPONSES,
)
def read_home_summary(
    filters: Annotated[StatisticsYearQueryParameters, Query()],
    current_user: CurrentUserDependency,
    _profile: CompletedProfileDependency,
    session: DatabaseSessionDependency,
) -> HomeSummaryResponse:
    effective_year = filters.year if filters.year is not None else date.today().year
    try:
        return home_summary(session, current_user.id, effective_year)
    except HomeValidationError as exc:
        status_code = (
            status.HTTP_403_FORBIDDEN
            if exc.code == "SELF_CONTACT_REQUIRED"
            else status.HTTP_422_UNPROCESSABLE_CONTENT
        )
        raise ApiError(status_code, exc.code, exc.message) from exc
