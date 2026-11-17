# ruff: noqa: RUF002
"""管理员整库备份的受保护 HTTP 接口。

只暴露「查看状态、手动生成、下载」三类能力；整库恢复**不提供**网页接口，只能由服务器本机
维护命令执行。所有接口仅允许 ``is_admin=1`` 的已登录账号，普通用户统一返回 403，不因备份
编号不同而泄漏备份是否存在。

敏感响应统一禁止缓存；下载固定 ``application/octet-stream``、ASCII 文件名、附件下载与
``no-store``／``no-cache``／``nosniff`` 安全头。
"""

from __future__ import annotations

from fastapi import APIRouter, Response, status
from fastapi.responses import FileResponse

from app.api.dependencies import (
    AdminUserDependency,
    CsrfProtectionDependency,
    SettingsDependency,
)
from app.api.errors import ApiError, ErrorResponse
from app.services.admin_backup import AdminBackupManager, BackupUnavailableError
from app.services.backup_index import BackupIndexCorruptedError
from app.services.database_backup import (
    BACKUP_TYPE_MANUAL,
    ERROR_CONNECTION_UNAVAILABLE,
    DatabaseBackupError,
)
from app.services.task_lock import TaskLockBusyError

router = APIRouter(tags=["管理员整库备份"])

MEDIA_TYPE = "application/octet-stream"
_NO_STORE = {"Cache-Control": "no-store"}
_DOWNLOAD_HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
    "X-Content-Type-Options": "nosniff",
}

ADMIN_BACKUP_ERROR_RESPONSES = {
    401: {"model": ErrorResponse},
    403: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    500: {"model": ErrorResponse},
    503: {"model": ErrorResponse},
    504: {"model": ErrorResponse},
}

_GENERIC_FAILURE_MESSAGE = "整库备份操作失败，请稍后重试"
_INDEX_CORRUPTED_MESSAGE = "备份索引已损坏，无法读取备份状态，请在本机检查备份目录"


def _manager(settings) -> AdminBackupManager:
    return AdminBackupManager(settings)


def _map_backup_error(exc: DatabaseBackupError) -> ApiError:
    if exc.code == ERROR_CONNECTION_UNAVAILABLE:
        return ApiError(status.HTTP_503_SERVICE_UNAVAILABLE, exc.code, exc.message, dict(_NO_STORE))
    if exc.code == "backup_command_timeout":
        return ApiError(
            status.HTTP_504_GATEWAY_TIMEOUT,
            exc.code,
            exc.message,
            dict(_NO_STORE),
        )
    if exc.code == "backup_index_corrupted":
        return ApiError(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            exc.code,
            _INDEX_CORRUPTED_MESSAGE,
            dict(_NO_STORE),
        )
    return ApiError(
        status.HTTP_500_INTERNAL_SERVER_ERROR,
        exc.code,
        _GENERIC_FAILURE_MESSAGE,
        dict(_NO_STORE),
    )


def _map_unavailable_error(exc: BackupUnavailableError) -> ApiError:
    if exc.code == "BACKUP_NOT_FOUND":
        return ApiError(status.HTTP_404_NOT_FOUND, exc.code, exc.message, dict(_NO_STORE))
    if exc.code == "BACKUP_NOT_READY":
        return ApiError(status.HTTP_409_CONFLICT, exc.code, exc.message, dict(_NO_STORE))
    return ApiError(
        status.HTTP_500_INTERNAL_SERVER_ERROR,
        exc.code,
        exc.message,
        dict(_NO_STORE),
    )


@router.get(
    "/admin/backups/database",
    summary="查看整库备份状态",
    responses=ADMIN_BACKUP_ERROR_RESPONSES,
)
def list_database_backups(
    response: Response,
    _admin: AdminUserDependency,
    settings: SettingsDependency,
) -> dict:
    response.headers["Cache-Control"] = "no-store"
    try:
        return _manager(settings).status()
    except BackupIndexCorruptedError as exc:
        raise ApiError(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "BACKUP_INDEX_CORRUPTED",
            _INDEX_CORRUPTED_MESSAGE,
            dict(_NO_STORE),
        ) from exc


@router.post(
    "/admin/backups/database",
    status_code=status.HTTP_201_CREATED,
    summary="生成一次手动整库备份",
    responses=ADMIN_BACKUP_ERROR_RESPONSES,
)
def create_database_backup(
    response: Response,
    _admin: AdminUserDependency,
    _csrf: CsrfProtectionDependency,
    settings: SettingsDependency,
) -> dict:
    response.headers["Cache-Control"] = "no-store"
    try:
        record = _manager(settings).create_backup(
            BACKUP_TYPE_MANUAL,
            application_version=settings.app_version,
        )
    except TaskLockBusyError as exc:
        raise ApiError(
            status.HTTP_409_CONFLICT,
            "BACKUP_TASK_CONFLICT",
            "已有备份或恢复任务正在运行，请稍后再试",
            dict(_NO_STORE),
        ) from exc
    except DatabaseBackupError as exc:
        raise _map_backup_error(exc) from exc
    return {
        "backup_id": record.backup_id,
        "backup_type": record.backup_type,
        "created_at": record.created_at.isoformat(),
        "completed_at": record.completed_at.isoformat() if record.completed_at else None,
        "size": record.size,
        "database": record.database,
        "schema_revision": record.schema_revision,
        "success": record.success,
        "offsite_status": record.offsite_status,
        "overall_healthy": record.offsite_status != "failed",
        "warning": record.failure_summary,
    }


@router.get(
    "/admin/backups/database/{backup_id}/download",
    summary="下载已完成且文件哈希完整的整库备份",
    responses=ADMIN_BACKUP_ERROR_RESPONSES,
)
def download_database_backup(
    backup_id: str,
    _admin: AdminUserDependency,
    settings: SettingsDependency,
) -> FileResponse:
    try:
        download = _manager(settings).open_download(backup_id)
    except BackupUnavailableError as exc:
        raise _map_unavailable_error(exc) from exc
    except BackupIndexCorruptedError as exc:
        raise ApiError(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "BACKUP_INDEX_CORRUPTED",
            _INDEX_CORRUPTED_MESSAGE,
            dict(_NO_STORE),
        ) from exc
    return FileResponse(
        path=download.path,
        media_type=MEDIA_TYPE,
        filename=download.filename,
        headers={
            **_DOWNLOAD_HEADERS,
        },
    )


__all__ = ["router"]
