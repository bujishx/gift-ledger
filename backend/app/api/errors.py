from dataclasses import dataclass, field

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: dict[str, int | str] | None = None


class ErrorResponse(BaseModel):
    error: ErrorDetail


@dataclass
class ApiError(Exception):
    status_code: int
    code: str
    message: str
    headers: dict[str, str] = field(default_factory=dict)
    details: dict[str, int | str] = field(default_factory=dict)


def _response(
    status_code: int,
    code: str,
    message: str,
    headers=None,
    details: dict[str, int | str] | None = None,
) -> JSONResponse:
    error = {"code": code, "message": message}
    if details:
        error["details"] = details
    return JSONResponse(
        status_code=status_code,
        content={"error": error},
        headers=headers,
    )


def register_error_handlers(application: FastAPI) -> None:
    @application.exception_handler(ApiError)
    async def handle_api_error(_request: Request, exc: ApiError) -> JSONResponse:
        return _response(exc.status_code, exc.code, exc.message, exc.headers, exc.details)

    @application.exception_handler(RequestValidationError)
    async def handle_validation_error(
        _request: Request, _exc: RequestValidationError
    ) -> JSONResponse:
        return _response(422, "VALIDATION_ERROR", "提交的信息格式不正确，请检查后重试")
