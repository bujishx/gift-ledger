# ruff: noqa: RUF002, RUF003
"""旧账上传预检、明确确认与持久批次结果接口。

面向前端的 HTTP 入口包括：

- ``POST /api/v1/imports/preview``：接收一个 ``.xlsx`` 文件，安全解析并做只读业务预检；
  保存原始字节和用户选择，返回逐行问题、待确认项、最终计划及问题清单下载 token；
  只有无必修问题且全部待确认项已处理时，才允许确认。
- ``POST /api/v1/imports/{token}/confirm``：原子领取 token，重新解析文件并在单个事务里
  重新匹配、核对已审阅计划后整批写入；完成结果与业务记录同事务持久保存，重复请求返回
  同一批次结果。回滚后释放领取状态，保留文件供修正后的再次预检或查询。
- ``GET /api/v1/imports/{token}/result`` 与 ``GET /api/v1/imports/batches``：查询当前状态
  或当前用户最近的已完成批次；临时文件到期不影响已持久保存的完成结果。
- ``GET /api/v1/imports/{token}/issues.xlsx``：独立下载逐行问题清单。

本模块只负责 HTTP 边界：鉴权、本人建档、可信来源、CSRF、有界读取、错误映射和临时文件
清理。解析、业务匹配、事务写入全部复用既有服务，不在此重新实现；也不自行另造 token，
统一复用 :class:`~app.services.import_preview_store.ImportPreviewStore`。
"""

from __future__ import annotations

import json
from hashlib import sha256
from io import BytesIO
from typing import Annotated

from fastapi import APIRouter, File, Form, Response, UploadFile, status
from openpyxl import Workbook

from app.api.dependencies import (
    CompletedProfileDependency,
    CsrfProtectionDependency,
    CurrentUserDependency,
    DatabaseSessionDependency,
    SensitiveActionGateDependency,
    SettingsDependency,
)
from app.api.errors import ApiError, ErrorResponse
from app.schemas.legacy_import import (
    LegacyImportBatchListResponse,
    LegacyImportConfirmResponse,
    LegacyImportPreviewResponse,
)
from app.services.import_preview_store import ImportPreviewStore, ImportPreviewStoreError
from app.services.legacy_import_analysis import (
    analyze_old_ledger_import,
    legacy_import_plan_fingerprint,
    legacy_import_preview_response,
)
from app.services.legacy_import_commit import (
    ERROR_ACCOUNT_UNAVAILABLE,
    ERROR_IMPORT_COMMIT_FAILED,
    ERROR_IMPORT_STATE_CHANGED,
    ERROR_IMPORT_VALIDATION_FAILED,
    LegacyImportCommitError,
    commit_old_ledger_import,
    get_completed_import,
    list_completed_imports,
)
from app.services.old_ledger_excel import OldLedgerWorkbookError, parse_old_ledger_workbook
from app.services.sensitive_actions import import_plan_context

router = APIRouter(tags=["旧账导入"])

# 接口自身的稳定错误码（不扩大全局错误结构，只是本路由使用的字符串）。
IMPORT_FILE_TYPE_INVALID = "IMPORT_FILE_TYPE_INVALID"

# 安全通用消息：不向客户端暴露文件内容、内部路径、SQL 或堆栈。
_INVALID_PREVIEW_MESSAGE = "导入预检凭证不存在、已过期或已经使用"
_COMMIT_FAILED_MESSAGE = "导入失败，请稍后重试"

_NO_STORE = {"Cache-Control": "no-store"}

IMPORT_ERROR_RESPONSES = {
    401: {"model": ErrorResponse},
    403: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    410: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
    500: {"model": ErrorResponse},
}


def _build_store(settings) -> ImportPreviewStore:
    return ImportPreviewStore(
        settings.import_temp_dir,
        ttl_minutes=settings.import_preview_ttl_minutes,
    )


def _map_store_error(exc: ImportPreviewStoreError) -> ApiError:
    if exc.code == "IMPORT_PREVIEW_EXPIRED":
        return ApiError(
            status.HTTP_410_GONE,
            "IMPORT_PREVIEW_EXPIRED",
            exc.message,
            dict(_NO_STORE),
        )
    # 外用户、已使用、不存在和畸形 token 统一使用相同响应，不暴露归属信息。
    return ApiError(
        status.HTTP_404_NOT_FOUND,
        "IMPORT_PREVIEW_INVALID",
        _INVALID_PREVIEW_MESSAGE,
        dict(_NO_STORE),
    )


def _map_commit_error(exc: LegacyImportCommitError) -> ApiError:
    if exc.code == ERROR_ACCOUNT_UNAVAILABLE:
        return ApiError(
            status.HTTP_401_UNAUTHORIZED,
            exc.code,
            exc.message,
            {"WWW-Authenticate": "Cookie", "Cache-Control": "no-store"},
        )
    if exc.code == ERROR_IMPORT_VALIDATION_FAILED:
        return ApiError(status.HTTP_409_CONFLICT, exc.code, exc.message, dict(_NO_STORE))
    if exc.code == ERROR_IMPORT_STATE_CHANGED:
        return ApiError(status.HTTP_409_CONFLICT, exc.code, exc.message, dict(_NO_STORE))
    # 普通内部失败使用安全通用消息，绝不透出内部细节。
    return ApiError(
        status.HTTP_500_INTERNAL_SERVER_ERROR,
        ERROR_IMPORT_COMMIT_FAILED,
        _COMMIT_FAILED_MESSAGE,
        dict(_NO_STORE),
    )


def _read_workbook_error(exc: OldLedgerWorkbookError) -> ApiError:
    return ApiError(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        exc.code,
        exc.message,
        dict(_NO_STORE),
    )


@router.post(
    "/imports/preview",
    response_model=LegacyImportPreviewResponse,
    summary="旧账文件预检",
    responses=IMPORT_ERROR_RESPONSES,
)
def preview_import(
    response: Response,
    file: Annotated[UploadFile, File(...)],
    current_user: CurrentUserDependency,
    _profile: CompletedProfileDependency,
    session: DatabaseSessionDependency,
    _csrf: CsrfProtectionDependency,
    settings: SettingsDependency,
    decisions: Annotated[str, Form()] = "{}",
) -> LegacyImportPreviewResponse:
    response.headers["Cache-Control"] = "no-store"

    filename = file.filename or ""
    if not filename.lower().endswith(".xlsx"):
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            IMPORT_FILE_TYPE_INVALID,
            "请上传 .xlsx 格式的旧账模板文件",
            dict(_NO_STORE),
        )

    # 有界读取：最多读取 import_max_file_bytes + 1 字节，超出即拒绝，绝不无上限读取。
    content = file.file.read(settings.import_max_file_bytes + 1)

    try:
        parsed = parse_old_ledger_workbook(
            content,
            max_file_bytes=settings.import_max_file_bytes,
            max_rows=settings.import_max_rows,
            max_uncompressed_bytes=settings.import_max_uncompressed_bytes,
        )
    except OldLedgerWorkbookError as exc:
        raise _read_workbook_error(exc) from exc

    try:
        if len(decisions) > 1024 * 1024:
            raise ValueError("too large")
        chosen = json.loads(decisions)
        if (
            not isinstance(chosen, dict)
            or len(chosen) > 20000
            or any(
                not isinstance(key, str)
                or not isinstance(value, str)
                or len(key) > 100
                or len(value) > 100
                for key, value in chosen.items()
            )
        ):
            raise ValueError("invalid decisions")
    except (ValueError, TypeError):
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "IMPORT_DECISIONS_INVALID",
            "确认决定格式不正确，请重新选择后预检",
            dict(_NO_STORE),
        ) from None
    analysis = (
        analyze_old_ledger_import(session, current_user.id, parsed, chosen)
        if chosen
        else analyze_old_ledger_import(session, current_user.id, parsed)
    )
    preview = legacy_import_preview_response(analysis)

    preview_token: str | None = None
    expires_at = None
    try:
        preview_token, expires_at = _build_store(settings).create(
            user_id=current_user.id,
            content=content,
            decisions=chosen,
            plan_fingerprint=legacy_import_plan_fingerprint(analysis)
            if preview.can_import
            else None,
            issues=[item.model_dump(mode="json") for item in preview.issues],
        )
    except Exception:
        raise ApiError(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            ERROR_IMPORT_COMMIT_FAILED,
            _COMMIT_FAILED_MESSAGE,
            dict(_NO_STORE),
        ) from None

    return legacy_import_preview_response(
        analysis,
        preview_token=preview_token,
        expires_at=expires_at,
    )


@router.post(
    "/imports/{token}/confirm",
    response_model=LegacyImportConfirmResponse,
    summary="确认并原子导入旧账",
    responses=IMPORT_ERROR_RESPONSES,
)
def confirm_import(
    response: Response,
    token: str,
    current_user: CurrentUserDependency,
    _profile: CompletedProfileDependency,
    session: DatabaseSessionDependency,
    _csrf: CsrfProtectionDependency,
    settings: SettingsDependency,
    authorization: SensitiveActionGateDependency,
) -> LegacyImportConfirmResponse:
    response.headers["Cache-Control"] = "no-store"
    batch_id = sha256(token.encode()).hexdigest()
    completed = get_completed_import(session, current_user.id, batch_id)
    if completed:
        return completed

    store = _build_store(settings)
    try:
        preview, _preview_status = store.read(token, user_id=current_user.id)
        authorization.authorize(
            "legacy_import", import_plan_context(token, preview.get("plan_fingerprint") or "")
        )
        claimed = store.claim(token, user_id=current_user.id)
    except ImportPreviewStoreError as exc:
        completed = get_completed_import(session, current_user.id, batch_id)
        if completed:
            return completed
        try:
            _payload, preview_status = store.read(token, user_id=current_user.id)
            if preview_status == "processing":
                raise ApiError(
                    status.HTTP_409_CONFLICT,
                    "IMPORT_PROCESSING",
                    "本批正在处理，请稍后查询结果",
                    dict(_NO_STORE),
                ) from exc
        except ImportPreviewStoreError:
            pass
        raise _map_store_error(exc) from exc

    succeeded = False
    try:
        if claimed.plan_fingerprint is None:
            raise LegacyImportCommitError(
                ERROR_IMPORT_VALIDATION_FAILED, "本批仍有必修问题或待确认决定，请重新预检"
            )
        parsed = parse_old_ledger_workbook(
            claimed.content,
            max_file_bytes=settings.import_max_file_bytes,
            max_rows=settings.import_max_rows,
            max_uncompressed_bytes=settings.import_max_uncompressed_bytes,
        )
        result = commit_old_ledger_import(
            session,
            current_user.id,
            parsed,
            decisions=claimed.decisions,
            expected_fingerprint=claimed.plan_fingerprint,
            batch_id=batch_id,
        )
        succeeded = True
        return result
    except OldLedgerWorkbookError as exc:
        # 预检时已通过解析，再失败属于内部异常；finally 释放本次领取供查询和重试。
        raise ApiError(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            ERROR_IMPORT_COMMIT_FAILED,
            _COMMIT_FAILED_MESSAGE,
            dict(_NO_STORE),
        ) from exc
    except LegacyImportCommitError as exc:
        raise _map_commit_error(exc) from exc
    except Exception:
        raise ApiError(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            ERROR_IMPORT_COMMIT_FAILED,
            _COMMIT_FAILED_MESSAGE,
            dict(_NO_STORE),
        ) from None
    finally:
        # 成功清理原文件；失败已回滚，释放本次领取并保留预检原件。
        # Windows 上杀毒软件可能短暂占用 xlsx；清理异常不能覆盖已经提交成功的导入结果。
        try:
            if succeeded:
                claimed.discard()
            else:
                claimed.release()
        except OSError:
            pass


@router.get(
    "/imports/batches",
    response_model=LegacyImportBatchListResponse,
    summary="最近已完成旧账导入批次",
)
def completed_batches(
    response: Response,
    current_user: CurrentUserDependency,
    _profile: CompletedProfileDependency,
    session: DatabaseSessionDependency,
):
    response.headers["Cache-Control"] = "no-store"
    return LegacyImportBatchListResponse(batches=list_completed_imports(session, current_user.id))


@router.get(
    "/imports/batches/{batch_id}",
    response_model=LegacyImportConfirmResponse,
    summary="查询已完成旧账导入批次",
)
def completed_batch(
    response: Response,
    batch_id: str,
    current_user: CurrentUserDependency,
    _profile: CompletedProfileDependency,
    session: DatabaseSessionDependency,
):
    response.headers["Cache-Control"] = "no-store"
    result = get_completed_import(session, current_user.id, batch_id)
    if result is None:
        raise ApiError(
            status.HTTP_404_NOT_FOUND,
            "IMPORT_BATCH_NOT_FOUND",
            "未找到本账号的已完成批次",
            dict(_NO_STORE),
        )
    return result


@router.get("/imports/{token}/result", summary="查询本次旧账导入状态")
def import_result(
    response: Response,
    token: str,
    current_user: CurrentUserDependency,
    _profile: CompletedProfileDependency,
    session: DatabaseSessionDependency,
    settings: SettingsDependency,
):
    response.headers["Cache-Control"] = "no-store"
    result = get_completed_import(session, current_user.id, sha256(token.encode()).hexdigest())
    if result:
        return {"status": "completed", "result": result.model_dump(mode="json")}
    try:
        _payload, preview_status = _build_store(settings).read(token, user_id=current_user.id)
    except ImportPreviewStoreError as exc:
        raise _map_store_error(exc) from exc
    return {"status": preview_status, "result": None}


@router.get("/imports/{token}/issues.xlsx", summary="下载旧账预检问题清单")
def import_issues(
    token: str,
    current_user: CurrentUserDependency,
    _profile: CompletedProfileDependency,
    settings: SettingsDependency,
):
    try:
        payload, _preview_status = _build_store(settings).read(token, user_id=current_user.id)
    except ImportPreviewStoreError as exc:
        raise _map_store_error(exc) from exc
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "问题清单"
    sheet.append(["原始行号", "字段", "原值", "问题类型", "原因", "处理建议"])
    for issue in payload.get("issues", []):
        sheet.append(
            [
                issue.get("row_number"),
                issue.get("field"),
                issue.get("raw_value"),
                {"error": "必修问题", "confirmation": "待确认", "info": "提示"}.get(
                    issue.get("severity"), "必修问题"
                ),
                issue.get("message"),
                issue.get("remedy"),
            ]
        )
    from openpyxl.styles import Alignment, Font, PatternFill

    for cell in sheet[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="345779")
    for column, width in zip("ABCDEF", (14, 24, 32, 16, 65, 65), strict=True):
        sheet.column_dimensions[column].width = width
    for row in sheet.iter_rows(min_row=2):
        for cell in row:
            if isinstance(cell.value, str):
                cell.data_type = "s"
            cell.alignment = Alignment(wrap_text=True, vertical="top")
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    content = BytesIO()
    workbook.save(content)
    workbook.close()
    return Response(
        content.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            **_NO_STORE,
            "Content-Disposition": 'attachment; filename="legacy-import-issues.xlsx"',
        },
    )


__all__ = ["router"]
