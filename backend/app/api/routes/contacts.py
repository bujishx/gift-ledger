from fastapi import APIRouter, Query, Response, status

from app.api.dependencies import (
    CompletedProfileDependency,
    CsrfProtectionDependency,
    CurrentUserDependency,
    DatabaseSessionDependency,
)
from app.api.errors import ApiError, ErrorResponse
from app.schemas.contacts import (
    ContactCreateRequest,
    ContactListResponse,
    ContactNameMatchesResponse,
    ContactResponse,
    ContactUpdateRequest,
)
from app.services.contacts import (
    ContactDistinguishingInfoRequiredError,
    ContactIdentityConflictError,
    ContactIdentityHashCollisionError,
    ContactInUseError,
    ContactNotFoundError,
    ContactRelationshipUnavailableError,
    ContactValidationError,
    SelfContactProtectedError,
    contact_response,
    create_contact,
    delete_contact,
    get_contact,
    list_contact_name_matches,
    list_contacts,
    set_contact_active,
    update_contact,
)

router = APIRouter(prefix="/contacts", tags=["联系人管理"])

ERROR_RESPONSES = {
    401: {"model": ErrorResponse},
    403: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
}


def _translate_contact_error(exc: Exception) -> ApiError:
    if isinstance(exc, ContactValidationError):
        return ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, exc.code, exc.message)
    if isinstance(exc, ContactNotFoundError):
        return ApiError(
            status.HTTP_404_NOT_FOUND,
            "CONTACT_NOT_FOUND",
            "未找到该联系人",
        )
    if isinstance(exc, SelfContactProtectedError):
        return ApiError(
            status.HTTP_403_FORBIDDEN,
            "SELF_CONTACT_PROTECTED",
            "本人资料受保护，请通过本人信息功能管理",
        )
    if isinstance(exc, ContactRelationshipUnavailableError):
        return ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "CONTACT_RELATIONSHIP_UNAVAILABLE",
            "所选关系类型不存在、已停用或不属于当前用户",
        )
    if isinstance(exc, ContactInUseError):
        return ApiError(
            status.HTTP_409_CONFLICT,
            "CONTACT_IN_USE",
            "该联系人已有事件或流水，不能删除，可以将其停用",
        )
    if isinstance(exc, ContactIdentityConflictError):
        contact = exc.contact
        code = "CONTACT_IDENTITY_EXISTS" if contact.is_active else "CONTACT_IDENTITY_INACTIVE"
        message = (
            "已有完全相同的联系人，请直接选择已有联系人"
            if contact.is_active
            else "已有完全相同但已停用的联系人，请先重新启用"
        )
        return ApiError(
            status.HTTP_409_CONFLICT,
            code,
            message,
            details={"existing_contact_id": contact.id},
        )
    if isinstance(exc, ContactDistinguishingInfoRequiredError):
        return ApiError(
            status.HTTP_409_CONFLICT,
            "CONTACT_DISTINGUISHING_INFO_REQUIRED",
            "存在同名联系人，请填写不同的联系人备注加以区分",
            details={"name_match_count": exc.match_count},
        )
    if isinstance(exc, ContactIdentityHashCollisionError):
        return ApiError(
            status.HTTP_409_CONFLICT,
            "CONTACT_IDENTITY_HASH_COLLISION",
            "联系人身份校验发生异常，请联系管理员并提供请求编号",
        )
    raise exc


def _read_contact_response(
    session: DatabaseSessionDependency,
    user_id: int,
    contact_id: int,
) -> ContactResponse:
    view = get_contact(session, user_id, contact_id)
    if view is None:
        raise _translate_contact_error(ContactNotFoundError())
    return contact_response(view)


@router.get(
    "",
    response_model=ContactListResponse,
    summary="读取当前用户的联系人列表",
    responses=ERROR_RESPONSES,
)
def list_contact_items(
    current_user: CurrentUserDependency,
    _profile: CompletedProfileDependency,
    session: DatabaseSessionDependency,
    search: str | None = Query(default=None, max_length=50),
    relationship_type_id: int | None = Query(default=None, gt=0),
    is_active: bool | None = None,
    include_inactive: bool = False,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> ContactListResponse:
    effective_is_active = is_active
    if is_active is None and not include_inactive:
        effective_is_active = True
    page = list_contacts(
        session,
        current_user.id,
        search=search,
        relationship_type_id=relationship_type_id,
        is_active=effective_is_active,
        limit=limit,
        offset=offset,
    )
    return ContactListResponse(
        items=[contact_response(item) for item in page.items],
        total=page.total,
        limit=page.limit,
        offset=page.offset,
    )


@router.get(
    "/name-matches",
    response_model=ContactNameMatchesResponse,
    summary="按身份规则查找当前用户的同名联系人",
    responses=ERROR_RESPONSES,
)
def list_name_matches(
    current_user: CurrentUserDependency,
    _profile: CompletedProfileDependency,
    session: DatabaseSessionDependency,
    name: str = Query(min_length=1, max_length=50),
) -> ContactNameMatchesResponse:
    try:
        matches = list_contact_name_matches(session, current_user.id, name)
    except ContactValidationError as exc:
        raise _translate_contact_error(exc) from exc
    return ContactNameMatchesResponse(items=[contact_response(item) for item in matches])


@router.post(
    "",
    response_model=ContactResponse,
    status_code=status.HTTP_201_CREATED,
    summary="新增当前用户的普通联系人",
    responses=ERROR_RESPONSES,
)
def create_contact_item(
    payload: ContactCreateRequest,
    current_user: CurrentUserDependency,
    _profile: CompletedProfileDependency,
    session: DatabaseSessionDependency,
    _csrf: CsrfProtectionDependency,
) -> ContactResponse:
    try:
        contact = create_contact(
            session,
            current_user.id,
            name=payload.name,
            relationship_type_id=payload.relationship_type_id,
            location=payload.location,
            phone=payload.phone,
            remark=payload.remark,
        )
    except (
        ContactDistinguishingInfoRequiredError,
        ContactIdentityConflictError,
        ContactIdentityHashCollisionError,
        ContactRelationshipUnavailableError,
        ContactValidationError,
        SelfContactProtectedError,
    ) as exc:
        raise _translate_contact_error(exc) from exc
    return _read_contact_response(session, current_user.id, contact.id)


@router.get(
    "/{contact_id}",
    response_model=ContactResponse,
    summary="读取当前用户的单个联系人",
    responses=ERROR_RESPONSES,
)
def get_contact_item(
    contact_id: int,
    current_user: CurrentUserDependency,
    _profile: CompletedProfileDependency,
    session: DatabaseSessionDependency,
) -> ContactResponse:
    return _read_contact_response(session, current_user.id, contact_id)


@router.patch(
    "/{contact_id}",
    response_model=ContactResponse,
    summary="更正当前用户的普通联系人",
    responses=ERROR_RESPONSES,
)
def update_contact_item(
    contact_id: int,
    payload: ContactUpdateRequest,
    current_user: CurrentUserDependency,
    _profile: CompletedProfileDependency,
    session: DatabaseSessionDependency,
    _csrf: CsrfProtectionDependency,
) -> ContactResponse:
    try:
        contact = update_contact(
            session,
            current_user.id,
            contact_id,
            name=payload.name,
            relationship_type_id=payload.relationship_type_id,
            location=payload.location,
            phone=payload.phone,
            remark=payload.remark,
            fields=payload.model_fields_set,
        )
    except (
        ContactDistinguishingInfoRequiredError,
        ContactIdentityConflictError,
        ContactIdentityHashCollisionError,
        ContactNotFoundError,
        ContactRelationshipUnavailableError,
        ContactValidationError,
        SelfContactProtectedError,
    ) as exc:
        raise _translate_contact_error(exc) from exc
    return _read_contact_response(session, current_user.id, contact.id)


def _change_contact_active(
    contact_id: int,
    current_user: CurrentUserDependency,
    session: DatabaseSessionDependency,
    *,
    is_active: bool,
) -> ContactResponse:
    try:
        contact = set_contact_active(
            session,
            current_user.id,
            contact_id,
            is_active=is_active,
        )
    except (ContactNotFoundError, SelfContactProtectedError) as exc:
        raise _translate_contact_error(exc) from exc
    return _read_contact_response(session, current_user.id, contact.id)


@router.post(
    "/{contact_id}/activate",
    response_model=ContactResponse,
    summary="重新启用当前用户的普通联系人",
    responses=ERROR_RESPONSES,
)
def activate_contact_item(
    contact_id: int,
    current_user: CurrentUserDependency,
    _profile: CompletedProfileDependency,
    session: DatabaseSessionDependency,
    _csrf: CsrfProtectionDependency,
) -> ContactResponse:
    return _change_contact_active(
        contact_id,
        current_user,
        session,
        is_active=True,
    )


@router.post(
    "/{contact_id}/deactivate",
    response_model=ContactResponse,
    summary="停用当前用户的普通联系人",
    responses=ERROR_RESPONSES,
)
def deactivate_contact_item(
    contact_id: int,
    current_user: CurrentUserDependency,
    _profile: CompletedProfileDependency,
    session: DatabaseSessionDependency,
    _csrf: CsrfProtectionDependency,
) -> ContactResponse:
    return _change_contact_active(
        contact_id,
        current_user,
        session,
        is_active=False,
    )


@router.delete(
    "/{contact_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除未被使用的普通联系人",
    responses=ERROR_RESPONSES,
)
def delete_contact_item(
    contact_id: int,
    current_user: CurrentUserDependency,
    _profile: CompletedProfileDependency,
    session: DatabaseSessionDependency,
    _csrf: CsrfProtectionDependency,
) -> Response:
    try:
        delete_contact(session, current_user.id, contact_id)
    except (
        ContactInUseError,
        ContactNotFoundError,
        SelfContactProtectedError,
    ) as exc:
        raise _translate_contact_error(exc) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
