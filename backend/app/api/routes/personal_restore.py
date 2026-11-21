# ruff: noqa: RUF002, RUF003
"""个人备份下载、恢复预览、确认替换与安全备份下载接口。

本模块只负责 HTTP 边界：鉴权、本人建档、可信来源、CSRF、有界读取、下载安全头、错误映射和
一次性预览 token 生命周期。包格式解析、只读预检和单事务替换恢复全部复用既有服务，不在此重新
实现；预览临时文件复用个人恢复预览存储，恢复前安全备份复用安全备份存储。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, File, Query, Response, UploadFile, status

from app.api.dependencies import (
    CompletedProfileDependency,
    CsrfProtectionDependency,
    CurrentUserDependency,
    DatabaseSessionDependency,
    SensitiveActionGateDependency,
    SensitiveActionStoreDependency,
    SettingsDependency,
    sensitive_action_api_error,
)
from app.api.errors import ApiError, ErrorResponse
from app.api.security import clear_auth_cookies
from app.schemas.personal_restore import (
    PersonalRestoreConfirmRequest,
    PersonalRestoreConfirmResponse,
    PersonalRestoreCounts,
    PersonalRestorePreviewResponse,
    PersonalRestoreReceipt,
)
from app.services.personal_backup import (
    PACKAGE_SUFFIX,
    PersonalBackupError,
    PersonalBackupLimits,
    build_personal_backup,
)
from app.services.personal_restore import (
    ERROR_ACCOUNT_UNAVAILABLE,
    ERROR_BACKUP_INVALID,
    ERROR_DICTIONARY_RENAMES_INVALID,
    ERROR_PASSWORD_INCORRECT,
    ERROR_RESTORE_FAILED,
    ERROR_RESTORE_STATE_CHANGED,
    ERROR_SAFETY_BACKUP_FAILED,
    ERROR_SYSTEM_DICTIONARY_MISMATCH,
    RESTORE_MODE_REPLACE,
    PersonalRestoreError,
    analyze_personal_restore,
    recent_restore_receipts,
    replace_personal_ledger,
    verify_restore_password,
)
from app.services.personal_restore import (
    PersonalRestoreDictionaryRename as ServiceDictionaryRename,
)
from app.services.personal_restore_preview_store import (
    PersonalRestorePreviewStore,
    PersonalRestorePreviewStoreError,
)
from app.services.personal_safety_backup_store import (
    PersonalSafetyBackupStore,
    PersonalSafetyBackupStoreError,
)
from app.services.registration_limits import uses_password_budget
from app.services.sensitive_actions import SensitiveActionError

_PREVIEW_EXPIRED = "PERSONAL_RESTORE_EXPIRED"
_PREVIEW_INVALID = "PERSONAL_RESTORE_INVALID"
_PREVIEW_CORRUPTED = "PERSONAL_RESTORE_CORRUPTED"
_SAFETY_EXPIRED = "SAFETY_BACKUP_EXPIRED"
_SAFETY_INVALID = "SAFETY_BACKUP_INVALID"
_SAFETY_CORRUPTED = "SAFETY_BACKUP_CORRUPTED"

router = APIRouter(tags=["个人备份恢复"])

# 固定安全 MIME：不依赖上传文件的 Content-Type，避免被浏览器按未知类型直接打开执行。
GIFTLEDGER_MEDIA_TYPE = "application/octet-stream"
SAFETY_BACKUP_FILENAME = "gift-ledger-personal-safety-backup.giftledger"

PERSONAL_RESTORE_FILE_TYPE_INVALID = "PERSONAL_RESTORE_FILE_TYPE_INVALID"
PERSONAL_RESTORE_EMPTY_FILE = "PERSONAL_RESTORE_EMPTY_FILE"
PERSONAL_RESTORE_FILE_TOO_LARGE = "PERSONAL_RESTORE_FILE_TOO_LARGE"

# 安全通用消息：不向客户端暴露文件内容、内部路径、SQL、人员数据或堆栈。
_REPLACE_NOTICE = (
    "恢复将以备份内容完整替换当前账本，无法与现有账本合并；请确认备份来自本系统且为所需版本。"
)
_INVALID_PREVIEW_MESSAGE = "恢复预览凭证不存在、已过期或已经使用"
_SAFETY_BACKUP_INVALID_MESSAGE = "恢复前安全备份不存在、已过期或无法读取"
_RESTORE_FAILED_MESSAGE = "恢复失败，请稍后重试"

_NO_STORE = {"Cache-Control": "no-store"}
_DOWNLOAD_HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
    "X-Content-Type-Options": "nosniff",
}

PERSONAL_RESTORE_ERROR_RESPONSES = {
    401: {"model": ErrorResponse},
    403: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    410: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
    500: {"model": ErrorResponse},
}


def _build_preview_store(settings) -> PersonalRestorePreviewStore:
    return PersonalRestorePreviewStore(
        settings.personal_restore_temp_dir,
        ttl_minutes=settings.personal_restore_preview_ttl_minutes,
    )


def _build_safety_backup_store(settings) -> PersonalSafetyBackupStore:
    return PersonalSafetyBackupStore(
        settings.personal_safety_backup_dir,
        ttl_hours=settings.personal_safety_backup_ttl_hours,
    )


def _download_response(content: bytes, filename: str) -> Response:
    return Response(
        content=content,
        media_type=GIFTLEDGER_MEDIA_TYPE,
        headers={
            **_DOWNLOAD_HEADERS,
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


def _map_preview_store_error(exc: PersonalRestorePreviewStoreError) -> ApiError:
    if exc.code == _PREVIEW_EXPIRED:
        return ApiError(status.HTTP_410_GONE, exc.code, exc.message, dict(_NO_STORE))
    if exc.code == _PREVIEW_CORRUPTED:
        return ApiError(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            exc.code,
            exc.message,
            dict(_NO_STORE),
        )
    # 外用户、已使用、不存在和畸形 token 统一使用相同响应，不暴露归属信息。
    return ApiError(
        status.HTTP_404_NOT_FOUND,
        _PREVIEW_INVALID,
        _INVALID_PREVIEW_MESSAGE,
        dict(_NO_STORE),
    )


def _map_safety_backup_store_error(exc: PersonalSafetyBackupStoreError) -> ApiError:
    if exc.code == _SAFETY_EXPIRED:
        return ApiError(status.HTTP_410_GONE, exc.code, exc.message, dict(_NO_STORE))
    if exc.code == _SAFETY_CORRUPTED:
        return ApiError(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            exc.code,
            exc.message,
            dict(_NO_STORE),
        )
    # 外用户、不存在和畸形 token 使用相同模糊 404，不泄漏归属或路径。
    return ApiError(
        status.HTTP_404_NOT_FOUND,
        _SAFETY_INVALID,
        _SAFETY_BACKUP_INVALID_MESSAGE,
        dict(_NO_STORE),
    )


def _map_restore_error(exc: PersonalRestoreError) -> ApiError:
    if exc.code == ERROR_ACCOUNT_UNAVAILABLE:
        return ApiError(
            status.HTTP_401_UNAUTHORIZED,
            exc.code,
            exc.message,
            {"WWW-Authenticate": "Cookie", "Cache-Control": "no-store"},
        )
    if exc.code == ERROR_PASSWORD_INCORRECT:
        return ApiError(status.HTTP_401_UNAUTHORIZED, exc.code, exc.message, dict(_NO_STORE))
    if exc.code in {ERROR_BACKUP_INVALID, ERROR_DICTIONARY_RENAMES_INVALID}:
        return ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            exc.code,
            exc.message,
            dict(_NO_STORE),
        )
    if exc.code in {ERROR_SYSTEM_DICTIONARY_MISMATCH, ERROR_RESTORE_STATE_CHANGED}:
        return ApiError(status.HTTP_409_CONFLICT, exc.code, exc.message, dict(_NO_STORE))
    if exc.code == ERROR_SAFETY_BACKUP_FAILED:
        return ApiError(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            exc.code,
            exc.message,
            dict(_NO_STORE),
        )
    # 其他内部失败使用安全通用消息，绝不透出内部细节。
    return ApiError(
        status.HTTP_500_INTERNAL_SERVER_ERROR,
        ERROR_RESTORE_FAILED,
        _RESTORE_FAILED_MESSAGE,
        dict(_NO_STORE),
    )


def _counts_from_backup(counts) -> PersonalRestoreCounts:
    return PersonalRestoreCounts(
        relationship_types=counts.relationship_types,
        kinship_types=counts.kinship_types,
        event_types=counts.event_types,
        contacts=counts.contacts,
        events=counts.events,
        gifts=counts.gifts,
    )


@router.get(
    "/backups/personal",
    summary="下载本人账本备份",
    responses=PERSONAL_RESTORE_ERROR_RESPONSES,
)
def download_personal_backup(
    current_user: CurrentUserDependency,
    _profile: CompletedProfileDependency,
    session: DatabaseSessionDependency,
    settings: SettingsDependency,
    authorization: SensitiveActionGateDependency,
) -> Response:
    authorization.authorize("personal_backup")
    try:
        download = build_personal_backup(
            session,
            current_user.id,
            application_version=settings.app_version,
            limits=PersonalBackupLimits.from_settings(settings),
        )
    except PersonalBackupError as exc:
        if exc.code == "user_unavailable":
            raise ApiError(
                status.HTTP_401_UNAUTHORIZED,
                "ACCOUNT_UNAVAILABLE",
                "账号不可用，请重新登录",
                {"WWW-Authenticate": "Cookie", "Cache-Control": "no-store"},
            ) from exc
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "PERSONAL_BACKUP_UNRESTORABLE",
            "无法生成经恢复校验的个人备份。" + exc.message,
            dict(_NO_STORE),
        ) from exc
    return _download_response(download.content, download.filename)


@router.post(
    "/restores/personal/preview",
    response_model=PersonalRestorePreviewResponse,
    summary="个人备份恢复预检",
    responses=PERSONAL_RESTORE_ERROR_RESPONSES,
)
def preview_personal_restore(
    response: Response,
    file: Annotated[UploadFile, File(...)],
    current_user: CurrentUserDependency,
    _profile: CompletedProfileDependency,
    session: DatabaseSessionDependency,
    _csrf: CsrfProtectionDependency,
    settings: SettingsDependency,
) -> PersonalRestorePreviewResponse:
    response.headers["Cache-Control"] = "no-store"

    filename = file.filename or ""
    if not filename.lower().endswith(PACKAGE_SUFFIX):
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            PERSONAL_RESTORE_FILE_TYPE_INVALID,
            "请上传 .giftledger 格式的个人备份文件",
            dict(_NO_STORE),
        )

    # 有界读取：最多读取 personal_restore_max_file_bytes + 1 字节，超出即拒绝。
    content = file.file.read(settings.personal_restore_max_file_bytes + 1)
    if not content:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            PERSONAL_RESTORE_EMPTY_FILE,
            "上传的备份文件是空的",
            dict(_NO_STORE),
        )
    if len(content) > settings.personal_restore_max_file_bytes:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            PERSONAL_RESTORE_FILE_TOO_LARGE,
            "备份文件超过大小上限",
            dict(_NO_STORE),
        )

    try:
        analysis = analyze_personal_restore(
            session, current_user.id, content, limits=PersonalBackupLimits.from_settings(settings)
        )
    except PersonalRestoreError as exc:
        raise _map_restore_error(exc) from exc

    try:
        preview_token, expires_at = _build_preview_store(settings).create(
            user_id=current_user.id,
            content=content,
            ledger_revision=analysis.ledger_revision,
        )
    except PersonalRestorePreviewStoreError:
        raise ApiError(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            ERROR_RESTORE_FAILED,
            _RESTORE_FAILED_MESSAGE,
            dict(_NO_STORE),
        ) from None

    return PersonalRestorePreviewResponse(
        preview_token=preview_token,
        expires_at=expires_at,
        format_version=analysis.format_version,
        application_version=analysis.application_version,
        created_at=analysis.created_at,
        current=_counts_from_backup(analysis.current),
        incoming=_counts_from_backup(analysis.incoming),
        mode=RESTORE_MODE_REPLACE,
        replace_notice=_REPLACE_NOTICE,
        dictionary_name_conflicts=[
            {
                "category": conflict.category,
                "backup_id": conflict.backup_id,
                "original_name": conflict.original_name,
                "suggested_name": conflict.suggested_name,
            }
            for conflict in analysis.dictionary_name_conflicts
        ],
    )


@uses_password_budget
def _authorize_restore(token, password, current_user, settings, password_attempts):
    """Reserve password capacity before consuming the preview or charging a failure."""
    try:
        password_attempts.begin_password_attempt(current_user.id)
    except SensitiveActionError as exc:
        raise sensitive_action_api_error(exc) from exc

    store = _build_preview_store(settings)
    try:
        claimed = store.claim(token, user_id=current_user.id)
    except PersonalRestorePreviewStoreError as exc:
        raise _map_preview_store_error(exc) from exc

    try:
        credential = verify_restore_password(current_user, password)
    except PersonalRestoreError as exc:
        claimed.discard()
        raise _map_restore_error(exc) from exc
    password_attempts.password_succeeded(current_user.id)
    return claimed, credential


@router.post(
    "/restores/personal/{token}/confirm",
    response_model=PersonalRestoreConfirmResponse,
    summary="重新验证密码后替换恢复",
    responses=PERSONAL_RESTORE_ERROR_RESPONSES,
)
def confirm_personal_restore(
    response: Response,
    token: str,
    body: PersonalRestoreConfirmRequest,
    current_user: CurrentUserDependency,
    _profile: CompletedProfileDependency,
    session: DatabaseSessionDependency,
    _csrf: CsrfProtectionDependency,
    settings: SettingsDependency,
    password_attempts: SensitiveActionStoreDependency,
) -> PersonalRestoreConfirmResponse:
    response.headers["Cache-Control"] = "no-store"

    claimed, credential = _authorize_restore(
        token, body.password.get_secret_value(), current_user, settings, password_attempts
    )

    try:
        result = replace_personal_ledger(
            session,
            current_user.id,
            claimed.content,
            expected_revision=claimed.ledger_revision,
            limits=PersonalBackupLimits.from_settings(settings),
            credential=credential,
            safety_backup_store=_build_safety_backup_store(settings),
            application_version=settings.app_version,
            dictionary_renames=tuple(
                ServiceDictionaryRename(
                    category=item.category,
                    backup_id=item.backup_id,
                    original_name=item.original_name,
                    new_name=item.new_name,
                )
                for item in body.dictionary_renames
            ),
        )
    except PersonalRestoreError as exc:
        raise _map_restore_error(exc) from exc
    finally:
        # 确认成功、密码错误、状态冲突、安全备份失败或未知失败都必须一次性消耗 token。
        try:
            claimed.discard()
        except OSError:
            pass

    # 只有成功才清除当前认证与 CSRF Cookie；失败路径会抛出异常，不会走到这里。
    clear_auth_cookies(response, settings)
    return PersonalRestoreConfirmResponse(
        counts=_counts_from_backup(result.counts),
        safety_backup_token=result.safety_backup_token,
        safety_backup_expires_at=result.safety_backup_expires_at,
        reauth_required=result.reauth_required,
    )


@router.post("/restores/personal/receipts", response_model=list[PersonalRestoreReceipt])
def list_personal_restore_receipts(
    response: Response,
    current_user: CurrentUserDependency,
    _profile: CompletedProfileDependency,
    session: DatabaseSessionDependency,
    authorization: SensitiveActionGateDependency,
    before: Annotated[int | None, Query(gt=0)] = None,
):
    response.headers["Cache-Control"] = "no-store"
    authorization.authorize("restore_receipts")
    return recent_restore_receipts(session, current_user, before)


@router.get(
    "/restores/personal/safety-backups/{token}",
    summary="下载恢复前安全备份",
    responses=PERSONAL_RESTORE_ERROR_RESPONSES,
)
def download_personal_safety_backup(
    token: str,
    current_user: CurrentUserDependency,
    _profile: CompletedProfileDependency,
    settings: SettingsDependency,
    authorization: SensitiveActionGateDependency,
) -> Response:
    authorization.authorize("safety_backup", token)
    store = _build_safety_backup_store(settings)
    try:
        download = store.load(token, user_id=current_user.id)
    except PersonalSafetyBackupStoreError as exc:
        raise _map_safety_backup_store_error(exc) from exc
    return _download_response(download.content, SAFETY_BACKUP_FILENAME)


__all__ = ["router"]
