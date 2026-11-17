from fastapi import APIRouter, status

from app.api.dependencies import (
    CompletedProfileDependency,
    CsrfProtectionDependency,
    CurrentUserDependency,
    DatabaseSessionDependency,
)
from app.api.errors import ApiError, ErrorResponse
from app.db.models import Contact
from app.schemas.profile import ProfileCreateRequest, ProfileResponse, ProfileUpdateRequest
from app.services.contacts import (
    ContactDistinguishingInfoRequiredError,
    ContactIdentityConflictError,
    ContactIdentityHashCollisionError,
)
from app.services.profile import (
    AccountUnavailableError,
    ProfileAlreadyExistsError,
    ProfileNotFoundError,
    ProfileValidationError,
    SelfRelationshipUnavailableError,
    create_self_profile,
    update_self_profile,
)

router = APIRouter(prefix="/me/profile", tags=["本人信息"])

ERROR_RESPONSES = {
    401: {"model": ErrorResponse},
    403: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
    503: {"model": ErrorResponse},
}


def _profile_response(profile: Contact) -> ProfileResponse:
    return ProfileResponse(
        id=profile.id,
        name=profile.name,
        location=profile.location,
    )


def _translate_profile_error(exc: Exception) -> ApiError:
    if isinstance(exc, ProfileValidationError):
        return ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, exc.code, exc.message)
    if isinstance(exc, ProfileAlreadyExistsError):
        return ApiError(
            status.HTTP_409_CONFLICT,
            "PROFILE_ALREADY_COMPLETED",
            "本人信息已经建立，请使用更正功能修改",
        )
    if isinstance(exc, ProfileNotFoundError):
        return ApiError(
            status.HTTP_403_FORBIDDEN,
            "PROFILE_REQUIRED",
            "请先完善本人信息",
        )
    if isinstance(exc, AccountUnavailableError):
        return ApiError(
            status.HTTP_401_UNAUTHORIZED,
            "INVALID_AUTHENTICATION",
            "登录状态无效或账号已停用，请重新登录",
            {"WWW-Authenticate": "Cookie"},
        )
    if isinstance(exc, SelfRelationshipUnavailableError):
        return ApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "SCHEMA_NOT_READY",
            "系统基础数据尚未准备完成",
        )
    if isinstance(exc, ContactIdentityConflictError):
        return ApiError(
            status.HTTP_409_CONFLICT,
            "CONTACT_IDENTITY_EXISTS",
            "本人资料与已有联系人完全相同，请先更正已有联系人资料",
            details={"existing_contact_id": exc.contact.id},
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


@router.post(
    "",
    response_model=ProfileResponse,
    status_code=status.HTTP_201_CREATED,
    summary="首次建立本人信息",
    responses=ERROR_RESPONSES,
)
def create_profile(
    payload: ProfileCreateRequest,
    current_user: CurrentUserDependency,
    session: DatabaseSessionDependency,
    _csrf: CsrfProtectionDependency,
) -> ProfileResponse:
    try:
        profile = create_self_profile(
            session,
            current_user.id,
            payload.name,
            payload.location,
        )
    except (
        AccountUnavailableError,
        ContactDistinguishingInfoRequiredError,
        ContactIdentityConflictError,
        ContactIdentityHashCollisionError,
        ProfileAlreadyExistsError,
        ProfileValidationError,
        SelfRelationshipUnavailableError,
    ) as exc:
        raise _translate_profile_error(exc) from exc
    return _profile_response(profile)


@router.get(
    "",
    response_model=ProfileResponse,
    summary="读取本人信息",
    responses=ERROR_RESPONSES,
)
def read_profile(profile: CompletedProfileDependency) -> ProfileResponse:
    return _profile_response(profile)


@router.patch(
    "",
    response_model=ProfileResponse,
    summary="更正本人信息",
    responses=ERROR_RESPONSES,
)
def update_profile(
    payload: ProfileUpdateRequest,
    current_user: CurrentUserDependency,
    _existing_profile: CompletedProfileDependency,
    session: DatabaseSessionDependency,
    _csrf: CsrfProtectionDependency,
) -> ProfileResponse:
    try:
        profile = update_self_profile(
            session,
            current_user.id,
            name=payload.name,
            location=payload.location,
            fields=set(payload.model_fields_set),
        )
    except (
        AccountUnavailableError,
        ContactDistinguishingInfoRequiredError,
        ContactIdentityConflictError,
        ContactIdentityHashCollisionError,
        ProfileNotFoundError,
        ProfileValidationError,
    ) as exc:
        raise _translate_profile_error(exc) from exc
    return _profile_response(profile)
