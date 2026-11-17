from fastapi import APIRouter, Response, status

from app.api.dependencies import (
    CompletedProfileDependency,
    CsrfProtectionDependency,
    CurrentUserDependency,
    DatabaseSessionDependency,
)
from app.api.errors import ApiError, ErrorResponse
from app.schemas.dictionaries import (
    DictionaryCategory,
    DictionaryCreateRequest,
    DictionaryItemResponse,
    DictionaryListResponse,
    DictionaryRenameRequest,
)
from app.services.dictionaries import (
    DictionaryItemInUseError,
    DictionaryItemNotFoundError,
    DictionaryNameConflictError,
    DictionaryValidationError,
    SystemDictionaryItemReadOnlyError,
    create_dictionary_item,
    delete_dictionary_item,
    dictionary_item_response,
    list_dictionary_items,
    rename_dictionary_item,
    set_dictionary_item_active,
)

router = APIRouter(prefix="/dictionaries", tags=["字典管理"])

ERROR_RESPONSES = {
    401: {"model": ErrorResponse},
    403: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
}


def _translate_dictionary_error(exc: Exception) -> ApiError:
    if isinstance(exc, DictionaryValidationError):
        return ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, exc.code, exc.message)
    if isinstance(exc, DictionaryItemNotFoundError):
        return ApiError(
            status.HTTP_404_NOT_FOUND,
            "DICTIONARY_ITEM_NOT_FOUND",
            "未找到该字典项",
        )
    if isinstance(exc, SystemDictionaryItemReadOnlyError):
        return ApiError(
            status.HTTP_403_FORBIDDEN,
            "SYSTEM_DICTIONARY_READ_ONLY",
            "系统内置项不能修改、停用或删除",
        )
    if isinstance(exc, DictionaryNameConflictError):
        return ApiError(
            status.HTTP_409_CONFLICT,
            "DICTIONARY_NAME_ALREADY_EXISTS",
            "该名称已经存在，请换一个名称",
        )
    if isinstance(exc, DictionaryItemInUseError):
        return ApiError(
            status.HTTP_409_CONFLICT,
            "DICTIONARY_ITEM_IN_USE",
            "该字典项已经被使用，不能删除，可以将其停用",
        )
    raise exc


@router.get(
    "/{category}",
    response_model=DictionaryListResponse,
    summary="读取当前用户可见的字典",
    responses=ERROR_RESPONSES,
)
def list_items(
    category: DictionaryCategory,
    current_user: CurrentUserDependency,
    _profile: CompletedProfileDependency,
    session: DatabaseSessionDependency,
    include_inactive: bool = False,
) -> DictionaryListResponse:
    items = list_dictionary_items(
        session,
        category,
        current_user.id,
        include_inactive=include_inactive,
    )
    return DictionaryListResponse(
        items=[dictionary_item_response(category, item) for item in items]
    )


@router.post(
    "/{category}",
    response_model=DictionaryItemResponse,
    status_code=status.HTTP_201_CREATED,
    summary="新增当前用户的自定义字典项",
    responses=ERROR_RESPONSES,
)
def create_item(
    category: DictionaryCategory,
    payload: DictionaryCreateRequest,
    current_user: CurrentUserDependency,
    _profile: CompletedProfileDependency,
    session: DatabaseSessionDependency,
    _csrf: CsrfProtectionDependency,
) -> DictionaryItemResponse:
    try:
        item = create_dictionary_item(session, category, current_user.id, payload.name)
    except (
        DictionaryNameConflictError,
        DictionaryValidationError,
    ) as exc:
        raise _translate_dictionary_error(exc) from exc
    return dictionary_item_response(category, item)


@router.patch(
    "/{category}/{item_id}",
    response_model=DictionaryItemResponse,
    summary="更正当前用户的自定义字典项名称",
    responses=ERROR_RESPONSES,
)
def rename_item(
    category: DictionaryCategory,
    item_id: int,
    payload: DictionaryRenameRequest,
    current_user: CurrentUserDependency,
    _profile: CompletedProfileDependency,
    session: DatabaseSessionDependency,
    _csrf: CsrfProtectionDependency,
) -> DictionaryItemResponse:
    try:
        item = rename_dictionary_item(
            session,
            category,
            current_user.id,
            item_id,
            payload.name,
        )
    except (
        DictionaryItemNotFoundError,
        DictionaryNameConflictError,
        DictionaryValidationError,
        SystemDictionaryItemReadOnlyError,
    ) as exc:
        raise _translate_dictionary_error(exc) from exc
    return dictionary_item_response(category, item)


def _change_item_active(
    category: DictionaryCategory,
    item_id: int,
    current_user: CurrentUserDependency,
    session: DatabaseSessionDependency,
    *,
    is_active: bool,
) -> DictionaryItemResponse:
    try:
        item = set_dictionary_item_active(
            session,
            category,
            current_user.id,
            item_id,
            is_active=is_active,
        )
    except (
        DictionaryItemNotFoundError,
        SystemDictionaryItemReadOnlyError,
    ) as exc:
        raise _translate_dictionary_error(exc) from exc
    return dictionary_item_response(category, item)


@router.post(
    "/{category}/{item_id}/activate",
    response_model=DictionaryItemResponse,
    summary="重新启用当前用户的自定义字典项",
    responses=ERROR_RESPONSES,
)
def activate_item(
    category: DictionaryCategory,
    item_id: int,
    current_user: CurrentUserDependency,
    _profile: CompletedProfileDependency,
    session: DatabaseSessionDependency,
    _csrf: CsrfProtectionDependency,
) -> DictionaryItemResponse:
    return _change_item_active(
        category,
        item_id,
        current_user,
        session,
        is_active=True,
    )


@router.post(
    "/{category}/{item_id}/deactivate",
    response_model=DictionaryItemResponse,
    summary="停用当前用户的自定义字典项",
    responses=ERROR_RESPONSES,
)
def deactivate_item(
    category: DictionaryCategory,
    item_id: int,
    current_user: CurrentUserDependency,
    _profile: CompletedProfileDependency,
    session: DatabaseSessionDependency,
    _csrf: CsrfProtectionDependency,
) -> DictionaryItemResponse:
    return _change_item_active(
        category,
        item_id,
        current_user,
        session,
        is_active=False,
    )


@router.delete(
    "/{category}/{item_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除未被使用的自定义字典项",
    responses=ERROR_RESPONSES,
)
def delete_item(
    category: DictionaryCategory,
    item_id: int,
    current_user: CurrentUserDependency,
    _profile: CompletedProfileDependency,
    session: DatabaseSessionDependency,
    _csrf: CsrfProtectionDependency,
) -> Response:
    try:
        delete_dictionary_item(session, category, current_user.id, item_id)
    except (
        DictionaryItemInUseError,
        DictionaryItemNotFoundError,
        SystemDictionaryItemReadOnlyError,
    ) as exc:
        raise _translate_dictionary_error(exc) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
