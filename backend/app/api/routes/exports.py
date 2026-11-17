from typing import Annotated

from fastapi import APIRouter, Query, Response, status

from app.api.dependencies import (
    CompletedProfileDependency,
    CurrentUserDependency,
    DatabaseSessionDependency,
    SensitiveActionGateDependency,
)
from app.api.errors import ApiError, ErrorResponse
from app.schemas.exports import QueryExportFilters
from app.services.exports import (
    ExcelDownload,
    all_data_excel,
    old_ledger_template_excel,
    query_excel,
)
from app.services.ledger_query_common import LedgerQueryValidationError
from app.services.sensitive_actions import query_export_context

router = APIRouter(tags=["数据导出"])

XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
EXPORT_ERROR_RESPONSES = {
    401: {"model": ErrorResponse},
    403: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
}


def _download_response(download: ExcelDownload) -> Response:
    return Response(
        content=download.content,
        media_type=XLSX_MEDIA_TYPE,
        headers={
            "Content-Disposition": f'attachment; filename="{download.filename}"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


def _translate_query_error(exc: LedgerQueryValidationError) -> ApiError:
    if exc.code == "SELF_CONTACT_REQUIRED":
        return ApiError(status.HTTP_403_FORBIDDEN, exc.code, exc.message)
    return ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, exc.code, exc.message)


@router.get(
    "/exports/query.xlsx",
    summary="导出当前查账结果",
    responses=EXPORT_ERROR_RESPONSES,
)
def export_query_results(
    filters: Annotated[QueryExportFilters, Query()],
    current_user: CurrentUserDependency,
    _profile: CompletedProfileDependency,
    session: DatabaseSessionDependency,
    authorization: SensitiveActionGateDependency,
) -> Response:
    authorization.authorize("export_query", query_export_context(filters))
    try:
        return _download_response(query_excel(session, current_user.id, filters))
    except LedgerQueryValidationError as exc:
        raise _translate_query_error(exc) from exc


@router.get(
    "/exports/all.xlsx",
    summary="导出本人全部可读数据",
    responses=EXPORT_ERROR_RESPONSES,
)
def export_all_data(
    current_user: CurrentUserDependency,
    _profile: CompletedProfileDependency,
    session: DatabaseSessionDependency,
    authorization: SensitiveActionGateDependency,
) -> Response:
    authorization.authorize("export_all")
    try:
        return _download_response(all_data_excel(session, current_user.id))
    except LedgerQueryValidationError as exc:
        raise _translate_query_error(exc) from exc


@router.get(
    "/imports/template.xlsx",
    summary="下载标准旧账导入模板",
    responses=EXPORT_ERROR_RESPONSES,
)
def download_old_ledger_template(
    current_user: CurrentUserDependency,
    _profile: CompletedProfileDependency,
    session: DatabaseSessionDependency,
) -> Response:
    try:
        return _download_response(old_ledger_template_excel(session, current_user.id))
    except LedgerQueryValidationError as exc:
        raise _translate_query_error(exc) from exc


__all__ = ["router"]
