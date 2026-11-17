from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Query, Response, status

from app.api.dependencies import (
    CompletedProfileDependency,
    CsrfProtectionDependency,
    CurrentUserDependency,
    DatabaseSessionDependency,
    GiftChangeReauthDependency,
)
from app.api.errors import ApiError, ErrorResponse
from app.schemas.ledger import (
    EventCreateRequest,
    EventDetailResponse,
    EventGiftsCreateRequest,
    EventIdentityCheckRequest,
    EventIdentityCheckResponse,
    EventListResponse,
    EventUpdateRequest,
    GiftDetailResponse,
    GiftUpdateRequest,
    LedgerEntryCreateRequest,
    LedgerEntryResponse,
)
from app.services.event_registration import create_event, create_event_gifts, list_events
from app.services.ledger import (
    EventAlreadyExistsError,
    GiftAlreadyExistsError,
    LedgerValidationError,
    create_ledger_entry,
    find_event_by_identity,
)
from app.services.ledger_records import (
    EventHasGiftsError,
    EventIdentityConflictError,
    EventNotFoundError,
    GiftNotFoundError,
    delete_event,
    delete_gift,
    event_detail_response,
    get_event_detail,
    get_gift_detail,
    gift_detail_response,
    update_event,
    update_gift,
)

router = APIRouter(tags=["事件与流水"])

ERROR_RESPONSES = {
    401: {"model": ErrorResponse},
    403: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
}


def _translate_ledger_error(exc: Exception) -> ApiError:
    if isinstance(exc, LedgerValidationError):
        if exc.code == "ACCOUNT_UNAVAILABLE":
            return ApiError(
                status.HTTP_401_UNAUTHORIZED,
                exc.code,
                exc.message,
                {"WWW-Authenticate": "Cookie"},
            )
        if exc.code == "SELF_CONTACT_REQUIRED":
            return ApiError(status.HTTP_403_FORBIDDEN, exc.code, exc.message)
        return ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, exc.code, exc.message)
    if isinstance(exc, EventNotFoundError):
        return ApiError(status.HTTP_404_NOT_FOUND, "EVENT_NOT_FOUND", "未找到该事件")
    if isinstance(exc, GiftNotFoundError):
        return ApiError(status.HTTP_404_NOT_FOUND, "GIFT_NOT_FOUND", "未找到该流水")
    if isinstance(exc, EventHasGiftsError):
        return ApiError(
            status.HTTP_409_CONFLICT,
            "EVENT_HAS_GIFTS",
            "该事件已有流水，不能更换事主或删除，请先逐笔处理流水",
        )
    if isinstance(exc, (EventIdentityConflictError, EventAlreadyExistsError)):
        return ApiError(
            status.HTTP_409_CONFLICT,
            "EVENT_ALREADY_EXISTS",
            str(exc),
            details={"existing_event_id": exc.event_id},
        )
    if isinstance(exc, GiftAlreadyExistsError):
        return ApiError(
            status.HTTP_409_CONFLICT,
            "GIFT_ALREADY_EXISTS",
            "该随礼人在这个事件中已有流水，请编辑原流水",
            details={"existing_gift_id": exc.gift_id},
        )
    raise exc


def _read_event_detail(
    session: DatabaseSessionDependency,
    user_id: int,
    event_id: int,
) -> EventDetailResponse:
    try:
        return event_detail_response(get_event_detail(session, user_id, event_id))
    except (EventNotFoundError, LedgerValidationError) as exc:
        raise _translate_ledger_error(exc) from exc


def _read_gift_detail(
    session: DatabaseSessionDependency,
    user_id: int,
    gift_id: int,
) -> GiftDetailResponse:
    try:
        return gift_detail_response(get_gift_detail(session, user_id, gift_id))
    except (EventNotFoundError, GiftNotFoundError, LedgerValidationError) as exc:
        raise _translate_ledger_error(exc) from exc


@router.post(
    "/events",
    response_model=EventDetailResponse,
    status_code=status.HTTP_201_CREATED,
    summary="先独立登记办事事件，可暂不记流水",
    responses=ERROR_RESPONSES,
)
def register_event(
    payload: EventCreateRequest,
    current_user: CurrentUserDependency,
    _profile: CompletedProfileDependency,
    session: DatabaseSessionDependency,
    _csrf: CsrfProtectionDependency,
) -> EventDetailResponse:
    try:
        event = create_event(
            session,
            current_user.id,
            host_contact_id=payload.host_contact_id,
            kinship_type_id=payload.kinship_type_id,
            event_type_id=payload.event_type_id,
            event_date=payload.event_date,
            remark=payload.remark,
        )
    except (EventAlreadyExistsError, LedgerValidationError) as exc:
        raise _translate_ledger_error(exc) from exc
    return _read_event_detail(session, current_user.id, event.id)


@router.post(
    "/events/check-identity",
    response_model=EventIdentityCheckResponse,
    summary="只读检测事主、亲属称谓、事件类型和备注是否已有事件",
    responses=ERROR_RESPONSES,
)
def check_event_identity(
    payload: EventIdentityCheckRequest,
    current_user: CurrentUserDependency,
    _profile: CompletedProfileDependency,
    session: DatabaseSessionDependency,
    _csrf: CsrfProtectionDependency,
) -> EventIdentityCheckResponse:
    try:
        event = find_event_by_identity(
            session,
            current_user.id,
            host_contact_id=payload.host_contact_id,
            kinship_type_id=payload.kinship_type_id,
            event_type_id=payload.event_type_id,
            event_remark=payload.remark,
            exclude_event_id=payload.exclude_event_id,
        )
    except LedgerValidationError as exc:
        raise _translate_ledger_error(exc) from exc
    return EventIdentityCheckResponse(
        existing_event=(
            _read_event_detail(session, current_user.id, event.id) if event is not None else None
        )
    )


@router.get(
    "/events",
    response_model=EventListResponse,
    summary="查事，包括尚无流水的事件",
    responses=ERROR_RESPONSES,
)
def search_events(
    current_user: CurrentUserDependency,
    _profile: CompletedProfileDependency,
    session: DatabaseSessionDependency,
    keyword: str | None = Query(default=None, max_length=100),
    relationship_type_ids: Annotated[list[int] | None, Query()] = None,
    kinship_type_ids: Annotated[list[int] | None, Query()] = None,
    event_type_ids: Annotated[list[int] | None, Query()] = None,
    host_contact_id: int | None = Query(default=None, gt=0),
    date_from: date | None = None,
    date_to: date | None = None,
    sort_by: Literal[
        "registered_at", "event_date", "host_name", "gift_count", "cash_amount"
    ] = "registered_at",
    sort_order: Literal["asc", "desc"] = "desc",
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> EventListResponse:
    if date_from and date_to and date_to < date_from:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "DATE_RANGE_INVALID", "结束日期不能早于开始日期"
        )
    try:
        return list_events(
            session,
            current_user.id,
            keyword=keyword,
            relationship_type_ids=relationship_type_ids,
            kinship_type_ids=kinship_type_ids,
            event_type_ids=event_type_ids,
            host_contact_id=host_contact_id,
            date_from=date_from,
            date_to=date_to,
            sort_by=sort_by,
            sort_order=sort_order,
            limit=limit,
            offset=offset,
        )
    except LedgerValidationError as exc:
        raise _translate_ledger_error(exc) from exc


@router.post(
    "/events/{event_id}/gifts",
    response_model=EventDetailResponse,
    status_code=status.HTTP_201_CREATED,
    summary="为已有事件原子登记一至五位随礼人的流水",
    responses=ERROR_RESPONSES,
)
def register_event_gifts(
    event_id: int,
    payload: EventGiftsCreateRequest,
    current_user: CurrentUserDependency,
    _profile: CompletedProfileDependency,
    session: DatabaseSessionDependency,
    _csrf: CsrfProtectionDependency,
) -> EventDetailResponse:
    try:
        create_event_gifts(session, current_user.id, event_id, payload.gifts)
    except (GiftAlreadyExistsError, EventNotFoundError, LedgerValidationError) as exc:
        raise _translate_ledger_error(exc) from exc
    return _read_event_detail(session, current_user.id, event_id)


@router.post(
    "/ledger/entries",
    response_model=LedgerEntryResponse,
    status_code=status.HTTP_201_CREATED,
    summary="原子登记一笔礼金或礼物流水",
    responses=ERROR_RESPONSES,
)
def create_entry(
    payload: LedgerEntryCreateRequest,
    current_user: CurrentUserDependency,
    _profile: CompletedProfileDependency,
    session: DatabaseSessionDependency,
    _csrf: CsrfProtectionDependency,
) -> LedgerEntryResponse:
    try:
        result = create_ledger_entry(
            session,
            current_user.id,
            host_contact_id=payload.host_contact_id,
            kinship_type_id=payload.kinship_type_id,
            event_type_id=payload.event_type_id,
            event_date=payload.event_date,
            event_remark=payload.event_remark,
            giver_contact_id=payload.giver_contact_id,
            amount=payload.amount,
            gift_details=payload.gift_details,
            gift_remark=payload.gift_remark,
        )
    except (GiftAlreadyExistsError, LedgerValidationError) as exc:
        raise _translate_ledger_error(exc) from exc

    detail = _read_gift_detail(session, current_user.id, result.gift.id)
    return LedgerEntryResponse(
        event=detail.event,
        gift=detail.gift,
        event_reused=result.event_reused,
    )


@router.get(
    "/events/{event_id}",
    response_model=EventDetailResponse,
    summary="读取当前用户的事件及全部流水",
    responses=ERROR_RESPONSES,
)
def read_event(
    event_id: int,
    current_user: CurrentUserDependency,
    _profile: CompletedProfileDependency,
    session: DatabaseSessionDependency,
) -> EventDetailResponse:
    return _read_event_detail(session, current_user.id, event_id)


@router.patch(
    "/events/{event_id}",
    response_model=EventDetailResponse,
    summary="更正当前用户的事件",
    responses=ERROR_RESPONSES,
)
def correct_event(
    event_id: int,
    payload: EventUpdateRequest,
    current_user: CurrentUserDependency,
    _profile: CompletedProfileDependency,
    session: DatabaseSessionDependency,
    _csrf: CsrfProtectionDependency,
) -> EventDetailResponse:
    try:
        update_event(
            session,
            current_user.id,
            event_id,
            host_contact_id=payload.host_contact_id,
            kinship_type_id=payload.kinship_type_id,
            event_type_id=payload.event_type_id,
            event_date=payload.event_date,
            remark=payload.remark,
            fields=payload.model_fields_set,
        )
    except (
        EventHasGiftsError,
        EventIdentityConflictError,
        EventNotFoundError,
        LedgerValidationError,
    ) as exc:
        raise _translate_ledger_error(exc) from exc
    return _read_event_detail(session, current_user.id, event_id)


@router.delete(
    "/events/{event_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除当前用户的空事件",
    responses=ERROR_RESPONSES,
)
def remove_event(
    event_id: int,
    current_user: CurrentUserDependency,
    _profile: CompletedProfileDependency,
    session: DatabaseSessionDependency,
    _csrf: CsrfProtectionDependency,
) -> Response:
    try:
        delete_event(session, current_user.id, event_id)
    except (EventHasGiftsError, EventNotFoundError, LedgerValidationError) as exc:
        raise _translate_ledger_error(exc) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/gifts/{gift_id}",
    response_model=GiftDetailResponse,
    summary="读取当前用户的单笔流水",
    responses=ERROR_RESPONSES,
)
def read_gift(
    gift_id: int,
    current_user: CurrentUserDependency,
    _profile: CompletedProfileDependency,
    session: DatabaseSessionDependency,
) -> GiftDetailResponse:
    return _read_gift_detail(session, current_user.id, gift_id)


@router.patch(
    "/gifts/{gift_id}",
    response_model=GiftDetailResponse,
    summary="更正当前用户的单笔流水",
    responses=ERROR_RESPONSES,
)
def correct_gift(
    gift_id: int,
    payload: GiftUpdateRequest,
    current_user: CurrentUserDependency,
    _profile: CompletedProfileDependency,
    session: DatabaseSessionDependency,
    _csrf: CsrfProtectionDependency,
    _reauth: GiftChangeReauthDependency,
) -> GiftDetailResponse:
    try:
        update_gift(
            session,
            current_user.id,
            gift_id,
            giver_contact_id=payload.giver_contact_id,
            amount=payload.amount,
            gift_details=payload.gift_details,
            remark=payload.remark,
            fields=payload.model_fields_set,
        )
    except (GiftAlreadyExistsError, GiftNotFoundError, LedgerValidationError) as exc:
        raise _translate_ledger_error(exc) from exc
    return _read_gift_detail(session, current_user.id, gift_id)


@router.delete(
    "/gifts/{gift_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除当前用户的单笔流水",
    responses=ERROR_RESPONSES,
)
def remove_gift(
    gift_id: int,
    current_user: CurrentUserDependency,
    _profile: CompletedProfileDependency,
    session: DatabaseSessionDependency,
    _csrf: CsrfProtectionDependency,
    _reauth: GiftChangeReauthDependency,
) -> Response:
    try:
        delete_gift(session, current_user.id, gift_id)
    except (GiftNotFoundError, LedgerValidationError) as exc:
        raise _translate_ledger_error(exc) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
