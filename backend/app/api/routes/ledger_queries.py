from typing import Annotated

from fastapi import APIRouter, Query, status

from app.api.dependencies import (
    CompletedProfileDependency,
    CurrentUserDependency,
    DatabaseSessionDependency,
)
from app.api.errors import ApiError, ErrorResponse
from app.schemas.ledger_queries import (
    EventsQueryResponse,
    GiftsQueryFilters,
    GiftsQueryResponse,
    LedgerQueryFilters,
    PeopleQueryFilters,
    PeopleQueryResponse,
)
from app.services.ledger_queries import (
    query_events as events_query,
)
from app.services.ledger_queries import (
    query_gifts as gifts_query,
)
from app.services.ledger_queries import (
    query_people as people_query,
)
from app.services.ledger_query_common import LedgerQueryValidationError

router = APIRouter(tags=["查账"])

QUERY_ERROR_RESPONSES = {
    401: {"model": ErrorResponse},
    403: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
}


def _translate_query_error(exc: LedgerQueryValidationError) -> ApiError:
    if exc.code == "SELF_CONTACT_REQUIRED":
        return ApiError(status.HTTP_403_FORBIDDEN, exc.code, exc.message)
    return ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, exc.code, exc.message)


@router.get(
    "/ledger/query/people",
    response_model=PeopleQueryResponse,
    summary="按普通联系人汇总查账结果",
    responses=QUERY_ERROR_RESPONSES,
)
def query_people(
    filters: Annotated[PeopleQueryFilters, Query()],
    current_user: CurrentUserDependency,
    _profile: CompletedProfileDependency,
    session: DatabaseSessionDependency,
) -> PeopleQueryResponse:
    try:
        return people_query(session, current_user.id, filters)
    except LedgerQueryValidationError as exc:
        raise _translate_query_error(exc) from exc


@router.get(
    "/ledger/query/events",
    response_model=EventsQueryResponse,
    summary="按事件聚合查账结果",
    responses=QUERY_ERROR_RESPONSES,
)
def query_events(
    filters: Annotated[LedgerQueryFilters, Query()],
    current_user: CurrentUserDependency,
    _profile: CompletedProfileDependency,
    session: DatabaseSessionDependency,
) -> EventsQueryResponse:
    try:
        return events_query(session, current_user.id, filters)
    except LedgerQueryValidationError as exc:
        raise _translate_query_error(exc) from exc


@router.get(
    "/ledger/query/gifts",
    response_model=GiftsQueryResponse,
    summary="逐笔返回查账流水明细",
    responses=QUERY_ERROR_RESPONSES,
)
def query_gifts(
    filters: Annotated[GiftsQueryFilters, Query()],
    current_user: CurrentUserDependency,
    _profile: CompletedProfileDependency,
    session: DatabaseSessionDependency,
) -> GiftsQueryResponse:
    try:
        return gifts_query(session, current_user.id, filters)
    except LedgerQueryValidationError as exc:
        raise _translate_query_error(exc) from exc
