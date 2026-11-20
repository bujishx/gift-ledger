import logging

from fastapi import APIRouter, HTTPException, Request, Response, status

from app.api.dependencies import (
    CsrfProtectionDependency,
    CurrentUserDependency,
    DatabaseSessionDependency,
    GiftChangeReauthStoreDependency,
    SensitiveActionStoreDependency,
    SessionIdDependency,
    SettingsDependency,
    TrustedOriginDependency,
    require_local_entry,
    sensitive_action_api_error,
)
from app.api.errors import ApiError, ErrorResponse
from app.api.maintenance_access import owner_sessions
from app.api.security import clear_auth_cookies, set_auth_cookies
from app.core.security import (
    PasswordPolicyError,
    SecurityConfigurationError,
    UsernamePolicyError,
    create_access_token,
    verify_password,
)
from app.db.models import User
from app.schemas.auth import (
    AccountResponse,
    CredentialsRequest,
    GiftChangeVerifyRequest,
    MessageResponse,
)
from app.schemas.sensitive_actions import (
    SensitiveActionGrantResponse,
    SensitiveActionVerifyRequest,
)
from app.services.auth import (
    AuthenticationFailedError,
    UsernameAlreadyExistsError,
    authenticate_user,
    get_self_name,
    register_user,
)
from app.services.import_preview_store import ImportPreviewStore, ImportPreviewStoreError
from app.services.maintenance_sessions import SessionStoreError
from app.services.personal_safety_backup_store import (
    PersonalSafetyBackupStore,
    PersonalSafetyBackupStoreError,
)
from app.services.sensitive_actions import (
    SensitiveActionError,
    import_plan_context,
    query_export_context,
)

audit_log = logging.getLogger("app.security_audit")

router = APIRouter(prefix="/auth", tags=["账号与登录"])

ERROR_RESPONSES = {
    401: {"model": ErrorResponse},
    403: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
    503: {"model": ErrorResponse},
}


def _account_response(user: User, display_name: str | None) -> AccountResponse:
    return AccountResponse(
        id=user.id,
        username=user.username,
        is_admin=bool(user.is_admin),
        profile_completed=display_name is not None,
        display_name=display_name,
    )


@router.post(
    "/register",
    response_model=AccountResponse,
    status_code=status.HTTP_201_CREATED,
    summary="注册普通账号",
    responses=ERROR_RESPONSES,
)
def register(
    payload: CredentialsRequest,
    settings: SettingsDependency,
    session: DatabaseSessionDependency,
    _trusted_origin: TrustedOriginDependency,
) -> AccountResponse:
    if not settings.registration_enabled:
        raise ApiError(
            status.HTTP_403_FORBIDDEN,
            "REGISTRATION_DISABLED",
            "当前暂未开放新用户注册",
        )
    try:
        user = register_user(session, payload.username, payload.password)
    except UsernamePolicyError as exc:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "INVALID_USERNAME",
            str(exc),
        ) from exc
    except PasswordPolicyError as exc:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            exc.code,
            exc.message,
        ) from exc
    except UsernameAlreadyExistsError as exc:
        raise ApiError(
            status.HTTP_409_CONFLICT,
            "USERNAME_ALREADY_EXISTS",
            "该用户名已被使用，请换一个用户名",
        ) from exc
    return _account_response(user, None)


@router.post(
    "/login",
    response_model=AccountResponse,
    summary="登录",
    responses=ERROR_RESPONSES,
)
def login(
    payload: CredentialsRequest,
    request: Request,
    response: Response,
    settings: SettingsDependency,
    session: DatabaseSessionDependency,
    _trusted_origin: TrustedOriginDependency,
) -> AccountResponse:
    try:
        user = authenticate_user(session, payload.username, payload.password)
        access_token = create_access_token(user.id, settings.jwt_secret, user.password_hash)
    except AuthenticationFailedError as exc:
        raise ApiError(
            status.HTTP_401_UNAUTHORIZED,
            "INVALID_CREDENTIALS",
            "用户名或密码错误",
            {"WWW-Authenticate": "Cookie"},
        ) from exc
    except SecurityConfigurationError as exc:
        raise ApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "AUTH_NOT_CONFIGURED",
            "登录功能尚未完成安全配置",
        ) from exc

    account = _account_response(user, get_self_name(session, user.id))
    set_auth_cookies(response, access_token, settings)
    if hasattr(request.app.state, "maintenance_owner_dir"):
        try:
            owner_sessions(request).record_business_login(user.id, user.username)
        except (HTTPException, SessionStoreError):
            audit_log.warning("maintenance_login_activity_unavailable")
    return account


@router.post(
    "/logout",
    response_model=MessageResponse,
    summary="退出登录",
    responses=ERROR_RESPONSES,
)
def logout(
    response: Response,
    settings: SettingsDependency,
    _current_user: CurrentUserDependency,
    session_id: SessionIdDependency,
    store: GiftChangeReauthStoreDependency,
    sensitive_store: SensitiveActionStoreDependency,
    _csrf: CsrfProtectionDependency,
) -> MessageResponse:
    clear_auth_cookies(response, settings)
    store.revoke(session_id)
    sensitive_store.revoke_session(session_id)
    return MessageResponse(message="已退出登录")


@router.get(
    "/me",
    response_model=AccountResponse,
    summary="读取当前账号状态",
    responses=ERROR_RESPONSES,
)
def me(
    current_user: CurrentUserDependency,
    session: DatabaseSessionDependency,
) -> AccountResponse:
    return _account_response(current_user, get_self_name(session, current_user.id))


@router.post(
    "/gift-changes/verify",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="验证密码，开启流水修改／删除的会话级再验证",
    responses=ERROR_RESPONSES,
)
def verify_gift_changes(
    payload: GiftChangeVerifyRequest,
    current_user: CurrentUserDependency,
    session_id: SessionIdDependency,
    store: GiftChangeReauthStoreDependency,
    sensitive_store: SensitiveActionStoreDependency,
    _csrf: CsrfProtectionDependency,
) -> Response:
    try:
        sensitive_store.begin_password_attempt(current_user.id)
    except SensitiveActionError as exc:
        raise sensitive_action_api_error(exc) from exc
    if not verify_password(payload.password, current_user.password_hash):
        audit_log.info("password_verification user_id=%s purpose=gift_change outcome=denied",
                       current_user.id)
        raise ApiError(
            status.HTTP_401_UNAUTHORIZED,
            "INVALID_PASSWORD",
            "密码错误",
        )
    sensitive_store.password_succeeded(current_user.id)
    store.mark_verified(session_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/sensitive-actions/verify",
    response_model=SensitiveActionGrantResponse,
    summary="为本次批量操作验证密码并签发五分钟一次性授权",
    responses={**ERROR_RESPONSES, 429: {"model": ErrorResponse}},
)
def verify_sensitive_action(
    payload: SensitiveActionVerifyRequest,
    request: Request,
    response: Response,
    current_user: CurrentUserDependency,
    session_id: SessionIdDependency,
    store: SensitiveActionStoreDependency,
    settings: SettingsDependency,
    _csrf: CsrfProtectionDependency,
) -> SensitiveActionGrantResponse:
    response.headers["Cache-Control"] = "no-store"
    if payload.purpose in {"personal_backup", "safety_backup"}:
        require_local_entry(request, settings)
    try:
        store.begin_password_attempt(current_user.id)
    except SensitiveActionError as exc:
        raise sensitive_action_api_error(exc) from exc
    if not verify_password(payload.password.get_secret_value(), current_user.password_hash):
        audit_log.info("password_verification user_id=%s purpose=%s outcome=denied",
                       current_user.id, payload.purpose)
        raise ApiError(status.HTTP_401_UNAUTHORIZED, "INVALID_PASSWORD", "密码错误",
                       {"Cache-Control": "no-store"})
    store.password_succeeded(current_user.id)
    context = ""
    if payload.purpose == "export_query":
        context = query_export_context(payload.query_filters)
    elif payload.purpose == "legacy_import":
        try:
            preview, preview_status = ImportPreviewStore(
                settings.import_temp_dir, ttl_minutes=settings.import_preview_ttl_minutes
            ).read(payload.preview_token, user_id=current_user.id)
        except ImportPreviewStoreError:
            raise ApiError(status.HTTP_404_NOT_FOUND, "IMPORT_PREVIEW_INVALID",
                           "导入预检不存在或已过期，请重新预检",
                           {"Cache-Control": "no-store"}) from None
        fingerprint = preview.get("plan_fingerprint")
        if preview_status != "pending" or not fingerprint:
            raise ApiError(status.HTTP_409_CONFLICT, "IMPORT_VALIDATION_FAILED",
                           "请先完成预检并确认导入计划", {"Cache-Control": "no-store"})
        context = import_plan_context(payload.preview_token, fingerprint)
    elif payload.purpose == "safety_backup":
        try:
            PersonalSafetyBackupStore(
                settings.personal_safety_backup_dir,
                ttl_hours=settings.personal_safety_backup_ttl_hours,
            ).load(payload.safety_backup_token, user_id=current_user.id)
        except PersonalSafetyBackupStoreError:
            raise ApiError(status.HTTP_404_NOT_FOUND, "SAFETY_BACKUP_INVALID",
                           "恢复前安全备份不存在或已过期",
                           {"Cache-Control": "no-store"}) from None
        context = payload.safety_backup_token
    grant, expires_at = store.issue(user_id=current_user.id, session_id=session_id,
                                    purpose=payload.purpose, context=context)
    audit_log.info("password_verification user_id=%s purpose=%s outcome=authorized",
                   current_user.id, payload.purpose)
    return SensitiveActionGrantResponse(grant=grant, expires_at=expires_at)
